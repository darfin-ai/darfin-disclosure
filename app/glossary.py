"""
용어사전(전문용어 하이라이트) 로직.
용어 마스터 데이터는 app/data/dictionary_terms.json 파일로 관리한다(DB 없음).
용어 추가/수정/삭제는 이 JSON 파일을 직접 편집하면 된다.

매칭 알고리즘: 원래 Spring DictionaryService의 로직을 그대로 포팅했다.
- 용어를 길이 내림차순으로 정렬해 긴 용어부터 매칭(짧은 용어가 긴 용어의 일부를
  가로채는 것을 방지, greedy matching)
- 이미 매칭된 문자 범위와 겹치면 건너뛴다(같은 자리에 중복 하이라이트 방지)
- 용어 하나당 원문에서 최대 1번만 하이라이트한다
캐싱은 하지 않는다 — 이 서비스는 DB가 없으므로 요청마다 다시 계산한다.
"""
import json
from pathlib import Path

from app.schemas import GlossaryTermHighlight

_DATA_PATH = Path(__file__).parent / "data" / "dictionary_terms.json"
_MAX_OCCURRENCES_PER_TERM = 1


def _load_terms_sorted_by_length_desc() -> list[dict]:
    with open(_DATA_PATH, encoding="utf-8") as f:
        terms = json.load(f)
    return sorted(terms, key=lambda t: len(t["term"]), reverse=True)


_TERMS_SORTED = _load_terms_sorted_by_length_desc()


def extract_term_highlights(original_text: str) -> list[GlossaryTermHighlight]:
    """원문에서 사전에 등록된 용어를 찾아 위치와 함께 반환한다(원문 내 위치 순 정렬)."""
    if not original_text or not original_text.strip():
        return []

    text_len = len(original_text)
    matched = bytearray(text_len)
    result: list[GlossaryTermHighlight] = []

    for term_entry in _TERMS_SORTED:
        term_str = term_entry["term"]
        if not term_str or not term_str.strip():
            continue

        occurrences = 0
        from_idx = 0
        while from_idx < text_len and occurrences < _MAX_OCCURRENCES_PER_TERM:
            found = original_text.find(term_str, from_idx)
            if found == -1:
                break

            end = found + len(term_str)
            if not any(matched[found:end]):
                for i in range(found, end):
                    matched[i] = 1
                result.append(
                    GlossaryTermHighlight(
                        termId=term_entry["id"],
                        term=term_str,
                        category=term_entry["category"],
                        definition=term_entry["definition"],
                        startIndex=found,
                        endIndex=end,
                    )
                )
                occurrences += 1

            from_idx = found + 1

    result.sort(key=lambda h: h.startIndex)
    return result
