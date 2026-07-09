"""
사업보고서(BIZ_REPORT, 정기공시) 압축 + 요약/분석 프롬프트 모듈.

DART에서 가져오는 문서는 사업보고서 하나가 아니라 90개+ 유형이 있으므로,
이 모듈은 그중 "사업보고서" 한 유형만 담당한다. registry.py가 type_code="BIZ_REPORT"를
이 모듈에 연결한다. 다른 유형(유상증자결정 등)은 rights_offering.py처럼 같은
인터페이스(compress_for_summary, extract_analysis_chunks, build_analysis_input,
resolve_offset, *_SYSTEM_INSTRUCTION, *_USER_PROMPT_TEMPLATE)를 갖는 별도 모듈로 추가한다.

요약 압축: Java AnnualReportContextCompressor 포팅 (텍스트를 자르고 재배열, 위험키워드 절대보존)
분석 압축: Java AnalysisSectionFilter 포팅 (좌표 보존, 소제목 단위로만 포함/제외)
"""
import re
from dataclasses import dataclass

# ── 섹션별 최대 길이(자) ──────────────────────────────────────────
MAX_SEC2_INTRO = 400          # II. 사업내용 도입부
MAX_SEC2_REVENUE = 300        # II. 매출비중 구간
MAX_SEC3_FINANCE = 400        # III. 재무 핵심 수치
MAX_SEC4_MDA_FRONT = 1200     # IV. MD&A 앞부분
MAX_SEC4_RISK_SENTENCES = 300  # IV. 위험 키워드 문장
TOTAL_MAX = 2600             # 전체 최대(약 1,700토큰)

# ── 섹션 구분 키워드 ──────────────────────────────────────────────
SEC2_START = ["II. 사업의 내용", "2. 사업의 내용", "사업의 내용", "II.사업의내용", "제2장"]
SEC3_START = ["III. 재무에 관한 사항", "3. 재무에 관한 사항", "재무에 관한 사항", "III.재무에관한사항", "제3장"]
SEC4_START = ["IV. 이사의 경영진단", "4. 이사의 경영진단", "이사의 경영진단", "경영진단 및 분석의견", "경영진단및분석의견", "MD&A", "제4장"]
SEC5_START = ["V.", "5. 감사보고서", "회계감사인", "감사의견", "제5장"]

# ── 재무 핵심 계정 키워드(손익계산서) ─────────────────────────────
FINANCE_KEYS = ["매출액", "영업수익", "영업이익", "영업손실", "당기순이익", "당기순손실", "영업이익률", "부채비율", "영업활동현금흐름"]

# ── 매출 비중 패턴 ────────────────────────────────────────────────
# "DS부문 36.9%" / "반도체 : 110,000억원 (45%)" 등
REVENUE_PCT_PATTERN = re.compile(r"[가-힣a-zA-Z0-9·\s]{1,20}[\s:：]{0,3}\d+[,\d]*\s*(억|조|원|%)")

# ── 절대 보존 위험 키워드 ─────────────────────────────────────────
# 이 키워드가 포함된 문장은 위치·길이 무관 무조건 포함
CRITICAL_KEYWORDS = [
    "자본잠식", "완전자본잠식", "상장폐지", "상장폐지사유",
    "영업정지", "횡령", "배임", "분식회계",
    "계속기업", "계속기업불확실성", "계속기업 의문",
    "감사의견 한정", "감사의견 부적정", "의견거절",
    "부도", "워크아웃", "기업회생", "법정관리",
    "대규모 손실", "대규모 적자", "자금난", "유동성 위기",
    "현금흐름 악화", "현금부족",
]

