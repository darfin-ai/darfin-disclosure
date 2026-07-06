"""
DART Open API 공시 수집기.

역할: DART API를 호출해서 기업정보·공시목록을 파싱하고 JSON으로 반환.
      DB는 전혀 모른다 — stock/disclosure INSERT는 Spring(JPA)이 한다.

DART API 흐름:
  ① GET https://opendart.fss.or.kr/api/corpCode.xml
      → ZIP(CORPCODE.xml) → 전체 기업 고유번호 목록
      → companyName 또는 stockCode로 기업을 찾아 dart_corp_code 확보

  ② GET https://opendart.fss.or.kr/api/company.json?corp_code=...
      → 해당 기업의 stock_code, corp_cls(Y=KOSPI, K=KOSDAQ) 확인

  ③ GET https://opendart.fss.or.kr/api/list.json?corp_code=...&bgn_de=...&end_de=...
      → 기간 내 공시 목록(rcept_no, report_nm, flr_nm, rcept_dt, pblntf_ty)

이 결과를 DartCollectResponse(JSON)로 돌려주면,
Spring이 받아서 stock + disclosure 테이블에 UPSERT한다.
"""
import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import httpx
from lxml import etree as LET

from config import settings
from app.schemas import (
    CorpInfo,
    DartCollectRequest,
    DartCollectResponse,
    DisclosureItem,
)

import json

DART_BASE = "https://opendart.fss.or.kr/api"


