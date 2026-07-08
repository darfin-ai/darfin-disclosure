"""
Spring Boot와 주고받는 HTTP 요청/응답 스키마.
이 서비스는 DB 컬럼을 전혀 모른다 — 여기 정의된 필드만 보고 JSON을 만들면 되고,
risk_tier 같은 DB 비정규화 값은 전부 Spring 쪽 책임이다.
"""
from pydantic import BaseModel


class SummaryRequest(BaseModel):
    typeCode: str  # 공시유형 코드(예: "BIZ_REPORT"). registry.py에서 이 코드로 압축/프롬프트를 선택한다.
    corpName: str | None = None
    dartContext: str  # 압축 전 원문 텍스트(요약 대상 3섹션을 합친 것)


class SummaryResponse(BaseModel):
    success: bool
    summaryText: str | None = None
    investorComment: str | None = None
    overallRisk: str | None = None  # Gemini가 돌려준 원본 라벨. risk_tier 변환은 Spring이 함
    modelName: str | None = None  # 실제로 호출한 Gemini 모델명(settings.gemini_model). Spring이 DB model_name 컬럼에 그대로 저장한다.
    tokensIn: int | None = None
    tokensOut: int | None = None
    latencyMs: int | None = None
    errorCode: str | None = None  # PIPELINE_NOT_REGISTERED, TEXT_TOO_SHORT, GEMINI_TIMEOUT 등
    errorMessage: str | None = None


class AnalysisRequest(BaseModel):
    typeCode: str  # 공시유형 코드. registry.py에서 이 코드로 좌표보존 압축/프롬프트를 선택한다.
    corpName: str | None = None
    dartFullText: str  # 압축하지 않은 원문 전체(좌표 보존을 위해 그대로 전달받음)


class AnalysisItem(BaseModel):
    analysisCategory: str
    targetKey: str
    materialImpact: str
    riskLevel: str
    charOffsetStart: int
    charOffsetEnd: int


class AnalysisResponse(BaseModel):
    success: bool
    items: list[AnalysisItem] | None = None
    droppedCount: int = 0
    truncated: bool = False  # max_output_tokens 한도로 응답이 잘려 일부 항목만 복구됐는지
    tokensIn: int | None = None
    tokensOut: int | None = None
    latencyMs: int | None = None
    errorCode: str | None = None
    errorMessage: str | None = None


# =====================================================================
# DART 수집 스키마 — Spring이 /dart/collect 를 호출하고,
# Python이 DART API를 직접 호출해서 파싱한 결과를 JSON으로 돌려주면
# Spring이 그걸 stock/disclosure 테이블에 저장한다.
# =====================================================================

class DartCollectRequest(BaseModel):
    companyName: str        # 기업명(예: "삼성전자") 또는 종목코드(예: "005930")
    bgnDe: str              # 검색 시작일 (YYYYMMDD)
    endDe: str              # 검색 종료일 (YYYYMMDD)


class CorpInfo(BaseModel):
    """stock 테이블 1행에 대응. Spring이 이걸 받아서 stock에 UPSERT한다."""
    dartCorpCode: str       # DART 고유번호 (8자리)
    companyName: str        # DART 기업명
    stockCode: str | None   # 종목코드(비상장이면 null)
    marketType: str         # KOSPI / KOSDAQ / 비상장


class DisclosureItem(BaseModel):
    """disclosure 테이블 1행에 대응. Spring이 이걸 받아서 disclosure에 UPSERT한다."""
    rceptNo: str            # DART 접수번호 (14자리, PK)
    dartCorpCode: str       # stock.dart_corp_code FK
    typeCode: str           # disclosure_type.type_code — pblntf_ty/detail_ty 기반으로 매핑
    title: str              # 공시 제목
    filerName: str          # 제출인
    filedAt: str            # 공시일자 (YYYY-MM-DD)
    rawZipPath: str | None  # 원문 ZIP 저장 경로 (다운로드 후 채움, 초기엔 null)


class DartCollectResponse(BaseModel):
    success: bool
    corp: CorpInfo | None = None
    disclosures: list[DisclosureItem] = []
    totalCount: int = 0
    errorMessage: str | None = None