# ── 제거 대상 상투 문구 패턴 ──────────────────────────────────────
BOILERPLATE_PATTERNS = [
    "본 보고서는 자본시장과 금융투자업에 관한 법률",
    "이 보고서를 읽으시기 전에",
    "동 보고서에 기재된 내용 중",
    "미래에 대한 기술은 전망이나 예측",
    "상기 내용은 투자판단의 참고",
    "※ 상기", "* 상기", "주) ", "주1)", "주2)", "주3)",
    "이하 여백", "이 하 여 백", "이 페이지는 의도적으로",
]

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?다])\s+")
_PAGE_NUMBER_PATTERN_1 = re.compile(r"^\s*[-–]\s*\d+\s*[-–]\s*$", re.MULTILINE)
_PAGE_NUMBER_PATTERN_2 = re.compile(r"^\s*\d+\s*/\s*\d+\s*$", re.MULTILINE)


def compress(raw_context: str) -> str:
    """메인 압축 함수. raw_context(백엔드에서 합친 3섹션 원문 전체) -> 압축된 컨텍스트(최대 2,600자)."""
    if not raw_context or not raw_context.strip():
        return ""

    original_len = len(raw_context)

    # 1단계: 전처리
    cleaned = _preprocess(raw_context)

    # 2단계: 절대 보존 위험 문장 먼저 추출
    critical_sentences = _extract_critical_sentences(cleaned)

    # 3단계: 섹션 분리
    sec2 = _extract_section(cleaned, SEC2_START, SEC3_START)
    sec3 = _extract_section(cleaned, SEC3_START, SEC4_START)
    sec4 = _extract_section(cleaned, SEC4_START, SEC5_START)

    if not sec2 and not sec3 and not sec4:
        return _compress_fallback(cleaned, critical_sentences)

    # 4단계: 섹션별 압축
    compressed2 = _compress_sec2(sec2)
    compressed3 = _compress_sec3(sec3)
    compressed4 = _compress_sec4(sec4)

    # 5단계: 조립
    parts = []
    if compressed2:
        parts.append(f"[II. 사업의 내용]\n{compressed2}")
    if compressed3:
        parts.append(f"[III. 재무에 관한 사항]\n{compressed3}")
    if compressed4:
        parts.append(f"[IV. 이사의 경영진단]\n{compressed4}")
    result = "\n\n".join(parts).strip()

    # 6단계: 위험 문장 미포함 시 끝에 추가
    result = _append_missing_critical_sentences(result, critical_sentences)

    # 7단계: 전체 길이 초과 시 절단
    if len(result) > TOTAL_MAX:
        result = _trim_to_max(result, TOTAL_MAX)

    return result


def _preprocess(text: str) -> str:
    result = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\n{4,}", "\n\n", result)
    result = _PAGE_NUMBER_PATTERN_1.sub("", result)
    result = _PAGE_NUMBER_PATTERN_2.sub("", result)

    for pattern in BOILERPLATE_PATTERNS:
        idx = result.find(pattern)
        if idx != -1:
            line_start = result.rfind("\n", 0, idx)
            line_end = result.find("\n", idx)
            line_start = max(line_start, 0)
            if line_end == -1:
                line_end = len(result)
            result = result[:line_start] + result[line_end:]

    return result.strip()


def _extract_section(text: str, start_keys: list[str], end_keys: list[str]) -> str:
    start = _find_first_index(text, start_keys)
    if start == -1:
        return ""

    end = len(text)
    found_end = None
    for key in end_keys:
        idx = _find_safe(text, key, start + 1)
        if idx != -1 and (found_end is None or idx < found_end):
            found_end = idx
    if found_end is not None:
        end = found_end

    return text[start:end]


def _find_safe(text: str, key: str, from_index: int) -> int:
    """
    text.find(key, from_index)와 같지만, key가 짧은 로마숫자/영문 조각일 때
    바로 앞 글자가 알파벀(영문)이면 그 매칭을 건너뛴다.
    예: "IV. 이사의 경영진단" 안의 "V."가 SEC5_START의 "V."와 우연히 일치하는 경우를 방지.
    """
    idx = from_index
    while True:
        idx = text.find(key, idx)
        if idx == -1:
            return -1
        prev_char = text[idx - 1] if idx > 0 else ""
        if not prev_char.isalpha():
            return idx
        idx += 1  # 오탐이었으므로 한 글자 옮겨서 재탐색


