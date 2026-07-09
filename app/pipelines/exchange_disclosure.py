"""
거래소공시(EXCHANGE, 불성실공시법인 지정·관리종목 지정·매매거래정지 등) 압축 + 요약/분석
프롬프트 모듈.

이 대분류 자체가 대부분 "이미 위험 신호"인 공시다(거래소가 직접 부과하는 제재·조치이므로).
따라서 다른 모듈처럼 위험 키워드를 "찾아서 보강"하는 게 아니라, 조치 유형 자체를
risk_label 판단의 1차 근거로 삼는다.
"""
import re
from dataclasses import dataclass

ITEM_LABELS: dict[str, list[str]] = {
    "EX1_Action_Type": ["불성실공시법인", "관리종목", "투자주의환기종목", "매매거래정지", "상장적격성 실질심사"],
    "EX2_Reason": ["지정사유", "정지사유", "위반내용", "공시불이행", "공시번복"],
    "EX3_Duration_Schedule": ["정지기간", "지정기간", "해제요건", "개선기간"],
    "EX4_Follow_Up": ["향후계획", "이의신청", "조치내용", "벌점"],
}

MAX_ITEM_LENGTH = 450
TOTAL_MAX = 1800

# 거래소공시는 이 키워드 중 하나라도 있으면 사실상 자동으로 High 이상 신호다.
CRITICAL_KEYWORDS = [
    "상장폐지", "상장적격성 실질심사", "매매거래정지", "공시번복", "고의", "중과실",
]

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?다])\s+")


def compress_for_summary(full_text: str) -> str:
    if not full_text or not full_text.strip():
        return ""
    parts: list[str] = []
    for item_code, labels in ITEM_LABELS.items():
        chunk = _extract_item_chunk(full_text, labels, MAX_ITEM_LENGTH)
        if chunk:
            parts.append(f"[{item_code}]\n{chunk}")
    result = "\n\n".join(parts)
    for keyword in CRITICAL_KEYWORDS:
        if keyword in full_text and keyword not in result:
            sentence = _find_sentence_containing(full_text, keyword)
            if sentence:
                result += f"\n\n[중요 공시 사항]\n{sentence}"
    return result[:TOTAL_MAX]


def _extract_item_chunk(text: str, labels: list[str], max_len: int) -> str:
    start = -1
    for label in labels:
        idx = text.find(label)
        if idx != -1 and (start == -1 or idx < start):
            start = idx
    if start == -1:
        return ""
    end = min(start + max_len, len(text))
    return text[start:end].strip()


def _is_table_row(line: str) -> bool:
    """파이프(|) 구분자가 있는 표 행인지 판별한다."""
    stripped = line.strip()
    return stripped.count("|") >= 2 or (stripped.count("|") >= 1 and len(stripped) < 50)


def _is_narrative_line(line: str) -> bool:
    """서술형 문장인지 판별한다 — 동사/서술어로 끝나거나 충분히 긴 줄."""
    stripped = line.strip()
    if len(stripped) < 10:
        return False
    # 파이프 행 제외
    if _is_table_row(stripped):
        return False
    # 서술어 어미로 끝나는 문장
    narrative_endings = (
        "합니다", "됩니다", "있습니다", "없습니다", "입니다", "습니다",
        "합니다.", "됩니다.", "있습니다.", "없습니다.", "입니다.", "습니다.",
        "한다", "된다", "있다", "없다", "이다",
        "한다.", "된다.", "있다.", "없다.", "이다.",
        "하였습니다", "되었습니다", "예정입니다", "해당합니다", "우려됩니다",
    )
    if any(stripped.endswith(e) for e in narrative_endings):
        return True
    # 번호 매김 라벨(가., 나., 1., 2. 등)은 제목이라 서술형이 아님
    import re
    if re.match(r"^[가-힣]\.", stripped) or re.match(r"^\d+\.", stripped):
        return False
    # 20자 이상이고 한글이 포함된 줄은 서술형일 가능성이 높음
    korean_chars = sum(1 for c in stripped if "가" <= c <= "힣")
    return len(stripped) >= 20 and korean_chars >= 5