class TodayDisclosureItem(BaseModel):
    """
    회사 지정 없이 조회한 "오늘의 공시" 1건. DisclosureItem과 달리 stock 정보
    (companyName/stockCode/marketType)를 자체적으로 들고 있다 — 여러 회사가 섞여
    있어서 CorpInfo를 한 번만 받는 DartCollectResponse 구조를 쓸 수 없기 때문.
    """
    rceptNo: str
    dartCorpCode: str
    companyName: str
    stockCode: str | None
    marketType: str
    typeCode: str
    title: str
    filerName: str
    filedAt: str  # YYYY-MM-DD


class TodayDisclosuresResponse(BaseModel):
    success: bool
    items: list[TodayDisclosureItem] = []
    errorMessage: str | None = None


class TableCell(BaseModel):
    """
    표 셀 하나. rowSpan/colSpan은 DART 원문의 ROWSPAN/COLSPAN 속성을 그대로 보존한 값
    (기본값 1)이다 — 재무제표/실적표처럼 "매출액"이 당해실적·누계실적 두 행에 걸쳐 한 번만
    표시되는 경우를 무시하면 행마다 칸 수가 달라져 표가 어긋나 보이므로, 원문 병합 구조를
    그대로 <td rowSpan>/<td colSpan>으로 그릴 수 있게 프론트에 전달한다.
    """
    rowSpan: int = 1
    colSpan: int = 1
    blocks: list["DocumentBlock"]


class DocumentBlock(BaseModel):
    """
    공시 원문을 문단/표 단위로 구조화한 블록 하나.
    charStart/charEnd는 `text`(평문) 안에서 이 블록이 차지하는 문자 구간이며,
    기존 하이라이트 offset(analysisItems.charOffsetStart/End, termHighlights.startIndex/endIndex)과
    동일한 좌표계를 공유한다 — 프론트가 표 안에서도 하이라이트 구간을 매핑할 수 있게 하기 위함.
    offset을 복원하지 못한 블록은 charStart/charEnd가 None이며, 구조 표시에는 쓰이지만 하이라이트 대상에서는 제외된다.

    표 셀은 문자열이 아니라 TableCell(rowSpan/colSpan + DocumentBlock 목록)이다 — 사업/분기보고서
    각주처럼 표 안에 또 표가 중첩된 경우(예: 종속기업 현황 스케줄)를 문자열로 뭉개지 않고 실제
    중첩 표로 표현하기 위함이다. 보통은 문단 블록 하나짜리 목록이다.

    type == "heading"은 장/절 제목(DART 고유 XML의 <TITLE> 또는 "I."/"1."/"가." 번호 패턴)이며
    level(1~3)로 위계를 나타낸다 — 프론트가 시각적 위계·목차(TOC)를 그리는 데 쓰인다.
    본문 문단과 동일하게 text/charStart/charEnd를 가지므로 하이라이트 좌표계도 그대로 공유한다.
    """
    type: str  # "paragraph" | "heading" | "table"
    text: str | None = None                    # type == "paragraph" | "heading"
    level: int | None = None                   # type == "heading" (1=장, 2=절, 3=항)
    rows: list[list[TableCell]] | None = None  # type == "table" (row -> cell)
    charStart: int | None = None
    charEnd: int | None = None


DocumentBlock.model_rebuild()
TableCell.model_rebuild()


class DartDocumentResponse(BaseModel):
    """
    GET /dart/document/{rcept_no} 응답.
    DART document.xml(ZIP)을 받아 태그를 제거한 평문을 돌려준다.
    이 text가 그대로 압축->요약/압축->분석의 입력(dartContext/dartFullText)과 하이라이트 offset 계산 기준이 된다.
    blocks는 같은 내용을 문단/표 단위로 구조화한 것으로, 화면의 "공시 원문" 탭에서 표를 실제 표 형태로 그리는 데 쓰인다.
    """
    success: bool
    text: str | None = None
    blocks: list[DocumentBlock] | None = None
    errorMessage: str | None = None


# =====================================================================
# 용어사전 스키마 — 용어 마스터 데이터는 app/data/dictionary_terms.json 파일로 관리한다.
# Spring이 공시 원문을 이 서비스로 보내면, 등록된 용어를 찾아 위치와 함께 돌려준다.
# =====================================================================

class GlossaryRequest(BaseModel):
    originalText: str  # 용어를 찾을 공시 원문 평문


class GlossaryTermHighlight(BaseModel):
    termId: int
    term: str
    category: str
    definition: str  # raw_definition 전문
    startIndex: int
    endIndex: int


class GlossaryResponse(BaseModel):
    success: bool
    terms: list[GlossaryTermHighlight] = []
    errorMessage: str | None = None