def _find_first_index(text: str, keys: list[str]) -> int:
    earliest = -1
    for key in keys:
        idx = text.find(key)
        if idx != -1 and (earliest == -1 or idx < earliest):
            earliest = idx
    return earliest


def _compress_sec2(sec2: str) -> str:
    if not sec2.strip():
        return ""

    intro = sec2[: min(MAX_SEC2_INTRO, len(sec2))]
    parts = [intro]

    revenue_chunk = _extract_revenue_chunk(sec2)
    if revenue_chunk and revenue_chunk[: min(30, len(revenue_chunk))] not in intro:
        parts.append(revenue_chunk[: min(MAX_SEC2_REVENUE, len(revenue_chunk))])

    return "\n".join(parts).strip()


def _extract_revenue_chunk(text: str) -> str:
    """'%' 또는 금액 단위가 100자 내에 3개 이상 등장하는 밀집 구간을 찾는다."""
    positions = [m.start() for m in REVENUE_PCT_PATTERN.finditer(text)]
    if len(positions) < 2:
        return ""

    for i in range(len(positions) - 1):
        far_idx = min(i + 2, len(positions) - 1)
        if positions[far_idx] - positions[i] < 400:
            chunk_start = max(0, positions[i] - 50)
            chunk_end = min(len(text), positions[i] + 500)
            return text[chunk_start:chunk_end]
    return ""


def _compress_sec3(sec3: str) -> str:
    if not sec3.strip():
        return ""

    is_keywords = ["연결 포괄손익계산서", "연결포괄손익계산서", "연결 손익계산서", "연결손익계산서", "포괄손익계산서", "손익계산서"]
    is_start = _find_first_index(sec3, is_keywords)
    target_area = sec3[is_start: is_start + 2000] if is_start != -1 else sec3

    kept: list[str] = []
    in_table = False

    for line in target_area.split("\n"):
        t = line.strip()
        if not t:
            continue

        if "당기" in t and "전기" in t:
            kept.append(t)
            in_table = True
            continue

        is_key = any(k in t for k in FINANCE_KEYS)
        has_amount = bool(re.search(r"[,\d]{5,}", t))

        if in_table and is_key and has_amount:
            kept.append(_truncate_line(t, 80))

        if "별도" in t and "손익" in t:
            break

        if len(kept) >= 8:
            break

    if not kept:
        return target_area[: min(MAX_SEC3_FINANCE, len(target_area))].strip()

    return "\n".join(kept)


def _compress_sec4(sec4: str) -> str:
    if not sec4.strip():
        return ""

    front = sec4[: min(MAX_SEC4_MDA_FRONT, len(sec4))]
    parts = [front]

    risk_sentences = _extract_risk_sentences(sec4, front)
    if risk_sentences:
        parts.append("[주요 위험 요인]\n" + " ".join(risk_sentences))

    return "\n".join(parts).strip()


def _extract_risk_sentences(full_text: str, already_included: str) -> list[str]:
    """앞부분에 없는 위험 관련 문장 추출. 수치가 있는 것 우선, 최대 3문장."""
    risk_keywords = ["리스크", "위험", "우려", "악화", "하락", "감소", "적자", "손실", "부담", "불확실"]
    result: list[str] = []
    total_len = 0

    for sentence in _SENTENCE_SPLIT_PATTERN.split(full_text):
        t = sentence.strip()
        if len(t) < 15 or len(t) > 200:
            continue
        if t[: min(20, len(t))] in already_included:
            continue

        has_risk = any(k in t for k in risk_keywords)
        has_number = bool(re.search(r"\d+", t))

        if has_risk and has_number:
            result.append(t)
            total_len += len(t)
            if len(result) >= 3 or total_len >= MAX_SEC4_RISK_SENTENCES:
                break

    return result


