"""
유상증자결정(RIGHTS_OFFERING, 주요사항보고서) 압축 + 요약/분석 프롬프트 모듈.

사업보고서(business_report.py)와는 완전히 다른 문서 구조를 갖는다 — 유상증자결정은
짧고 정형화된 공시라서 항목별(발행가액, 발행방식, 자금사용목적 등) 표 형태 추출이 핵심이며,
사업보고서처럼 긴 섹션을 자르고 재배열하는 과정이 필요 없다.

이 모듈은 registry.py가 요구하는 같은 인터페이스
(compress_for_summary, extract_analysis_chunks, build_analysis_input, resolve_offset,
*_SYSTEM_INSTRUCTION, *_USER_PROMPT_TEMPLATE)를 구현한다.
"""
import re
from dataclasses import dataclass

# ── 핵심 항목별 라벨(DART 표준 양식의 표 항목명) ──────────────────
ITEM_LABELS: dict[str, list[str]] = {
    "RO1_Issue_Type": ["신주의 종류와 수", "증자방식"],
    "RO2_Issue_Price": ["1주당 발행가액", "신주 발행가액"],
    "RO3_Funding_Purpose": ["자금조달의 목적", "시설자금", "운영자금", "타법인 증권 취득자금", "채무상환자금"],
    "RO4_Allotment_Method": ["주주배정", "제3자배정", "일반공모", "신주배정기준일"],
    "RO5_Major_Shareholder": ["최대주주", "특수관계인", "신주배정 후 지분율"],
}

# 항목당 최대 길이(자) — 유상증자결정은 표가 짧으므로 사업보고서보다 훨씬 작게 설정
MAX_ITEM_LENGTH = 500
TOTAL_MAX = 2000

# 절대 보존 위험 키워드 — 유상증자결정 특유의 위험 신호
CRITICAL_KEYWORDS = [
    "제3자배정", "최대주주 변경", "경영권 변동", "특수관계인 배정",
    "전환가액 조정", "리픽싱", "대규모 희석", "지분율 급감",
]

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?다])\s+")


def compress_for_summary(full_text: str) -> str:
    """
    요약용 압축. 항목별 라벨이 등장하는 행(또는 그 주변 문장)을 추출해서 합친다.
    사업보고서처럘 긴 섹션을 자르는 게 아니라, 표 형태 항목을 그대로 모으는 방식이다.
    """
    if not full_text or not full_text.strip():
        return ""

    parts: list[str] = []
    for item_code, labels in ITEM_LABELS.items():
        chunk = _extract_item_chunk(full_text, labels, MAX_ITEM_LENGTH)
        if chunk:
            parts.append(f"[{item_code}]\n{chunk}")

    result = "\n\n".join(parts)

    # 위험 키워드는 항목 추출에서 빠졌더라도 절대 보존
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
    """분석용 좌표보존 압축. 항목별 라벨 위치 그대로(재배열 없이) 청크를 추출한다."""
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
        parts.append(f"=== {item_code} ===\n{chunk.text}\n")
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
    "당신은 국내 자본시장에서 유상증자 공시를 전문적으로 분석하는 애널리스트입니다. "
    "유상증자결정 공시의 핵심 조건(발행가액, 조달목적, 배정방식)을 읽고, "
    "일반 투자자가 10초 만에 이 증자가 자신의 지분에 미칠 영향을 이해할 수 있도록 "
    "직관적으로 요약해야 합니다. "
    "마크다운 기호나 부가 설명 없이 오직 지정된 JSON 객체 형식으로만 응답하십시오."
)

