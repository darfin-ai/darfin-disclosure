"""
요약/분석 핵심 로직.
이 모듈은 DB를 전혀 모른다 — Spring이 보내준 typeCode로 registry.py에서
해당 공시유형의 압축/프롬프트 묶음(PipelineSpec)을 찾아 실행하고,
JSON으로 변환된 결과만 돌려준다. 저장은 전부 Spring(JPA) 책임이다.

typeCode가 BIZ_REPORT든 RIGHTS_OFFERING이든 그 외 새로 등록되는 어떤 유형이든
이 함수들은 동일하게 동작한다 — 실제 분기는 registry.py가 담당한다.
"""
from app import registry
from config import settings
from app.gemini_client import GeminiCallError, call_gemini_json
from app.schemas import (
    AnalysisItem,
    AnalysisRequest,
    AnalysisResponse,
    SummaryRequest,
    SummaryResponse,
)

MIN_CONTEXT_CHARS = 100


def run_summary(req: SummaryRequest) -> SummaryResponse:
    """레지스트리에서 typeCode에 맞는 spec을 찾아 압축 -> Gemini 호출. 저장하지 않음."""
    try:
        spec = registry.get_spec(req.typeCode)
    except KeyError as exc:
        return SummaryResponse(success=False, errorCode="PIPELINE_NOT_REGISTERED", errorMessage=str(exc), modelName=settings.gemini_model)

    if not req.dartContext or len(req.dartContext.strip()) < MIN_CONTEXT_CHARS:
        return SummaryResponse(
            success=False, errorCode="TEXT_TOO_SHORT",
            errorMessage=f"dartContext가 너무 짧습니다(최소 {MIN_CONTEXT_CHARS}자)",
            modelName=settings.gemini_model,
        )

    compressed = spec.compress_for_summary(req.dartContext)
    if not compressed.strip():
        return SummaryResponse(
            success=False, errorCode="NO_HEADINGS_FOUND",
            errorMessage="압축 결과가 비어 있습니다(항목/섹션을 찾지 못함)",
            modelName=settings.gemini_model,
        )

    corp_name = req.corpName or "해당 기업"
    user_prompt = spec.summary_user_prompt_template.format(corp_name=corp_name, context=compressed)

    try:
        result = call_gemini_json(spec.summary_system_instruction, user_prompt, temperature=0.2, max_output_tokens=1000)
    except GeminiCallError as exc:
        return SummaryResponse(success=False, errorCode="GEMINI_TIMEOUT", errorMessage=str(exc), modelName=settings.gemini_model)

    data = result.data
    if not isinstance(data, dict) or not all(k in data for k in ("summary_text", "investor_comment", "overall_risk")):
        return SummaryResponse(
            success=False, errorCode="INVALID_RESPONSE_SHAPE",
            errorMessage=f"Gemini 응답 형식이 예상과 다릅니다: {data!r}"[:300],
            modelName=settings.gemini_model,
        )

    overall_risk = data["overall_risk"]
    if spec.risk_labels and overall_risk not in spec.risk_labels:
        return SummaryResponse(
            success=False, errorCode="INVALID_RISK_LABEL",
            errorMessage=f"'{overall_risk}'는 이 유형({req.typeCode})에 정의된 risk_labels에 없습니다: {spec.risk_labels}",
            modelName=settings.gemini_model,
        )

    return SummaryResponse(
        success=True,
        summaryText=data["summary_text"],
        investorComment=data["investor_comment"],
        overallRisk=overall_risk,
        modelName=settings.gemini_model,
        tokensIn=result.tokens_in,
        tokensOut=result.tokens_out,
        latencyMs=result.latency_ms,
    )


def run_analysis(req: AnalysisRequest) -> AnalysisResponse:
    """
    레지스트리에서 typeCode에 맞는 spec을 찾아 좌표보존 압축 -> Gemini 호출 -> 오프셋 역산.
    targetKey가 원문과 한 글자라도 다르면(drift) 그 항목은 버리고 droppedCount만 늘린다.
    """
    try:
        spec = registry.get_spec(req.typeCode)
    except KeyError as exc:
        return AnalysisResponse(success=False, errorCode="PIPELINE_NOT_REGISTERED", errorMessage=str(exc))

    if not req.dartFullText or len(req.dartFullText.strip()) < MIN_CONTEXT_CHARS:
        return AnalysisResponse(
            success=False, errorCode="TEXT_TOO_SHORT",
            errorMessage=f"dartFullText가 너무 짧습니다(최소 {MIN_CONTEXT_CHARS}자)",
        )

    chunks = spec.extract_analysis_chunks(req.dartFullText)
    if not chunks:
        return AnalysisResponse(
            success=False, errorCode="NO_HEADINGS_FOUND",
            errorMessage="분석 대상 항목/소제목을 원문에서 찾지 못했습니다.",
        )

    gemini_input = spec.build_analysis_input(chunks)
    user_prompt = spec.analysis_user_prompt_template.format(context=gemini_input)

    try:
        result = call_gemini_json(spec.analysis_system_instruction, user_prompt, temperature=0.1, max_output_tokens=1500)
    except GeminiCallError as exc:
        return AnalysisResponse(success=False, errorCode="GEMINI_TIMEOUT", errorMessage=str(exc))

    raw_items = result.data
    if not isinstance(raw_items, list):
        return AnalysisResponse(
            success=False, errorCode="INVALID_RESPONSE_SHAPE",
            errorMessage=f"분석 응답은 배열이어야 합니다: {raw_items!r}"[:300],
        )

    items: list[AnalysisItem] = []
    dropped = 0

    for node in raw_items:
        if not isinstance(node, dict):
            dropped += 1
            continue

        target_key = node.get("targetKey", "")
        category = node.get("analysisCategory", "")
        risk_level = node.get("riskLevel", "Neutral")

        if not target_key:
            dropped += 1
            continue
        if spec.analysis_categories and category not in spec.analysis_categories:
            dropped += 1
            continue
        if spec.risk_labels and risk_level not in spec.risk_labels:
            dropped += 1
            continue

        offset = spec.resolve_offset(chunks, target_key)
        if offset == -1:
            dropped += 1
            continue

        items.append(
            AnalysisItem(
                analysisCategory=category,
                targetKey=target_key,
                materialImpact=node.get("materialImpact", ""),
                riskLevel=risk_level,
                charOffsetStart=offset,
                charOffsetEnd=offset + len(target_key),
            )
        )

    if not items:
        return AnalysisResponse(
            success=False, errorCode="ALL_ITEMS_DROPPED", droppedCount=dropped,
            errorMessage="모든 분석 항목이 검증에 실패해 버려졌습니다",
        )

    return AnalysisResponse(
        success=True,
        items=items,
        droppedCount=dropped,
        truncated=result.truncated,
        tokensIn=result.tokens_in,
        tokensOut=result.tokens_out,
        latencyMs=result.latency_ms,
    )
