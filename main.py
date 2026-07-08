"""
Darfin LLM Pipeline Service.

역할: 압축 + Gemini 호출/JSON 응답, DART 원문 수집, 용어사전 매칭. 이게 전부다.
이 서비스는 (Gemini 호출용 설정을 빼면) DB를 전혀 모른다(SQLAlchemy도, 어떤 DB
드라이버도 사용하지 않음). 용어사전 마스터 데이터조차 DB가 아니라
app/data/dictionary_terms.json 파일로 관리한다.
Spring Boot가 이 서비스를 호출해 JSON 응답을 받고, risk_tier 비정규화와
DB(ai_summary_result/ai_analysis_item) 저장은 전부 Spring(JPA) 쪽에서 처리한다.

DART에서 가져오는 문서는 사업보고서 하나가 아니라 90개+ 공시유형이 있으므로,
요청에 typeCode를 함께 보내면 registry.py가 그 유형에 맞는 압축/프롬프트
묶음을 선택해서 실행한다. 새 유형을 추가하려면 registry.py를 참고하라.

실행:
    uvicorn main:app --host 127.0.0.1 --port 8002
"""
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app import dart_collector, glossary, registry
from config import settings
from app.schemas import (
    AnalysisRequest, AnalysisResponse,
    DartCollectRequest, DartCollectResponse,
    DartDocumentResponse,
    GlossaryRequest, GlossaryResponse,
    SummaryRequest, SummaryResponse,
    TodayDisclosuresResponse,
)
from app.services import run_analysis, run_summary

app = FastAPI(
    title="Darfin LLM Pipeline Service",
    description="DART 공시 수집(파싱) + 압축 + Gemini 요약/분석 + 용어사전. DB 접근 없음(Spring이 저장 담당)",
    version="0.4.0",
)


@app.post("/llm/summary", response_model=SummaryResponse)
def summarize(req: SummaryRequest) -> SummaryResponse:
    return run_summary(req)


@app.post("/llm/analysis", response_model=AnalysisResponse)
def analyze(req: AnalysisRequest) -> AnalysisResponse:
    return run_analysis(req)


@app.post("/glossary/terms", response_model=GlossaryResponse)
def extract_glossary_terms(req: GlossaryRequest) -> GlossaryResponse:
    """
    공시 원문에서 app/data/dictionary_terms.json에 등록된 전문용어를 찾아
    위치(startIndex/endIndex)와 함께 반환한다. 용어 추가/수정은 그 JSON 파일을
    직접 편집하면 된다(DB 없음, 캐싱 없음 — 매 요청마다 다시 계산한다).
    """
    terms = glossary.extract_term_highlights(req.originalText)
    return GlossaryResponse(success=True, terms=terms)


@app.post("/dart/collect", response_model=DartCollectResponse)
def collect(req: DartCollectRequest) -> DartCollectResponse:
    """
    DART API에서 기업정보 + 공시목록을 가져와 JSON으로 반환.
    Spring이 이 응답을 받아서 stock / disclosure 테이블에 UPSERT한다.
    """
    return dart_collector.collect(req)


@app.get("/dart/today-disclosures", response_model=TodayDisclosuresResponse)
def get_today_disclosures(limit: int = 6) -> TodayDisclosuresResponse:
    """
    회사 지정 없이 오늘 접수된 전체 공시 중 최신순 상위 N건을 반환한다.
    DisclosureSearch.jsx 검색창 아래 "오늘 올라온 공시" 피드가 주기적으로 호출한다.
    """
    try:
        items = dart_collector.fetch_today_disclosures(limit)
    except Exception as exc:
        return TodayDisclosuresResponse(success=False, errorMessage=f"오늘의 공시 조회 실패: {exc}")

    return TodayDisclosuresResponse(success=True, items=items)


@app.get("/dart/document/{rcept_no}", response_model=DartDocumentResponse)
def get_document(rcept_no: str) -> DartDocumentResponse:
    """
    DART 공시 원문(document.xml) 평문을 반환한다.
    DisclosureViewer.jsx 좌측 "공시 원문" 탭, 그리고 요약/분석 압축 단계 입력으로 그대로 쓰인다.
    """
    try:
        text, blocks = dart_collector.fetch_document(rcept_no)
    except Exception as exc:
        return DartDocumentResponse(success=False, errorMessage=f"공시 원문 조회 실패: {exc}")

    if not text.strip():
        return DartDocumentResponse(success=False, errorMessage="원문 텍스트를 추출하지 못했습니다.")

    return DartDocumentResponse(success=True, text=text, blocks=blocks)


@app.get("/dart/document/{rcept_no}/zip")
def download_document_zip(rcept_no: str):
    """
    DART 공시 원문 ZIP 파일을 그대로 스트리밍한다.
    Spring GET /api/disclosures/{rceptNo}/download-zip 이 이 엔드포인트를 프록시해서
    브라우저로 내려보낸다.

    DART document.xml API는 ZIP(HWP 변환 XML 포함)을 반환하며, PDF는 API로 제공하지 않는다.
    DART 공식 뷰어(HWP/PDF)는 https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no} 에서 확인 가능.
    """
    try:
        zip_bytes = dart_collector.fetch_document_zip(rcept_no)
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=f"DART 원문 ZIP 다운로드 실패: {exc}")

    filename = f"{rcept_no}.zip"
    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/dart/debug/list")
def debug_dart_list(corp_code: str, bgn_de: str = "20260101", end_de: str = "20260630"):
    """
    DART list.json 원본 응답을 그대로 반환한다.
    pblntf_ty 값이 실제로 뭐가 오는지 확인용.
    예: GET /dart/debug/list?corp_code=00126380
    """
    import httpx
    from config import settings
    resp = httpx.get(
        "https://opendart.fss.or.kr/api/list.json",
        params={
            "crtfc_key": settings.dart_api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": 1,
            "page_count": 20,
        },
        timeout=15,
    )
    data = resp.json()
    rows = data.get("list", [])
    sample = [
        {
            "rcept_no": r.get("rcept_no"),
            "report_nm": r.get("report_nm"),
            "pblntf_ty": r.get("pblntf_ty"),
            "pblntf_detail_ty": r.get("pblntf_detail_ty"),
        }
        for r in rows[:20]
    ]
    return {
        "status": data.get("status"),
        "total_count": data.get("total_count"),
        "sample": sample,
    }


@app.get("/llm/registered-types")
def registered_types():
    return {"typeCodes": registry.list_registered_type_codes()}


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
