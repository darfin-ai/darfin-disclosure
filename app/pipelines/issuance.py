"""
발행공시(ISSUANCE, 증권신고서·투자설명서) 압축 + 요약/분석 프롬프트 모듈.

유상증자결정(RIGHTS_OFFERING)이 "이사회가 증자를 결정했다"는 짧은 공시인 것과 달리,
증권신고서·투자설명서는 모집/매출의 구체적 조건과 위험요소를 전부 서술하는 긴 문서다
(사업보고서에 준하는 분량). 그래서 business_report.py처럼 섹션 단위로 자르고
재배열하는 방식을 쓰되, 섹션 구성은 발행공시 표준 목차에 맞춘다.
"""
import re
from dataclasses import dataclass

MAX_SEC1_SUMMARY = 500       # I. 모집/매출 개요
MAX_SEC2_TERMS = 500         # II. 증권의 권리내용 / 공모가격 산정
MAX_SEC3_RISK_FRONT = 1000   # III. 투자위험요소 앞부분
MAX_SEC3_RISK_SENTENCES = 400
TOTAL_MAX = 2600

SEC1_START = ["1. 모집 또는 매출에 관한 일반사항", "모집가액", "모집(매출)총액", "I. 모집 또는 매출에 관한 사항"]
SEC2_START = ["2. 증권의 권리내용", "공모가격 산정", "수요예측", "II. 증권의 권리내용"]
SEC3_START = ["투자위험요소", "투자위험 요소", "III. 투자위험요소", "사업위험", "회사위험", "기타위험"]
SEC4_START = ["IV.", "자금의 사용목적", "4. 자금의 사용목적"]

CRITICAL_KEYWORDS = [
    "상장폐지", "관리종목", "감사의견 비적정", "계속기업 불확실성",
    "최대주주 변경", "경영권 분쟁", "소송 계류", "대규모 손실",
    "환매청구권", "조기상환", "원금손실 가능성",
]

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?다])\s+")


def compress_for_summary(full_text: str) -> str:
    if not full_text or not full_text.strip():
        return ""

    critical_sentences = _extract_critical_sentences(full_text)

    sec1 = _extract_section(full_text, SEC1_START, SEC2_START)
    sec2 = _extract_section(full_text, SEC2_START, SEC3_START)
    sec3 = _extract_section(full_text, SEC3_START, SEC4_START)

    if not sec1 and not sec2 and not sec3:
        return _compress_fallback(full_text, critical_sentences)

    parts = []
    if sec1:
        parts.append(f"[모집/매출 개요]\n{sec1[:MAX_SEC1_SUMMARY]}")
    if sec2:
        parts.append(f"[증권 조건]\n{sec2[:MAX_SEC2_TERMS]}")
    if sec3:
        front = sec3[:MAX_SEC3_RISK_FRONT]
        risk_sentences = _extract_risk_sentences(sec3, front)
        risk_block = f"[투자위험요소]\n{front}"
        if risk_sentences:
            risk_block += "\n[위험 보강]\n" + " ".join(risk_sentences)
        parts.append(risk_block)

    result = "\n\n".join(parts).strip()
    result = _append_missing_critical_sentences(result, critical_sentences)
    return result[:TOTAL_MAX]


def _extract_section(text: str, start_keys: list[str], end_keys: list[str]) -> str:
    start = _find_first_index(text, start_keys)
    if start == -1:
        return ""
    end = len(text)
    found_end = None
    for key in end_keys:
        idx = text.find(key, start + 1)
        if idx != -1 and (found_end is None or idx < found_end):
            found_end = idx
    if found_end is not None:
        end = found_end
    return text[start:end]


def _find_first_index(text: str, keys: list[str]) -> int:
    earliest = -1
    for key in keys:
        idx = text.find(key)
        if idx != -1 and (earliest == -1 or idx < earliest):
            earliest = idx
    return earliest