def _filter_to_narrative(text: str) -> str:
    """
    텍스트에서 서술형 문장만 추출한다.
    파이프 구분자 표 행은 제외하고, 서술형 줄과 섹션 제목만 남긴다.
    서술형 줄이 하나도 없으면 원문을 그대로 반환(Gemini가 맥락을 잃지 않도록).
    """
    lines = text.split("\n")
    narrative_lines = []
    section_labels = []  # 가., 나., 1. 같은 섹션 제목도 맥락 제공용으로 유지

    import re
    for line in lines:
        stripped = line.strip()
        if not stripped:
            narrative_lines.append("")
            continue
        if re.match(r"^[가-힣]\.", stripped) or re.match(r"^\d+\.", stripped):
            section_labels.append(stripped)
            narrative_lines.append(line)
        elif _is_narrative_line(stripped):
            narrative_lines.append(line)
        elif stripped.startswith("※") or stripped.startswith("*") or stripped.startswith("\*\*"):
            # 주석/각주도 서술형 맥락 제공
            if len(stripped) >= 15:
                narrative_lines.append(line)

    result_lines = [l for l in narrative_lines if l is not None]
    result = "\n".join(result_lines).strip()

    if not result or len(result) < 20:
        # 서술형이 없으면 원문 그대로 반환
        return text.strip()

    return result


def _find_sentence_containing(text: str, keyword: str) -> str:
    for sentence in _SENTENCE_SPLIT_PATTERN.split(text):
        if keyword in sentence and len(sentence) <= 300:
            return sentence.strip()
    return ""


@dataclass
class ExtractedChunk:
    text: str
    start_offset: int


def extract_analysis_chunks(full_text: str) -> dict[str, ExtractedChunk]:
    result: dict[str, ExtractedChunk] = {}
    for item_code, labels in ITEM_LABELS.items():
        start = -1
        for label in labels:
            idx = full_text.find(label)
            if idx != -1 and (start == -1 or idx < start):
                start = idx
        if start == -1:
            continue
        end = min(start + MAX_ITEM_LENGTH, len(full_text))
        result[item_code] = ExtractedChunk(text=full_text[start:end], start_offset=start)
    return result


def build_analysis_input(chunks: dict[str, ExtractedChunk]) -> str:
    parts = []
    for item_code, chunk in chunks.items():
        parts.append(f"=== {item_code} ===\n{_filter_to_narrative(chunk.text)}\n")
    return "\n".join(parts)


def resolve_offset(chunks: dict[str, ExtractedChunk], target_text: str) -> int:
    for chunk in chunks.values():
        local_idx = chunk.text.find(target_text)
        if local_idx != -1:
            return chunk.start_offset + local_idx
    return -1


# =====================================================================
# 요약 프롬프트
# =====================================================================

SUMMARY_SYSTEM_INSTRUCTION = (
    "당신은 한국거래소 상장관리 및 시장조치를 전문적으로 분석하는 애널리스트입니다. "
    "불성실공시·관리종목·매매거래정지 등 거래소의 제재·조치 공시를 읽고, "
    "일반 투자자가 10초 만에 이 조치가 거래 가능성과 상장유지에 미칠 영향을 "
    "이해할 수 있도록 직관적으로 요약해야 합니다. "
    "마크다운 기호나 부가 설명 없이 오직 지정된 JSON 객체 형식으로만 응답하십시오."
)