def _get_with_retry(url: str, params: dict, timeout: int = 30, retries: int = 1) -> httpx.Response:
    """
    httpx.get 래퍼. 사내망/VPN에서 DART 서버로의 연결이 일시적으로 끊기는 경우(타임아웃,
    연결 거부 등)를 대비해 한 번 더 재시도한다.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return httpx.get(url, params=params, timeout=timeout)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1)
    raise last_exc

# ── pblntf_ty(대분류) → disclosure_type.type_code 매핑 ──────────────
# DART의 공시 대분류 코드를 Darfin의 type_code로 변환한다.
PBLNTF_TY_TO_TYPE_CODE: dict[str, str] = {
    "A": "BIZ_REPORT",         # 정기공시 (사업/반기/분기보고서)
    "B": "MAJOR_EVENT",        # 주요사항보고
    "C": "ISSUANCE",           # 발행공시
    "D": "EQUITY",             # 지분공시
    "E": "OTHER",              # 기타공시
    "F": "AUDIT",              # 외부감사관련
    "G": "FUND",               # 펀드공시
    "H": "ABS",                # 자산유동화
    "I": "EXCHANGE",           # 거래소공시
    "J": "FTC",                # 공정위공시
}

# ── 제목 키워드 기반 type_code 추론 ──────────────────────────────────
# DART list.json이 pblntf_ty를 null로 내려줄 때 공시 제목으로 대분류를 추론한다.
# 위에서 아래로 순서대로 매칭하므로 더 구체적인 패턴을 먼저 배치한다.
_TITLE_TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    # 정기공시
    (("사업보고서", "반기보고서", "분기보고서"), "BIZ_REPORT"),
    # 외부감사
    (("감사보고서", "내부회계관리제도", "감사의견"), "AUDIT"),
    # 펀드공시
    (("집합투자", "투자설명서", "펀드"), "FUND"),
    # 자산유동화
    (("자산유동화", "유동화전문회사"), "ABS"),
    # 거래소공시
    (("불성실공시", "관리종목", "매매거래정지", "상장폐지"), "EXCHANGE"),
    # 공정위공시
    (("대규모내부거래", "기업집단현황", "공정거래"), "FTC"),
    # 발행공시
    (("증권신고서", "투자설명서", "소액공모", "증권발행실적"), "ISSUANCE"),
    # 지분공시 — 임원·주요주주 소유보고 계열
    (("소유상황보고서", "대량보유상황보고", "주식등의대량보유"), "EQUITY"),
    # 유상증자 (MAJOR_EVENT 세부)
    (("유상증자결정",), "RIGHTS_OFFERING"),
    # 주요사항보고서 계열
    (("주요사항보고서", "자기주식취득결정", "자기주식처분결정",
      "자기주식취득결과", "자기주식처분결과",
      "전환사채", "신주인수권부사채", "교환사채",
      "영업(잠정)실적", "잠정실적", "현금·현물배당결정",
      "주식배당결정", "무상증자결정", "합병결정", "분할결정",
      "중요한자산취득", "중요한자산처분"), "MAJOR_EVENT"),
    # 기업설명회·조회공시 등 → 기타공시
    (("기업설명회", "조회공시", "풍문또는보도", "장래사업"), "OTHER"),
]


def _infer_type_code_from_title(title: str) -> str:
    """공시 제목 키워드로 type_code를 추론한다. 매칭 실패 시 'OTHER' 반환."""
    for keywords, type_code in _TITLE_TYPE_RULES:
        if any(kw in title for kw in keywords):
            return type_code
    return "OTHER"

CORP_CLS_TO_MARKET: dict[str, str] = {
    "Y": "KOSPI",
    "K": "KOSDAQ",
    "N": "KONEX",
    "E": "비상장",
}

# ── corpCode.xml 인메모리 캐시 ────────────────────────────────────
# DART 전체 기업코드 목록(zip)은 매 검색마다 다시 받기엔 너무 무겁고(수만 건),
# 하루 단위로만 갱신되는 데이터라 프로세스 메모리에 캐싱해 둔다.
# 캐싱 전에는 자동수집 요청마다 이 다운로드+파싱 때문에 응답이 느려져
# 화면에서는 "버튼이 응답 없음"처럼 보였다.
_CORP_LIST_CACHE_TTL_SECONDS = 24 * 60 * 60
_corp_list_cache: list[tuple[str, str, str]] | None = None
_corp_list_cache_fetched_at: float = 0.0


def _load_corp_list() -> list[tuple[str, str, str]]:
    """(corp_code, corp_name, stock_code) 튜플 리스트를 캐시에서 반환하거나, 없으면 새로 받아온다."""
    global _corp_list_cache, _corp_list_cache_fetched_at

    now = time.monotonic()
    if _corp_list_cache is not None and (now - _corp_list_cache_fetched_at) < _CORP_LIST_CACHE_TTL_SECONDS:
        return _corp_list_cache

    resp = httpx.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": settings.dart_api_key}, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes.decode("utf-8"))

    corp_list = [
        (
            (corp.findtext("corp_code") or "").strip(),
            (corp.findtext("corp_name") or "").strip(),
            (corp.findtext("stock_code") or "").strip(),
        )
        for corp in root.findall("list")
    ]

    _corp_list_cache = corp_list
    _corp_list_cache_fetched_at = now
    return corp_list


def collect(req: DartCollectRequest) -> DartCollectResponse:
    """
    메인 수집 함수.
    ① DART에서 기업 고유번호(dart_corp_code) 탐색
    ② 기업 개황(stock_code, market_type) 조회
    ③ 기간 내 공시 목록 조회
    → DartCollectResponse(JSON)로 반환
    """
    # ── ① 기업 고유번호 탐색 ────────────────────────────────────────
    try:
        corp_code, corp_name = _find_corp_code(req.companyName)
    except Exception as exc:
        return DartCollectResponse(success=False, errorMessage=f"기업 고유번호 탐색 실패: {exc}")

    if corp_code is None:
        return DartCollectResponse(
            success=False,
            errorMessage=f"'{req.companyName}'에 해당하는 기업을 DART에서 찾을 수 없습니다.",
        )

    # ── ② 기업 개황(stock_code, market_type) ────────────────────────
    try:
        corp_info = _fetch_company_info(corp_code, corp_name)
    except Exception as exc:
        return DartCollectResponse(success=False, errorMessage=f"기업 개황 조회 실패: {exc}")

    # ── ③ 공시 목록 조회 ─────────────────────────────────────────────
    try:
        disclosures = _fetch_disclosure_list(corp_code, req.bgnDe, req.endDe)
    except Exception as exc:
        return DartCollectResponse(
            success=False,
            corp=corp_info,
            errorMessage=f"공시 목록 조회 실패: {exc}",
        )

    return DartCollectResponse(
        success=True,
        corp=corp_info,
        disclosures=disclosures,
        totalCount=len(disclosures),
    )


def _find_corp_code(query: str) -> tuple[str | None, str]:
    """
    DART corpCode.xml(ZIP, 캐시됨)에서 기업명 또는 종목코드로 corp_code를 탐색한다.
    반환: (corp_code, corp_name) — 없으면 (None, "")
    """
    corp_list = _load_corp_list()

    # 종목코드로 먼저 정확히 매칭, 없으면 기업명 포함 매칭
    is_stock_code = bool(re.match(r"^\d{6}$", query.strip()))
    exact_match = None
    contains_match = None

    for code, name, stock in corp_list:
        if is_stock_code:
            if stock == query.strip():
                return code, name
        else:
            if name == query.strip():
                exact_match = (code, name)
            elif query.strip() in name and contains_match is None:
                contains_match = (code, name)

    if exact_match:
        return exact_match
    if contains_match:
        return contains_match
    return None, ""


def _fetch_company_info(corp_code: str, corp_name: str) -> CorpInfo:
    """
    DART company.json으로 stock_code, corp_cls(시장구분)를 조회한다.
    """
    resp = httpx.get(
        f"{DART_BASE}/company.json",
        params={"crtfc_key": settings.dart_api_key, "corp_code": corp_code},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        # 기업 개황 조회 실패 시 — 이미 corp_code는 확보했으므로 기본값으로 진행
        return CorpInfo(
            dartCorpCode=corp_code,
            companyName=corp_name,
            stockCode=None,
            marketType="비상장",
        )

    stock_code = data.get("stock_code", "").strip() or None
    corp_cls = data.get("corp_cls", "E").strip()
    market_type = CORP_CLS_TO_MARKET.get(corp_cls, "비상장")
    name = data.get("corp_name", corp_name).strip()

    return CorpInfo(
        dartCorpCode=corp_code,
        companyName=name,
        stockCode=stock_code,
        marketType=market_type,
    )


def _fetch_disclosure_list(
    corp_code: str, bgn_de: str, end_de: str
) -> list[DisclosureItem]:
    """
    DART list.json으로 기간 내 공시 목록을 가져온다.
    DART는 한 번에 최대 100건을 반환하고 페이지네이션이 있으므로
    마지막 페이지까지 반복 호출한다.
    """
    items: list[DisclosureItem] = []
    page_no = 1

    while True:
        resp = httpx.get(
            f"{DART_BASE}/list.json",
            params={
                "crtfc_key": settings.dart_api_key,
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_no": page_no,
                "page_count": 100,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status == "013":
            # 데이터 없음 — 정상적으로 0건인 경우
            break
        if status != "000":
            raise RuntimeError(f"DART list.json 오류: status={status}, message={data.get('message')}")

        for row in data.get("list", []):
            rcept_no = (row.get("rcept_no") or "").strip()
            if not rcept_no:
                continue

            pblntf_ty = (row.get("pblntf_ty") or "").strip()
            report_nm = (row.get("report_nm") or "").strip()
            if pblntf_ty:
                type_code = PBLNTF_TY_TO_TYPE_CODE.get(pblntf_ty, "OTHER")
            else:
                # DART가 pblntf_ty를 null로 내려줄 때 제목으로 대분류 추론
                type_code = _infer_type_code_from_title(report_nm)

            # rcept_dt 형식: "20260101123456"(14자리) → filed_at: "2026-01-01"
            rcept_dt_raw = (row.get("rcept_dt") or "").strip()
            filed_at = _parse_filed_at(rcept_dt_raw)

            items.append(
                DisclosureItem(
                    rceptNo=rcept_no,
                    dartCorpCode=corp_code,
                    typeCode=type_code,
                    title=(row.get("report_nm") or "").strip(),
                    filerName=(row.get("flr_nm") or "").strip(),
                    filedAt=filed_at,
                    rawZipPath=None,  # 원문 다운로드는 별도 단계(Spring이 요청 시 처리)
                )
            )

        # 페이지네이션 확인
        total_count = int(data.get("total_count", 0))
        if page_no * 100 >= total_count:
            break
        page_no += 1

    return items


def _parse_filed_at(rcept_dt: str) -> str:
    """
    DART의 rcept_dt는 "20260101123456" 또는 "20260101" 형식.
    YYYY-MM-DD 형식으로 변환한다.
    """
    digits = re.sub(r"\D", "", rcept_dt)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


# ── 공시 원문 조회 ────────────────────────────────────────────────
# DisclosureViewer.jsx 좌측 "공시 원문" 탭 + 요약/분석 압축 단계의 입력(dartContext/dartFullText)
# 둘 다 이 함수가 만드는 평문을 그대로 사용한다.

def fetch_document_zip(rcept_no: str) -> bytes:
    """
    DART document.xml API에서 공시 원문 ZIP 파일을 그대로 반환한다.
    fetch_document_text와 같은 엔드포인트를 호출하지만, XML 파싱 없이 바이너리 그대로 돌려준다.
    브라우저 다운로드용으로 main.py의 StreamingResponse에서 사용한다.
    """
    resp = _get_with_retry(
        f"{DART_BASE}/document.xml",
        params={"crtfc_key": settings.dart_api_key, "rcept_no": rcept_no},
    )
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError("DART document.xml 응답을 파싱할 수 없습니다.")
        raise RuntimeError(
            f"DART document.xml 오류: status={data.get('status')}, message={data.get('message')}"
        )

    return resp.content


def fetch_document_text(rcept_no: str) -> str:
    """하위호환용 — 평문만 필요한 호출부를 위해 fetch_document()의 text만 반환한다."""
    text, _blocks = fetch_document(rcept_no)
    return text


def fetch_document(rcept_no: str) -> tuple[str, list[dict]]:
    """
    GET https://opendart.fss.or.kr/api/document.xml?crtfc_key=...&rcept_no=...
    공시 원문(ZIP 안의 XML 1개 이상)을 받아 (평문, 구조화 블록 목록)으로 변환해 반환한다.

    text: 태그를 제거한 평문. 요약/분석 압축 입력과 하이라이트 charOffset 계산 기준이 되는
          기존 좌표계를 그대로 유지한다(하위호환).
    blocks: 문단/표 단위로 구조화한 목록. 표는 "셀 | 셀" 평문 대신 실제 행/열(rows)로 내려줘서
          프론트가 <table>로 그릴 수 있게 한다. 각 블록의 charStart/charEnd는 위 text 안에서
          그 블록이 차지하는 문자 구간이라 기존 하이라이트 offset과 그대로 맞물린다.

    사내망/VPN 환경에서는 DART 서버 연결이 가끔 일시적으로 느려지거나 끊기는 경우가 있어
    (WinError 10060 등 연결 타임아웃), 한 번 더 재시도한다.
    """
    resp = _get_with_retry(
        f"{DART_BASE}/document.xml",
        params={"crtfc_key": settings.dart_api_key, "rcept_no": rcept_no},
    )
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        # 정상적으로 ZIP을 못 내려주는 경우 DART는 {"status": "...", "message": "..."} JSON을 돌려준다.
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError("DART document.xml 응답을 파싱할 수 없습니다.")
        raise RuntimeError(f"DART document.xml 오류: status={data.get('status')}, message={data.get('message')}")

    file_texts: list[str] = []
    file_blocks: list[list[dict]] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in sorted(zf.namelist()):
            t, b = _xml_to_text_and_blocks(zf.read(name))
            file_texts.append(t)
            file_blocks.append(b)

    # 파일이 여러 개면 기존과 동일하게 "\n\n"으로 이어붙이되, 앞선 파일들의 누적 길이만큼
    # 각 파일의 블록 offset을 shift해서 전체 text 좌표계에 맞춘다.
    parts: list[str] = []
    blocks: list[dict] = []
    offset = 0
    for t, b in zip(file_texts, file_blocks):
        if not t.strip():
            continue
        if parts:
            offset += 2  # "\n\n" 구분자
        for blk in b:
            shifted = dict(blk)
            if shifted.get("charStart") is not None:
                shifted["charStart"] += offset
                shifted["charEnd"] += offset
            blocks.append(shifted)
        parts.append(t)
        offset += len(t)

    text = "\n\n".join(parts)
    return text, blocks


# 문단/표 구분 없이 모든 태그를 공백 하나로 뭉개면 화면에서 읽기 어려워서,
# ElementTree로 구조를 따라가며 문단은 줄바꿈으로, 표는 행/열 구조로 풀어준다.
def _xml_to_text_and_blocks(xml_bytes: bytes) -> tuple[str, list[dict]]:
    """
    DART 공시 원문 XML(HWP 변환 산출물)을 (평문, 구조화 블록 목록)으로 변환한다.
    평문은 기존 _xml_to_readable_text와 동일한 규칙(공백 정리 포함)으로 만들어 하위호환을 유지하고,
    블록은 그 평문 안에서 자신이 차지하는 위치(charStart/charEnd)를 순차 탐색으로 복원해 붙인다.
    """
    try:
        raw = xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raw = xml_bytes.decode("cp949", errors="ignore")

    # DART의 HWP→XML 변환 산출물은 "&"나 "<"가 이스케이프되지 않은 채 텍스트에 그대로
    # 들어있는 경우가 흔하다(예: "S&P", "D&M Holdings", "3(전)기<별도재무제표>"). 표준
    # xml.etree.ElementTree는 이런 문서를 통째로 파싱 실패 처리해서 구조를 다 잃어버리므로,
    # libxml2 기반 lxml의 관용(recover) 모드로 어긋난 부분만 건너뛰고 최대한 복구한다.
    #
    # 거래소 조회공시류(예: "장래사업ㆍ경영계획")는 DART 고유 스키마가 아니라 KRX가 만드는
    # 순수 HTML 폼 원문을 그대로 내려준다 — 이건 우리가 선택할 수 있는 게 아니라 DART가
    # 실제로 그렇게 준다. XML recover 모드는 <br>처럼 HTML의 "빈 태그"(닫지 않아도 되는
    # 태그) 관례를 모르기 때문에 <br> 뒤를 어디서 닫아야 할지 잘못 추측해서, 뒤따라오는
    # 형제 행(<tr>)들이 그 셀 안으로 통째로 삼켜지는 오염이 생겼다(직접 대조 확인함).
    # 그래서 HTML 문서는 <br>/<img> 같은 빈 태그를 제대로 아는 전용 HTMLParser로 보낸다.
    body_stripped = raw.lstrip()
    is_html_doc = bool(re.match(r"<!doctype\s+html|<html[\s>]", body_stripped, re.IGNORECASE))

    if is_html_doc:
        parser = LET.HTMLParser(recover=True)
        root = LET.fromstring(raw, parser=parser)
    else:
        xml_decl_stripped = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw, count=1)
        parser = LET.XMLParser(recover=True)
        try:
            root = LET.fromstring(xml_decl_stripped, parser=parser)
        except LET.XMLSyntaxError:
            root = None

    if root is None:
        fallback = _strip_tags_fallback(raw)
        blocks = [{"type": "paragraph", "text": fallback, "charStart": 0, "charEnd": len(fallback)}] if fallback else []
        return fallback, blocks

    def tag_of(el) -> str:
        # 주석/처리명령(PI) 노드는 tag가 문자열이 아니라 콜러블이라 upper() 호출 시 죽는다.
        return el.tag.upper() if isinstance(el.tag, str) else ""

    # 사용자에게 보여줄 필요가 없는 태그 — 내용째 건너뜀
    _SKIP_TAGS = {"STYLE", "SCRIPT", "HEAD", "META", "LINK"}

    def walk_into(el, target: list[dict]) -> None:
        """el의 내용을 target 리스트에 블록으로 쌓는다. 표 셀 안에서도 그대로 재사용되므로
        (build_cell_blocks) 표 안에 또 표가 중첩된 경우(예: 사업보고서 각주의 종속기업 현황
        스케줄)도 문자열로 뭉개지 않고 실제 중첩 표로 재귀 처리된다."""
        if not isinstance(el.tag, str):
            # XML 주석(<!-- -->)·처리명령 노드 — Xalan 계열 변환기가 남긴
            # "<!--?javax.xml.transform.disable-output-escaping?-->" 같은 직렬화 힌트가
            # 주석으로 문서에 그대로 남아있는 경우가 있다. lxml에서 주석 노드의 .text는 그
            # 주석 내용 자체를 돌려주므로, 걸러내지 않으면 화면에 그대로 새어나온다.
            # (주석 바로 뒤에 이어지는 진짜 본문은 부모 루프의 tail 처리로 그대로 잡힌다.)
            return

        tag = tag_of(el)

        if tag in _SKIP_TAGS:
            return  # CSS·JS·메타 태그는 내용 포함 통째로 무시

        if tag == "TABLE":
            rows = render_table_rows(el)
            if rows:
                target.append({"type": "table", "rows": rows})
            return  # 표 내부는 더 내려가지 않는다(중복 렌더 방지)

        own_text = (el.text or "").strip()
        if own_text:
            target.append({"type": "paragraph", "text": own_text})

        for child in el:
            walk_into(child, target)
            tail = (child.tail or "").strip()
            if tail:
                target.append({"type": "paragraph", "text": tail})

    def build_cell_blocks(cell_el) -> list[dict]:
        """표 셀 하나의 내용을 문서 본문과 동일한 규칙(walk_into)으로 블록화한다.
        보통은 문단 블록 하나뿐이지만, 셀 안에 표가 중첩돼 있으면 표 블록도 섞여 나온다."""
        blocks: list[dict] = []
        walk_into(cell_el, blocks)
        return blocks

    def span_int(el, attr: str) -> int:
        # DART 원문의 재무제표/실적표는 ROWSPAN/COLSPAN을 자주 쓴다(예: "매출액"이 당해실적/
        # 누계실적 두 행에 걸쳐 한 번만 표시됨). 이걸 무시하고 매 행을 <td> 개수 그대로 나열하면
        # 행마다 칸 수가 달라져서 표가 어긋나 보인다 — 그래서 원문 속성값을 그대로 보존해
        # 프론트가 실제 <td rowSpan>/<td colSpan>으로 그리게 한다.
        # DART 고유 XML은 대문자(ROWSPAN), 거래소 HTML 폼(카테고리 I)은 소문자(rowspan)를 쓴다.
        raw_val = el.get(attr.upper()) or el.get(attr.lower())
        try:
            n = int(raw_val)
            return n if n > 0 else 1
        except (TypeError, ValueError):
            return 1

    def render_table_rows(table_el) -> list[list[dict]]:
        rows: list[list[dict]] = []
        for tr in table_el.iter():
            if tag_of(tr) != "TR":
                continue
            cells = [
                {
                    "rowSpan": span_int(cell, "ROWSPAN"),
                    "colSpan": span_int(cell, "COLSPAN"),
                    "blocks": build_cell_blocks(cell),
                }
                for cell in tr
                if tag_of(cell) in ("TD", "TU", "TE", "TH")
            ]
            cells = [c for c in cells if c["blocks"]]
            if cells:
                rows.append(cells)
        return rows

    # DOCUMENT-NAME/FORMULA-VERSION/COMPANY-NAME/SUMMARY 등은 <BODY> 앞에 오는 문서
    # 메타데이터(색인/서식버전 정보)라 실제 DART 뷰어 화면에는 나오지 않는다. <BODY>가
    # 있으면 그 안쪽만 훑어서 이 메타데이터가 본문 문단으로 새는 걸 막는다. <BODY>/<body>가
    # 없는 (드문) 포맷은 하위호환을 위해 루트 전체를 훑는다. recover 모드로 복구된 트리는
    # <body>가 루트 바로 아래가 아닐 수도 있어 전체를 훑어(iter) 찾는다.
    raw_blocks: list[dict] = []
    body_el = next((el for el in root.iter() if tag_of(el) == "BODY"), None)
    walk_into(body_el if body_el is not None else root, raw_blocks)

    # 셀이 이제 문자열이 아니라 (재귀적으로 중첩 가능한) 블록 목록이라, 평문 직렬화도
    # 재귀로 만든다. 셀 안 블록들은 "\n"으로, 셀들은 " | "로, 행들은 "\n"으로 잇는다.
    def block_plain_text(b: dict) -> str:
        if b["type"] == "table":
            row_lines = []
            for row in b["rows"]:
                cell_texts = [
                    "\n".join(t for t in (block_plain_text(cb) for cb in cell["blocks"]) if t)
                    for cell in row
                ]
                row_lines.append(" | ".join(cell_texts))
            return "\n".join(row_lines)
        return b["text"]

    plain_parts = [block_plain_text(b) for b in raw_blocks]
    text = "\n\n".join(p for p in plain_parts if p)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # raw_blocks를 text 안의 위치(charStart/charEnd)와 함께 다시 훑는다. 표는 자기 범위
    # 전체를 먼저 찾아 커서를 그 끝으로 넘긴 뒤(형제 블록 탐색용), 셀 내부는 표 시작 지점부터
    # 별도 커서로 재귀 탐색한다 — 그래야 중첩된 표/문단도 같은 좌표계 안에서 정확히 잡힌다.
    def assign_offsets(raw: list[dict], cursor: list[int]) -> list[dict]:
        result: list[dict] = []
        for b in raw:
            plain = block_plain_text(b)
            normalized = re.sub(r"[ \t]+", " ", plain).strip()
            if not normalized:
                continue

            idx = text.find(normalized, cursor[0])
            char_start = char_end = None
            if idx != -1:
                char_start, char_end = idx, idx + len(normalized)
                cursor[0] = char_end

            if b["type"] == "table":
                inner_cursor = [char_start if char_start is not None else cursor[0]]
                rows = [
                    [
                        {
                            "rowSpan": cell["rowSpan"],
                            "colSpan": cell["colSpan"],
                            "blocks": assign_offsets(cell["blocks"], inner_cursor),
                        }
                        for cell in row
                    ]
                    for row in b["rows"]
                ]
                result.append({"type": "table", "rows": rows, "charStart": char_start, "charEnd": char_end})
            else:
                result.append({"type": "paragraph", "text": normalized, "charStart": char_start, "charEnd": char_end})
        return result

    blocks = assign_offsets(raw_blocks, [0])

    return text, blocks


def _strip_tags_fallback(text: str) -> str:
    """XML 파싱이 실패한 경우(깨진 마크업 등)를 위한 단순 태그 제거 폴백."""
    # <style>...</style>, <script>...</script> 블록 전체 제거 (내용 포함)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
