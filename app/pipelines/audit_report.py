"""
외부감사관련(AUDIT, 감사보고서·연결감사보고서) 압축 + 요약/분석 프롬프트 모듈.

이 대분류는 "감사의견" 한 단어가 다른 어떤 항목보다 결정적인 위험 신호다.
적정의견이 아니면(한정/부적정/의견거절) 그 자체로 거래정지·상장폐지 사유가 될 수 있어,
다른 모듈과 달리 감사의견을 가장 먼저, 가장 우선해서 추출한다.
"""
import re
from dataclasses import dataclass

ITEM_LABELS: dict[str, list[str]] = {
    "AU1_Audit_Opinion": ["감사의견", "적정", "한정", "부적정", "의견거절"],
    "AU2_Emphasis_Matter": ["강조사항", "계속기업 관련 중요한 불확실성", "강조사항문단"],
    "AU3_Key_Audit_Matters": ["핵심감사사항", "핵심감사사항으로 결정한 사항"],
    "AU4_Internal_Control": ["내부회계관리제도", "내부회계관리제도 검토의견", "중요한 취약점"],
}

MAX_ITEM_LENGTH = 500
TOTAL_MAX = 2000

CRITICAL_KEYWORDS = [
    "한정의견", "부적정의견", "의견거절", "계속기업 관련 중요한 불확실성",
    "중요한 취약점", "감사범위 제한", "회계처리기준 위반",
]

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?다])\s+")


def compress_for_summary(full_text: str) -> str:
    if not full_text or not full_text.strip():
        return ""

    # 감사의견 자체를 가장 먼저 별도로 추출해 최상단에 고정 — 다른 정보가 길어져도
    # 압축 결과에서 누락되지 않게 한다.
    opinion_sentence = _find_audit_opinion_sentence(full_text)

    parts: list[str] = []
    if opinion_sentence:
        parts.append(f"[감사의견]\n{opinion_sentence}")

    for item_code, labels in ITEM_LABELS.items():
        if item_code == "AU1_Audit_Opinion":
            continue  # 이미 위에서 별도 처리
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


def _find_audit_opinion_sentence(text: str) -> str:
    opinion_keywords = ["적정", "한정", "부적정", "의견거절"]
    for sentence in _SENTENCE_SPLIT_PATTERN.split(text):
        t = sentence.strip()
        if "감사의견" in t or any(f"{k}의견" in t for k in opinion_keywords):
            return t[:300]
    return ""


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
        elif stripped.startswith("※") or stripped.startswith("*") or stripped.startswith("**"):
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
    "당신은 국내 회계감사 및 재무제표 분석 전문 애널리스트입니다. "
    "상장사의 감사보고서를 읽고, 일반 투자자가 10초 만에 감사의견의 중요성과 "
    "재무제표 신뢰성을 이해할 수 있도록 직관적으로 요약해야 합니다. "
    "마크다운 기호나 부가 설명 없이 오직 지정된 JSON 객체 형식으로만 응답하십시오."
)

SUMMARY_USER_PROMPT_TEMPLATE = (
    "다음은 [{corp_name}]의 감사보고서 핵심 항목(감사의견, 강조사항, 핵심감사사항)을 "
    "발췌한 원문입니다.\n\n"
    "[요약 미션]\n"
    "제시된 원문을 바탕으로 이 감사보고서가 재무제표 신뢰성에 대해 무엇을 말하는지 "
    "총평하는 요약본을 작성하십시오.\n\n"
    "[Output Guidelines]\n"
    "1. summary_text: 감사의견과 핵심 강조사항 유무를 1문장(50자 이내)으로 압축하십시오.\n"
    "2. investor_comment: 감사의견이 적정이 아닌 이유, 또는 강조사항·핵심감사사항이 "
    "의미하는 바를 3문장 이내로 해설하십시오.\n"
    "3. overall_risk: 감사의견이 한정/부적정/의견거절이거나 계속기업 불확실성이 "
    "언급되면 반드시 'Critical'을 부여하십시오. 적정의견이고 특이사항이 없으면 'Low'를 부여하십시오.\n\n"
    "[JSON Schema]\n"
    "{{\n"
    '  "summary_text": "(String) 1문장 핵심 요약",\n'
    '  "investor_comment": "(String) 3문장 이내 투자자 해설",\n'
    '  "overall_risk": "(Enum: [Low, Neutral, High, Critical])"\n'
    "}}\n\n"
    "[감사보고서 발췌 데이터]\n{context}"
)

# =====================================================================
# 분석 프롬프트
# =====================================================================

ANALYSIS_SYSTEM_INSTRUCTION = (
    "너는 공인회계사 출신 재무제표 분석 전문가다. 상장사의 '감사보고서'를 분석하여, "
    "감사의견의 함의, 강조사항·핵심감사사항이 시사하는 리스크, 내부통제의 신뢰성을 평가하라."
)

ANALYSIS_USER_PROMPT_TEMPLATE = (
    "[Mission]\n"
    "제공된 DART '감사보고서' 발췌 구획을 분석하여, "
    "1) 감사의견의 함의(Audit_Opinion), 2) 강조사항·핵심감사사항이 시사하는 리스크(Key_Audit_Risk), "
    "3) 내부통제 신뢰성(Internal_Control)을 식별하고 JSON 배열로 반환하라.\n\n"
    "[Output Guidelines]\n"
    "1. 감사의견이 적정이 아니거나 계속기업 관련 중요한 불확실성이 언급되면 "
    "'Audit_Opinion' 카테고리에서 반드시 'Critical'로 분류하라.\n"
    "2. 내부회계관리제도에 중요한 취약점이 발견되면 'Internal_Control'에서 'High' 이상으로 분류하라.\n"
    "3. targetKey는 반드시 투자 판단의 근거가 되는 완전한 서술형 문장이어야 한다.\n"
    "   · 파이프(|) 구분자가 포함된 표 행 절대 금지\n"
    "   · 표 항목명·레이블·단어 단독 금지 (장내매도, 적정, 보유목적, 주주배정 등)\n"
    "   · 최소 20자 이상의 완전한 문장(서술어로 끝나는 문장)만 허용\n"
    "   · 원문에서 한 글자도 수정 없이 그대로 발췌.\n"
    "4. 결과는 반드시 아래 구조의 순수한 JSON 배열 형태로만 출력하고, "
    "마크다운 기호나 추가 텍스트를 절대 붙이지 말 것.\n\n"
    "[JSON Schema]\n"
    "[\n"
    "  {{\n"
    '    "analysisCategory": "(Enum: [Audit_Opinion, Key_Audit_Risk, Internal_Control])",\n'
    "   · 원문에서 한 글자도 수정 없이 그대로 발췌.\n"
    '    "materialImpact": "(String) 해당 내용이 재무제표 신뢰성에 미칠 영향 분석 (3문장 이내)",\n'
    '    "riskLevel": "(Enum: [Low, Neutral, High, Critical])"\n'
    "  }}\n"
    "]\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)
