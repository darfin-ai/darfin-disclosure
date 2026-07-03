"""
사업보고서 분석(하이라이트) 전용 섹션 필터.
Java의 AnalysisSectionFilter를 그대로 포팅했다.

요약용 압축기(summary_compressor)와 다른 점:
  - 텍스트를 자르고 재배열하지 않는다(좌표 보존 필수)
  - "소제목 단위"로만 포함/제외를 결정한다(문장 내부 절단 없음)
  - 결과는 원문의 부분 문자열 그대로 -> char_offset 계산이 정확해야 함

적용 범위: DART 전체 상장사 사업보고서(표준 목차 양식 공통)
"""
import re
from dataclasses import dataclass

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