SUMMARY_USER_PROMPT_TEMPLATE = (
    "다음은 [{corp_name}]에 대한 거래소공시 핵심 항목(조치유형, 사유, 기간)을 발췌한 "
    "원문입니다.\n\n"
    "[요약 미션]\n"
    "제시된 원문을 바탕으로 이 거래소 조치가 투자자에게 어떤 의미인지 총평하는 "
    "요약본을 작성하십시오.\n\n"
    "[Output Guidelines]\n"
    "1. summary_text: 조치 유형과 사유를 1문장(50자 이내)으로 압축하십시오.\n"
    "2. investor_comment: 이 조치가 거래 가능성과 상장유지 여부에 미칠 영향을 "
    "3문장 이내로 해설하십시오.\n"
    "3. overall_risk: 매매거래정지·상장적격성 실질심사·고의적 공시번복이 포함되면 "
    "반드시 'Critical'을 부여하십시오. 경미한 불성실공시 벌점 부과 수준이면 'High'나 "
    "'Neutral'을 부여하십시오.\n\n"
    "[JSON Schema]\n"
    "{{\n"
    '  "summary_text": "(String) 1문장 핵심 요약",\n'
    '  "investor_comment": "(String) 3문장 이내 투자자 해설",\n'
    '  "overall_risk": "(Enum: [Low, Neutral, High, Critical])"\n'
    "}}\n\n"
    "[거래소공시 발췌 데이터]\n{context}"
)

# =====================================================================
# 분석 프롬프트
# =====================================================================

ANALYSIS_SYSTEM_INSTRUCTION = (
    "너는 한국거래소 상장관리 규정 전문가다. 거래소공시(불성실공시·관리종목·매매거래정지 등)를 "
    "분석하여, 조치의 심각도와 상장유지 가능성에 미치는 영향을 평가하라."
)

ANALYSIS_USER_PROMPT_TEMPLATE = (
    "[Mission]\n"
    "제공된 DART '거래소공시' 발췌 구획을 분석하여, "
    "1) 조치의 성격과 심각도(Action_Severity), 2) 거래 가능성에 대한 영향(Trading_Impact), "
    "3) 상장유지 리스크(Listing_Risk)를 식별하고 JSON 배열로 반환하라.\n\n"
    "[Output Guidelines]\n"
    "1. 매매거래정지나 상장적격성 실질심사 대상이면 'Listing_Risk'에서 반드시 'Critical'로 "
    "분류하라.\n"
    "2. targetKey는 반드시 투자 판단의 근거가 되는 서술형 문장이어야 한다. 표의 항목명·레이블·단어 하나(예: \"장내매도(-)\" \"적정\" \"보유목적\" \"주주배정\" 등)는 targetKey로 절대 사용 금지. 원문 발췌 구획에서 정확히 일치하는 문자열만 사용하라(재구성·요약 금지 — 한 글자도 다르면 안 됨).\n"
    "3. materialImpact는 결론부터 쉬운 말로 최대 2문장으로 써라. 전문용어를 쓸 경우 "
    "괄호로 쉬운 설명을 바로 덧붙이고, 배경 설명이나 원문 재진술로 늘리지 마라.\n"
    "4. 결과는 반드시 아래 구조의 순수한 JSON 배열 형태로만 출력하고, "
    "마크다운 기호나 추가 텍스트를 절대 붙이지 말 것.\n\n"
    "[JSON Schema]\n"
    "[\n"
    "  {{\n"
    '    "analysisCategory": "(Enum: [Action_Severity, Trading_Impact, Listing_Risk])",\n'
    '    "targetKey": "(String) 반드시 아래 조건을 모두 충족하는 완전한 서술형 문장\n      · 최소 20자 이상, 주어+서술어 구조의 완전한 문장\n      · 파이프(|)가 포함된 표 행 사용 절대 금지\n      · 표 항목명·레이블·단어 단독 금지 (예: \"장내매도(-)\" \"적정\" \"보유목적\" \"주주배정\" \"경영참가\")\n      · \"~합니다\" \"~됩니다\" \"~있습니다\" \"~입니다\"처럼 서술어로 끝나야 함\n      · 원문에서 한 글자도 수정 없이 그대로 발췌",\n'
    '    "materialImpact": "(String) 결론부터 쉬운 말로, 최대 2문장",\n'
    '    "riskLevel": "(Enum: [Low, Neutral, High, Critical])"\n'
    "  }}\n"
    "]\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)
