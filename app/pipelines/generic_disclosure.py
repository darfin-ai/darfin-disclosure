"""
범용(GENERIC) 공시 압축 + 요약/분석 프롬프트 모듈.

business_report.py(BIZ_REPORT)나 rights_offering.py(RIGHTS_OFFERING)처럼 보고서 양식별로
섹션을 정교하게 추출하는 전용 로직이 아직 없는 나머지 대분류(MAJOR_EVENT, ISSUANCE, EQUITY,
OTHER, AUDIT, FUND, ABS, EXCHANGE, FTC 등)에 공통으로 쓰이는 폴백 파이프라인이다.
registry.py가 type_code별 전용 PipelineSpec을 못 찾으면 이 모듈로 등록된 GENERIC spec을 대신 쓴다.

섹션 구분 없이 원문 앞부분을 그대로 잘라서 쓰는 단순한 방식 — 특정 유형의 사용량이 많아지면
business_report.py처럼 그 유형 전용 모듈을 만들어 registry.py에 따로 등록하면 된다.
"""
from dataclasses import dataclass

MAX_SUMMARY_CONTEXT = 3000   # 요약용 압축 최대 길이(자)
MAX_ANALYSIS_CHUNK = 4000    # 분석용 청크 최대 길이(자)


@dataclass
class ExtractedChunk:
    text: str
    start_offset: int


def compress_for_summary(full_text: str) -> str:
    """레지스트리 인터페이스: 요약용 압축 진입점. 별도 섹션 추출 없이 앞부분만 자른다."""
    return full_text.strip()[:MAX_SUMMARY_CONTEXT]


def extract_analysis_chunks(full_text: str) -> dict[str, ExtractedChunk]:
    """레지스트리 인터페이스: 분석용 좌표보존 압축 진입점. 원문 앞부분 전체를 하나의 청크로 쓴다."""
    text = full_text[:MAX_ANALYSIS_CHUNK]
    if not text.strip():
        return {}
    return {"FULL_TEXT": ExtractedChunk(text=text, start_offset=0)}


def build_analysis_input(chunks: dict[str, ExtractedChunk]) -> str:
    """레지스트리 인터페이스: 분석용 Gemini 입력 조립 진입점."""
    return "\n\n".join(chunk.text for chunk in chunks.values())


def resolve_offset(chunks: dict[str, ExtractedChunk], target_text: str) -> int:
    """레지스트리 인터페이스: targetKey -> 원문 오프셋 역산 진입점."""
    for chunk in chunks.values():
        local_idx = chunk.text.find(target_text)
        if local_idx != -1:
            return chunk.start_offset + local_idx
    return -1


# =====================================================================
# 요약 프롬프트 (보고서 양식 불문, 범용)
# =====================================================================

SUMMARY_SYSTEM_INSTRUCTION = (
    "당신은 여의도 최고의 시니어 주식 애널리스트입니다. "
    "상장사가 제출한 공시 원문을 읽고, 일반 개인 투자자가 10초 만에 핵심을 이해할 수 있도록 "
    "직관적이고 명확하게 요약해야 합니다. "
    "마크다운 기호(```json 등)나 부가 설명 없이 오직 지정된 JSON 객체 형식으로만 응답하십시오."
)

SUMMARY_USER_PROMPT_TEMPLATE = (
    "다음은 [{corp_name}]이 제출한 공시 원문의 일부입니다.\n\n"
    "[요약 미션]\n"
    "제시된 원문을 바탕으로 이 공시의 핵심 내용을 요약하십시오.\n\n"
    "[Output Guidelines]\n"
    "1. summary_text: 공시의 핵심 내용을 1문장(50자 이내)으로 압축하십시오.\n"
    "2. investor_comment: 투자자가 주목해야 할 핵심 포인트와 그 이유를 3문장 이내의 전문가 톤으로 해설하십시오.\n"
    "3. overall_risk: 원문에서 자본잠식, 대규모 적자, 횡령/배임, 상장폐지 사유 등 치명적 악재가 "
    "발견되면 'Critical' 또는 'High'를, 특별한 위험이 없다면 'Low'나 'Neutral'을 부여하십시오.\n\n"
    "[JSON Schema]\n"
    "{{\n"
    '  "summary_text": "(String) 1문장 핵심 요약",\n'
    '  "investor_comment": "(String) 3문장 이내의 투자자 관점 상세 해설",\n'
    '  "overall_risk": "(Enum: [Low, Neutral, High, Critical])"\n'
    "}}\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)

# =====================================================================
# 분석 프롬프트 (범용)
# =====================================================================

ANALYSIS_SYSTEM_INSTRUCTION = (
    "너는 기업 공시 분석 전문 애널리스트다. 상장사가 제출한 공시 원문을 분석하여, "
    "투자자 관점에서 중요한 사실과 그 영향을 식별하라."
)

ANALYSIS_USER_PROMPT_TEMPLATE = (
    "[Mission]\n"
    "제공된 공시 원문 발췌를 분석하여 투자자에게 중요한 항목을 식별하고 JSON 배열로 반환하라.\n\n"
    "[Output Guidelines]\n"
    "1. targetKey는 아래 발췌 원문에서 정확히 일치하는 문자열만 사용하라(재구성·요약 금지 — 한 글자도 다르면 안 됨).\n"
    "2. materialImpact는 결론부터 쉬운 말로 최대 2문장으로 써라. 전문용어를 쓸 경우 "
    "괄호로 쉬운 설명을 바로 덧붙이고, 배경 설명이나 원문 재진술로 늘리지 마라.\n"
    "3. 결과는 반드시 아래 구조의 순수한 JSON 배열 형태로만 출력하고, 마크다운 기호나 추가 텍스트를 붙이지 말 것.\n\n"
    "[JSON Schema]\n"
    "[\n"
    "  {{\n"
    '    "analysisCategory": "(String) 이 항목의 분류(예: Key_Fact, Risk, Opportunity, Governance)",\n'
    '    "targetKey": "(String) 원문 내 핵심 하이라이트 문장",\n'
    '    "materialImpact": "(String) 결론부터 쉬운 말로, 최대 2문장",\n'
    '    "riskLevel": "(Enum: [Low, Neutral, High, Critical])"\n'
    "  }}\n"
    "]\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)