SUMMARY_USER_PROMPT_TEMPLATE = (
    "다음은 [{corp_name}]의 유상증자결정 공시 핵심 항목을 발췌한 원문입니다.\n\n"
    "[요약 미션]\n"
    "제시된 원문을 바탕으로 이 유상증자가 기존 주주에게 어떤 의미인지 총평하는 요약본을 작성하십시오.\n\n"
    "[Output Guidelines]\n"
    "1. summary_text: 발행규모와 조달목적을 1문장(50자 이내)으로 압축하십시오.\n"
    "2. investor_comment: 배정방식(주주배정/제3자배정)에 따른 희석 영향과 "
    "조달자금의 용도가 기업가치에 미칠 영향을 3문장 이내로 해설하십시오.\n"
    "3. overall_risk: 제3자배정으로 최대주주가 변경되거나 리픽싱 조항이 있으면 'Critical'을, "
    "기존주주 지분 희석이 상당하면 'High'를, 주주배정이고 희석이 경미하면 'Low'나 'Neutral'을 부여하십시오.\n\n"
    "[JSON Schema]\n"
    "{{\n"
    '  "summary_text": "(String) 1문장 핵심 요약",\n'
    '  "investor_comment": "(String) 3문장 이내 투자자 해설",\n'
    '  "overall_risk": "(Enum: [Low, Neutral, High, Critical])"\n'
    "}}\n\n"
    "[유상증자결정 발췌 데이터]\n{context}"
)

# =====================================================================
# 분석 프롬프트
# =====================================================================

ANALYSIS_SYSTEM_INSTRUCTION = (
    "너는 자본시장법 및 기업 재무 전문 애널리스트다. 상장사의 '유상증자결정' 공시를 분석하여, "
    "지분 희석 위험, 자금조달 목적의 타당성, 발행가액의 공정성을 평가하라."
)

ANALYSIS_USER_PROMPT_TEMPLATE = (
    "[Mission]\n"
    "제공된 DART '유상증자결정' 발췌 구획을 분석하여, "
    "1) 지분 희석 위험(Dilution Risk), 2) 자금조달 목적의 타당성(Funding Purpose), "
    "3) 발행가액의 공정성(Pricing Fairness), 4) 최대주주 지분 영향(Major Shareholder Impact)을 "
    "식별하고 JSON 배열로 반환하라.\n\n"
    "[Output Guidelines]\n"
    "1. 제3자배정이고 최대주주가 변경되면 반드시 'Critical' 등급으로 분류하라.\n"
    "2. 리픽싱(전환가액 조정) 조항이 있으면 'Dilution Risk' 카테고리에서 'High' 이상으로 분류하라.\n"
    "3. targetKey는 반드시 투자 판단의 근거가 되는 완전한 서술형 문장이어야 한다.\n"
    "   · 파이프(|) 구분자가 포함된 표 행 절대 금지\n"
    "   · 표 항목명·레이블·단어 단독 금지 (장내매도, 적정, 보유목적, 주주배정 등)\n"
    "   · 최소 20자 이상의 완전한 문장(서술어로 끝나는 문장)만 허용\n"
    "   · 원문에서 한 글자도 수정 없이 그대로 발췌.\n"
    "4. materialImpact는 결론부터 쉬운 말로 최대 2문장으로 써라. 전문용어를 쓸 경우 "
    "괄호로 쉬운 설명을 바로 덧붙이고, 배경 설명이나 원문 재진술로 늘리지 마라.\n"
    "5. 결과는 반드시 아래 구조의 순수한 JSON 배열 형태로만 출력하고, "
    "마크다운 기호나 추가 텍스트를 절대 붙이지 말 것.\n\n"
    "[JSON Schema]\n"
    "[\n"
    "  {{\n"
    '    "analysisCategory": "(Enum: [Dilution_Risk, Funding_Purpose, Pricing_Fairness, Major_Shareholder_Impact])",\n'
    "   · 원문에서 한 글자도 수정 없이 그대로 발췌.\n"
    '    "materialImpact": "(String) 결론부터 쉬운 말로, 최대 2문장",\n'
    '    "riskLevel": "(Enum: [Low, Neutral, High, Critical])"\n'
    "  }}\n"
    "]\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)
