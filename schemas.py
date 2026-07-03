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


class DartDocumentResponse(BaseModel):
    """
    GET /dart/document/{rcept_no} 응답.
    DART document.xml(ZIP)을 받아 태그를 제거한 평문을 돌려준다.
    이 text가 화면의 "공시 원문" 탭에 표시되고, 그대로 압축->요약/압축->분석의 입력(dartContext/dartFullText)이 된다.
    """
    success: bool
    text: str | None = None
    errorMessage: str | None = None
