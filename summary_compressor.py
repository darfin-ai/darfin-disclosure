"""
사업보고서 DART_SUMMARY_CONTEXT 압축기.
Java의 AnnualReportContextCompressor를 그대로 포팅했다.

목적: Gemini 입력 토큰 최소화
대상: 대한민국 DART 상장사 사업보고서 전체 (범용)
원칙:
  1. 섹션별 의미 있는 부분만 추출(단순 글자 수 절단 금지)
  2. 위험 신호 키워드는 어느 위치에 있어도 반드시 보존
  3. 숫자(금액·비율·날짜)가 포함된 문장 우선 보존
  4. 중복·상투 문장 제거

요약 전용 압축기다(분석용은 analysis_section_filter.py 참고 — 좌표 보존이 필요해 별도 로직).
"""
import re

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
