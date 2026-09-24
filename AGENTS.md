# darfin-disclosure

Stateless FastAPI microservice for **수시공시** DART collection, compression, and Gemini summarization/analysis. Spring Boot calls this over HTTP and handles all persistence.

## Commands

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# .env: DART_API_KEY, GEMINI_API_KEY (optional: GEMINI_MODEL, APP_HOST, APP_PORT)
uvicorn main:app --host 127.0.0.1 --port 8002 --reload
```

## Structure

```
main.py                 # FastAPI entry, route registration
config.py               # pydantic-settings (.env)
app/
  schemas.py            # Pydantic models (camelCase, matches Spring)
  registry.py           # type_code → PipelineSpec registry
  services.py           # run_summary, run_analysis (type-agnostic)
  gemini_client.py      # Single Gemini call point
  dart_collector.py     # DART Open API + XML→text
  glossary.py           # Term dictionary matching
  pipelines/            # Per-disclosure-type modules (12 types)
```

## Read before changing

| Area | Location |
|------|----------|
| API schemas | [app/schemas.py](app/schemas.py) |
| Pipeline registry | [app/registry.py](app/registry.py) |
| README (endpoints, extending) | [README.md](README.md) |
| Architecture | [../docs/architecture.md](../docs/architecture.md) |

## API endpoints

- `/dart/collect`, `/dart/document/{rcept_no}` — DART collection
- `/llm/summary`, `/llm/analysis` — Gemini processing
- `/glossary/terms` — term dictionary
- `/health` — health check

## Conventions

- **No database** — return JSON only; Spring handles UPSERT and `risk_tier` mapping
- Registry pattern: add pipeline in `app/pipelines/`, register in `registry.py`
- Each pipeline exports: `compress_for_summary`, `extract_analysis_chunks`, `build_analysis_input`, `resolve_offset`, plus prompt constants
- Unregistered `type_code` falls back to `generic_disclosure.py`
- JSON field naming: **camelCase** (`typeCode`, `corpName`) to match Spring
- Gemini client: single file `gemini_client.py`, retries on 503/429
- Risk labels: `Low` / `Neutral` / `High` / `Critical` — Spring normalizes to DB

## Do not

- Add database writes — persistence is `darfin-main`'s job
- Touch 정기공시 pipeline — that's `darfin-company-analysis`
- Change response shapes without coordinating Spring DTOs
- Commit `.env` with API keys

## Related repos

- `../darfin-main` — calls this service, stores results
- `../darfin-front` — `/disclosure` UI
- `../darfin-company-analysis` — separate 정기공시 pipeline
