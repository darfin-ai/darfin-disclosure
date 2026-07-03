"""
Gemini API 클라이언트.
요약/분석 두 서비스가 공통으로 쓰는 단일 호출 지점.
이 모듈은 DB를 전혀 모른다 — 입력(프롬프트)을 받아 Gemini를 호출하고
파싱된 JSON과 토큰/지연시간 메타데이터만 돌려준다.
"""
import json
import time
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from config import settings

_client = genai.Client(api_key=settings.gemini_api_key)

# 503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED 발생 시 재시도 설정
_MAX_RETRIES = 3          # 최대 재시도 횟수 (초기 1회 포함 최대 4회 호출)
_RETRY_BASE_DELAY = 5.0   # 첫 재시도 대기 시간(초) — 이후 2배씩 증가 (5 → 10 → 20)
_RETRYABLE_KEYWORDS = ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "high demand", "503", "429")


class GeminiCallError(Exception):
    """Gemini 호출 자체가 실패했거나 응답을 JSON으로 파싱할 수 없을 때."""


@dataclass
class GeminiResult:
    data: Any
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int


def call_gemini_json(
    system_instruction: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    max_output_tokens: int = 1500,
) -> GeminiResult:
    """
    Gemini를 호출하고 JSON 파싱 결과를 반환한다.
    503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED 는 지수 백오프로 최대 3회 재시도한다.
    재시도 대기: 5초 → 10초 → 20초
    """
    started = time.monotonic()
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(f"[Gemini 재시도 {attempt}/{_MAX_RETRIES}] {delay:.0f}초 대기 중... (원인: {last_exc})")
            time.sleep(delay)

        try:
            response = _client.models.generate_content(
                model=settings.gemini_model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                    # gemini-2.5 계열은 기본적으로 "thinking"에 출력 토큰 예산을 먼저 소비한다.
                    # thinking 출력이 max_output_tokens를 다 써버리면 실제 JSON 응답이 비거나
                    # 중간에 잘려(MAX_TOKENS) 파싱에 실패한다. 단순 추출/분류 작업이라
                    # thinking이 불필요하므로 꺼서 전체 예산을 응답에 쓰도록 한다.
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )

            latency_ms = int((time.monotonic() - started) * 1000)
            raw_text = (response.text or "").strip()
            parsed = _parse_json_response(raw_text)

            usage = getattr(response, "usage_metadata", None)
            tokens_in = getattr(usage, "prompt_token_count", None) if usage else None
            tokens_out = getattr(usage, "candidates_token_count", None) if usage else None

            return GeminiResult(
                data=parsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
            )

        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            is_retryable = any(kw in exc_str for kw in _RETRYABLE_KEYWORDS)

            if is_retryable and attempt < _MAX_RETRIES:
                continue  # 재시도

            # 재시도 불가 에러이거나 횟수 초과
            raise GeminiCallError(f"Gemini 호출 실패: {exc}") from exc

    raise GeminiCallError(f"Gemini 최대 재시도({_MAX_RETRIES}회) 초과: {last_exc}")


def _parse_json_response(raw_text: str) -> Any:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiCallError(
            f"Gemini 응답을 JSON으로 파싱할 수 없습니다: {exc}. raw={raw_text[:300]}"
        ) from exc