def _extract_risk_sentences(full_text: str, already_included: str) -> list[str]:
    risk_keywords = ["위험", "손실가능성", "불확실", "변동성", "하락", "감소"]
    result: list[str] = []
    total_len = 0
    for sentence in _SENTENCE_SPLIT_PATTERN.split(full_text):
        t = sentence.strip()
        if len(t) < 15 or len(t) > 200:
            continue
        if t[: min(20, len(t))] in already_included:
            continue
        if any(k in t for k in risk_keywords):
            result.append(t)
            total_len += len(t)
            if len(result) >= 3 or total_len >= MAX_SEC3_RISK_SENTENCES:
                break
    return result


def _extract_critical_sentences(text: str) -> list[str]:
    result = []
    for sentence in _SENTENCE_SPLIT_PATTERN.split(text):
        t = sentence.strip()
        for keyword in CRITICAL_KEYWORDS:
            if keyword in t and len(t) <= 300:
                result.append(t)
                break
    return result


def _append_missing_critical_sentences(result: str, criticals: list[str]) -> str:
    if not criticals:
        return result
    missing = [s for s in criticals if s[: min(20, len(s))] not in result]
    if not missing:
        return result
    return result + "\n[중요 공시 사항]\n" + " ".join(missing)


def _compress_fallback(text: str, criticals: list[str]) -> str:
    parts = []
    total_len = 0
    for sentence in _SENTENCE_SPLIT_PATTERN.split(text):
        t = sentence.strip()
        if not t or len(t) > 200:
            continue
        if re.search(r"\d+", t):
            parts.append(t)
            total_len += len(t)
            if total_len >= TOTAL_MAX:
                break
    result = " ".join(parts).strip()
    return _append_missing_critical_sentences(result, criticals)


@dataclass
class ExtractedChunk:
    text: str
    start_offset: int


# 분석은 좌표보존이 필요하므로 섹션 시작 위치를 그대로 청크 경계로 쓴다(재배열 없음).
ANALYSIS_TARGET_HEADINGS: dict[str, list[str]] = {
    "IS1_Offering_Terms": SEC1_START + SEC2_START,
    "IS2_Investment_Risk": SEC3_START,
    "IS3_Use_Of_Proceeds": ["자금의 사용목적", "조달자금의 사용계획"],
}

MAX_CHUNK_LENGTH = 2500
_NEXT_HEADING_PATTERN = re.compile(r"\n\s*([0-9]+\.|[가-하]\.)\s*[가-힣]")


def extract_analysis_chunks(full_text: str) -> dict[str, ExtractedChunk]:
    result: dict[str, ExtractedChunk] = {}
    for code, headings in ANALYSIS_TARGET_HEADINGS.items():
        start = _find_first_index(full_text, headings)
        if start == -1:
            continue
        end = len(full_text)
        match = _NEXT_HEADING_PATTERN.search(full_text, start + 10)
        if match:
            end = match.start()
        if end - start > MAX_CHUNK_LENGTH:
            end = start + MAX_CHUNK_LENGTH
        result[code] = ExtractedChunk(text=full_text[start:end], start_offset=start)
    return result


def build_analysis_input(chunks: dict[str, ExtractedChunk]) -> str:
    parts = []
    for code, chunk in chunks.items():
        parts.append(f"=== {code} ===\n{chunk.text}\n")
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
    "당신은 국내 자본시장에서 증권신고서·투자설명서를 전문적으로 분석하는 애널리스트입니다. "
    "공모/사모 발행 조건과 투자위험요소를 읽고, 일반 투자자가 10초 만에 이 증권 발행에 "
    "참여할지 판단할 핵심 정보를 이해할 수 있도록 요약해야 합니다. "
    "마크다운 기호나 부가 설명 없이 오직 지정된 JSON 객체 형식으로만 응답하십시오."
)