def _extract_critical_sentences(text: str) -> list[str]:
    result: list[str] = []
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
    """섹션 구분이 안 되는 경우: 숫자 포함 문장 우선 추출."""
    parts: list[str] = []
    total_len = 0

    for sentence in _SENTENCE_SPLIT_PATTERN.split(text):
        t = sentence.strip()
        if not t or len(t) > 200:
            continue
        has_number = bool(re.search(r"\d+", t))
        has_finance = any(k in t for k in FINANCE_KEYS)
        if has_number or has_finance:
            parts.append(t)
            total_len += len(t)
            if total_len >= TOTAL_MAX:
                break

    result = " ".join(parts).strip()
    return _append_missing_critical_sentences(result, criticals)


def _truncate_line(line: str, max_len: int) -> str:
    return line if len(line) <= max_len else line[:max_len] + "…"


def _trim_to_max(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text

    critical_idx = text.find("[중요 공시 사항]")
    if critical_idx != -1 and critical_idx < max_len:
        critical = text[critical_idx:]
        allowed_front = max_len - len(critical) - 2
        return text[: max(0, allowed_front)] + "\n" + critical

    return text[:max_len]


# ── T1~T5 타겟별 소제목 키워드(전 상장사 공통 DART 표준 양식) ───────
TARGET_HEADINGS: dict[str, list[str]] = {
    "T1_Market_Competitiveness": [
        "1. 사업의 개요", "가. 사업의 개요", "사업의 개요",
        "주요 제품", "주요제품 등의 현황", "시장점유율", "시장 점유율",
    ],
    "T2_Financial_Health": [
        "연결 손익계산서", "연결손익계산서", "연결 포괄손익계산서",
        "요약재무정보", "요약 재무정보", "재무상태표",
    ],
    "T3_Risk_Exposure": [
        "사업의 위험", "가. 사업의 위험", "다. 사업의 위험",
        "나. 회사의 현황", "법적 절차", "우발채무", "우발 채무",
        "계속기업 관련 중요한 불확실성",
    ],
    "T4_Governance": [
        "이사회 등 회사의 기관에 관한 사항", "이사회 현황",
        "사외이사", "이사회 내 위원회", "최대주주 등의 현황",
    ],
    "T5_Growth_Potential": [
        "연구개발활동", "연구개발 활동", "3. 연구개발활동",
        "연구개발비용", "향후 투자계획", "투자계획",
    ],
}

# 다음 소제목 경계 패턴("1. ", "가. " 등 — 사업보고서 표준 번호체계)
_NEXT_HEADING_PATTERN = re.compile(r"\n\s*([0-9]+\.|[가-하]\.)\s*[가-힣]")

MAX_CHUNK_LENGTH = 2500  # 타겟별 최대 길이(자)


@dataclass
class ExtractedChunk:
    text: str
    start_offset: int


def extract_target_chunks(full_text: str) -> dict[str, ExtractedChunk]:
    """사업보고서 원문 전체에서 T1~T5 분석 대상 구획만 추출한다."""
    result: dict[str, ExtractedChunk] = {}

    for target_code, headings in TARGET_HEADINGS.items():
        chunk = _find_heading_chunk(full_text, headings)
        if chunk is not None:
            result[target_code] = chunk

    return result


def build_gemini_input(chunks: dict[str, ExtractedChunk]) -> str:
    """Gemini에 전달할 최종 입력 텍스트 조립(구획만, 원문 전체 아님)."""
    parts = []
    for target_code, chunk in chunks.items():
        parts.append(f"=== {target_code} ===\n{chunk.text}\n")
    return "\n".join(parts)


def resolve_offset_in_full_text(chunks: dict[str, ExtractedChunk], target_text: str) -> int:
    """Gemini가 반환한 targetText(원문 일부)의 전체 문서 내 실제 offset 계산. 못 찾으면 -1(drift)."""
    for chunk in chunks.values():
        local_idx = chunk.text.find(target_text)
        if local_idx != -1:
            return chunk.start_offset + local_idx
    return -1


def _find_heading_chunk(full_text: str, heading_keywords: list[str]) -> ExtractedChunk | None:
    start = -1
    for keyword in heading_keywords:
        idx = full_text.find(keyword)
        if idx != -1 and (start == -1 or idx < start):
            start = idx
    if start == -1:
        return None

    end = len(full_text)
    match = _NEXT_HEADING_PATTERN.search(full_text, start + 10)
    if match:
        end = match.start()

    if end - start > MAX_CHUNK_LENGTH:
        end = _find_sentence_boundary(full_text, start + MAX_CHUNK_LENGTH)

    chunk_text = full_text[start:end]
    return ExtractedChunk(text=chunk_text, start_offset=start)


def _find_sentence_boundary(text: str, approx_end: int) -> int:
    search_end = min(approx_end + 200, len(text))
    for i in range(approx_end, search_end):
        if text[i] in (".", "다"):
            return i + 1
    return min(approx_end, len(text))


# =====================================================================
# 요약 프롬프트
# =====================================================================

SUMMARY_SYSTEM_INSTRUCTION = (
    "당신은 여의도 최고의 시니어 주식 애널리스트입니다. "
    "수십 페이지에 달하는 상장사의 '사업보고서' 핵심 텍스트를 읽고, "
    "일반 개인 투자자가 10초 만에 기업의 펀더멘털과 현 상태를 완벽히 이해할 수 있도록 "
    "직관적이고 명확하게 요약해야 합니다. "
    "마크다운 기호(```json 등)나 부가 설명 없이 "
    "오직 지정된 JSON 객체 형식으로만 응답하십시오."
)

SUMMARY_USER_PROMPT_TEMPLATE = (
    "다음은 [{corp_name}]의 사업보고서 핵심 섹션(사업의 내용, 재무사항, 이사의 경영진단)을 발췌한 원문입니다.\n\n"
    "[요약 미션]\n"
    "제시된 원문을 바탕으로 기업의 한 해 농사를 총평하는 요약본을 작성하십시오.\n\n"
    "[Output Guidelines]\n"
    "1. summary_text: 주력 사업의 성과와 당기 실적(매출/영업이익의 전년 대비 증감)을 1문장(50자 이내)으로 강렬하게 압축하십시오.\n"
    "2. investor_comment: 실적이 변동한 핵심 원인(예: 환율, 판가 하락, 신제품 호조 등)과 "
    "향후 기업의 가치를 좌우할 핵심 동인(R&D 투자, 주주환원 등)을 "
    "3문장 이내의 전문가 톤으로 해설하십시오.\n"
    "3. overall_risk: 원문에서 자본잠식, 대규모 적자 전환, 심각한 현금흐름 악화, 횡령/배임, "
    "상장폐지 사유 등 치명적 악재가 발견되면 'Critical' 또는 'High'를 부여하고, "
    "안정적인 성장이나 무난한 흑자 기조라면 'Low'나 'Neutral'을 부여하십시오.\n\n"
    "[JSON Schema]\n"
    "{{\n"
    '  "summary_text": "(String) 1문장 실적 및 성과 요약",\n'
    '  "investor_comment": "(String) 3문장 이내의 투자자 관점 상세 해설",\n'
    '  "overall_risk": "(Enum: [Low, Neutral, High, Critical])"\n'
    "}}\n\n"
    "[사업보고서 발췌 데이터]\n{context}"
)

# =====================================================================
# 분석 프롬프트
# =====================================================================

ANALYSIS_SYSTEM_INSTRUCTION = (
    "너는 기업 분석 및 회계 감사 전문 애널리스트다. 상장사의 '사업보고서(정기공시)'를 분석하여, "
    "해당 기업의 수익성, 경쟁우위, 잠재적 리스크, 그리고 경영 효율성을 평가하라."
)

ANALYSIS_USER_PROMPT_TEMPLATE = (
    "[Mission]\n"
    "제공된 DART '사업보고서' 발췌 구획을 분석하여, "
    "1) 본업의 경쟁력(Pricing Power), 2) 재무적 지속가능성(Profitability), "
    "3) 경영진이 인지하는 핵심 리스크(Key Risks), 4) 미래 성장 동력(R&D/CAPEX)을 "
    "식별하고 JSON 배열로 반환하라.\n\n"
    "[Output Guidelines]\n"
    "1. '해자(Economic Moat)', '마진 압박(Margin Compression)', "
    "'우발 부채(Contingent Liability)', '자본 지출(CAPEX Intensity)' 같은 "
    "전문 용어를 쓸 때는 반드시 괄호로 쉬운 말 설명을 바로 덧붙여라 "
    "(예: \"마진 압박(원가 상승으로 이익률이 줄어드는 현상)\"). 용어만 던지고 "
    "설명 없이 넘어가지 마라.\n"
    "2. 경영진의 리스크 섹션에서 '지속가능성 불확실성'이 언급되면 반드시 "
    "'Critical' 등급(riskLevel)으로 분류하라.\n"
    "3. R&D 비용이 매출 대비 감소 추세라면 'Innovation Slowdown'으로 분류하고 "
    "materialImpact에 명시하라.\n"
    "4. materialImpact는 배경 설명 없이 결론(이 사실이 투자자에게 왜 중요한지)부터 "
    "최대 2문장으로 간결하게 써라. 3문장 이상, 군더더기 수식어, 원문 재진술은 금지.\n"
    "5. targetKey는 반드시 투자 판단의 근거가 되는 서술형 문장이어야 한다. "
    "표의 항목명·레이블·단어 하나(예: \"장내매도(-)\" \"적정\" \"보유목적\" 등)는 "
    "targetKey로 절대 사용 금지. 아래 발췌 구획 원문에서 정확히 일치하는 "
    "문자열만 사용하라(재구성·요약 금지 — 한 글자도 다르면 안 됨).\n"
    "6. 결과는 반드시 아래 구조의 순수한 JSON 배열 형태로만 출력하고, "
    "마크다운 기호나 추가 텍스트를 절대 붙이지 말 것.\n\n"
    "[JSON Schema]\n"
    "[\n"
    "  {{\n"
    '    "analysisCategory": "(Enum: [Market_Competitiveness, Financial_Health, Risk_Exposure, Governance, Growth_Potential])",\n'
    '    "targetKey": "(String) 반드시 아래 조건을 모두 충족하는 완전한 서술형 문장\n      · 최소 20자 이상, 주어+서술어 구조의 완전한 문장\n      · 파이프(|)가 포함된 표 행 사용 절대 금지\n      · 표 항목명·레이블·단어 단독 금지 (예: \"장내매도(-)\" \"적정\" \"보유목적\" \"주주배정\" \"경영참가\")\n      · \"~합니다\" \"~됩니다\" \"~있습니다\" \"~입니다\"처럼 서술어로 끝나야 함\n      · 원문에서 한 글자도 수정 없이 그대로 발췌",\n'
    '    "materialImpact": "(String) 결론부터 쉬운 말로, 최대 2문장",\n'
    '    "riskLevel": "(Enum: [Low, Neutral, High, Critical])"\n'
    "  }}\n"
    "]\n\n"
    "[공시 원문 발췌 데이터]\n{context}"
)


# =====================================================================
# 레지스트리 인터페이스 — registry.PipelineSpec이 호출하는 함수들
# =====================================================================

def compress_for_summary(full_text: str) -> str:
    """레지스트리 인터페이스: 요약용 압축 진입점."""
    return compress(full_text)


def extract_analysis_chunks(full_text: str) -> dict[str, ExtractedChunk]:
    """레지스트리 인터페이스: 분석용 좌표보존 압축 진입점."""
    return extract_target_chunks(full_text)


def build_analysis_input(chunks: dict[str, ExtractedChunk]) -> str:
    """레지스트리 인터페이스: 분석용 Gemini 입력 조립 진입점."""
    return build_gemini_input(chunks)


def resolve_offset(chunks: dict[str, ExtractedChunk], target_text: str) -> int:
    """레지스트리 인터페이스: targetKey -> 원문 오프셋 역산 진입점."""
    return resolve_offset_in_full_text(chunks, target_text)
