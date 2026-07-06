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
    truncated: bool = False


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

            try:
                parsed = _parse_json_response(raw_text)
                truncated = False
            except GeminiCallError:
                # 구독 등급별 max_output_tokens 제한에 걸려 배열이 중간에 잘린 경우,
                # 전체를 실패 처리하지 않고 완전히 닫힌 항목까지만 살려서 부분 결과로 반환한다.
                if not _is_max_tokens_truncated(response):
                    raise
                salvaged = _salvage_partial_json_array(raw_text)
                if salvaged is None:
                    raise
                print(f"[Gemini 응답 잘림] finish_reason=MAX_TOKENS — 완전한 항목 {len(salvaged)}개만 복구")
                parsed = salvaged
                truncated = True

            usage = getattr(response, "usage_metadata", None)
            tokens_in = getattr(usage, "prompt_token_count", None) if usage else None
            tokens_out = getattr(usage, "candidates_token_count", None) if usage else None

            return GeminiResult(
                data=parsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                truncated=truncated,
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


def _is_max_tokens_truncated(response: Any) -> bool:
    """candidates[0].finish_reason이 MAX_TOKENS인지 확인한다(응답이 토큰 한도로 잘렸는지)."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return False
    finish_reason = getattr(candidates[0], "finish_reason", None)
    return finish_reason is not None and "MAX_TOKENS" in str(finish_reason)


def _salvage_partial_json_array(raw_text: str) -> list[Any] | None:
    """
    Gemini 응답이 JSON 배열 중간에 잘렸을 때, 마지막으로 완전히 닫힌 최상위
    객체까지만 잘라내 유효한 배열로 복구한다. 배열이 아니거나 완전한 객체가
    하나도 없으면 None을 반환한다(호출 측에서 원래 에러를 그대로 던지게 함).
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    if not text.startswith("["):
        return None

    depth = 0
    in_string = False
    escape = False
    last_complete_end: int | None = None

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            # depth == 1 -> 배열([) 바로 안쪽에서 항목({...})이 완전히 닫힌 지점
            if ch == "}" and depth == 1:
                last_complete_end = i + 1

    if last_complete_end is None:
        return None

    try:
        result = json.loads(text[:last_complete_end] + "]")
    except json.JSONDecodeError:
        return None

    return result if isinstance(result, list) else None


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