SUMMARY_USER_PROMPT_TEMPLATE = (
    "다음은 [{corp_name}]의 증권신고서/투자설명서 핵심 섹션(모집개요, 증권조건, 투자위험요소)을 "
    "발췌한 원문입니다.\n\n"
    "[요약 미션]\n"
    "제시된 원문을 바탕으로 이 증권 발행의 조건과 핵심 위험을 총평하는 요약본을 작성하십시오.\n\n"
    "[Output Guidelines]\n"
    "1. summary_text: 발행 규모와 자금사용목적을 1문장(50자 이내)으로 압축하십시오.\n"
    "2. investor_comment: 공모가 산정 근거와 투자위험요소 중 가장 중요한 1~2가지를 "
    "3문장 이내로 해설하십시오.\n"
    "3. overall_risk: 상장폐지·계속기업 불확실성·원금손실 가능성 등 치명적 위험요소가 "
    "있으면 'Critical' 또는 'High'를, 통상적인 발행 조건이면 'Low'나 'Neutral'을 부여하십시오.\n\n"
    "[JSON Schema]\n"
    "{{\n"
    '  "summary_text": "(String) 1문장 핵심 요약",\n'
    '  "investor_comment": "(String) 3문장 이내 투자자 해설",\n'
    '  "overall_risk": "(Enum: [Low, Neutral, High, Critical])"\n'
    "}}\n\n"
    "[발행공시 발췌 데이터]\n{context}"
)

# =====================================================================
# 분석 프롬프트
# =====================================================================

ANALYSIS_SYSTEM_INSTRUCTION = (
    "너는 증권 발행시장(IPO·유상증자·회사채 등) 전문 애널리스트다. 상장사의 발행공시를 분석하여, "
    "발행조건의 적정성, 투자위험요소의 심각도, 자금사용목적의 타당성을 평가하라."
)

ANALYSIS_USER_PROMPT_TEMPLATE = (
    "[Mission]\n"
    "제공된 DART '발행공시' 발췌 구획을 분석하여, "
    "1) 발행조건의 적정성(Offering_Terms), 2) 투자위험요소(Investment_Risk), "
    "3) 자금사용목적의 타당성(Use_Of_Proceeds)을 식별하고 JSON 배열로 반환하라.\n\n"
    "[Output Guidelines]\n"
    "1. 계속기업 불확실성이나 상장폐지 사유가 언급되면 'Investment_Risk' 카테고리에서 "
    "반드시 'Critical'로 분류하라.\n"
    "2. targetKey는 반드시 투자 판단의 근거가 되는 서술형 문장이어야 한다. 표의 항목명·레이블·단어 하나(예: \"장내매도(-)\" \"적정\" \"보유목적\" \"주주배정\" 등)는 targetKey로 절대 사용 금지. 원문 발췌 구획에서 정확히 일치하는 문자열만 사용하라(재구성·요약 금지 — 한 글자도 다르면 안 됨).\n"
    "3. 결과는 반드시 아래 구조의 순수한 JSON 배열 형태로만 출력하고, "
    "마크다운 기호나 추가 텍스트를 절대 붙이지 말 것.\n\n"
    "[JSON Schema]\n"
    "[\n"
    "  {{\n"
    '    "analysisCategory": "(Enum: [Offering_Terms, Investment_Risk, Use_Of_Proceeds])",\n'
    '    "targetKey": "(String) 반드시 아래 조건을 모두 충족하는 완전한 서술형 문장\n      · 최소 20자 이상, 주어+서술어 구조의 완전한 문장\n      · 파이프(|)가 포함된 표 행 사용 절대 금지\n      · 표 항목명·레이블·단어 단독 금지 (예: \"장내매도(-)\" \"적정\" \"보유목적\" \"주주배정\" \"경영참가\")\n      · \"~합니다\" \"~됩니다\" \"~있습니다\" \"~입니다\"처럼 서술어로 끝나야 함\n      · 원문에서 한 글자도 수정 없이 그대로 발췌",\n'
    '    "materialImpact": "(String) 해당 조건이 투자판단에 미칠 영향 분석 (3문장 이내)",\n'
    '    "riskLevel": "(Enum: [Low, Neutral, High, Critical])"\n'
    "  }}\n"
    "]\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)
