# Darfin LLM Pipeline Service

DART(전자공시시스템) 공시 문서를 수집하고, Gemini로 요약·분석하는 FastAPI 서비스입니다.
Spring Boot 백엔드가 이 서비스를 HTTP로 호출해 결과를 받아가는 구조이며, 이 서비스는 **DB를 전혀 모릅니다**.
DB 저장(UPSERT), `risk_tier` 등 값 정규화는 전부 Spring(JPA) 쪽 책임이고, 이 서비스는 다음만 담당합니다.

1. DART Open API 호출(수집)
2. 공시 원문 압축(토큰 절약)
3. Gemini 호출 및 JSON 파싱
4. 결과를 그대로 JSON으로 반환

## 디렉터리 구조

```
darfin-disclosure/
├── main.py                  # FastAPI 엔트리포인트 — 라우터 등록만 담당
├── config.py                # pydantic-settings 기반 환경설정 (.env 로드)
├── requirements.txt
├── .env                      # 실제 API 키(DART_API_KEY, GEMINI_API_KEY) — git에 커밋되지 않음
│
├── app/                     # 서비스 핵심 로직
│   ├── schemas.py           # Spring ↔ Python 요청/응답 pydantic 모델
│   ├── registry.py          # type_code -> PipelineSpec(압축+프롬프트) 레지스트리
│   ├── services.py          # 요약/분석 실행 로직 (run_summary, run_analysis)
│   ├── gemini_client.py      # Gemini API 단일 호출 지점 (재시도/타임아웃 처리)
│   ├── dart_collector.py     # DART Open API 호출 + 공시 원문 XML -> 평문 변환
│   │
│   └── pipelines/           # 공시유형(type_code)별 압축 기준 + 프롬프트 모듈
│       ├── business_report.py       # BIZ_REPORT      정기공시(사업/반기/분기보고서)
│       ├── rights_offering.py       # RIGHTS_OFFERING  유상증자결정
│       ├── major_event.py           # MAJOR_EVENT      주요사항보고서 일반
│       ├── issuance.py              # ISSUANCE         증권신고서·투자설명서
│       ├── equity.py                # EQUITY           대량보유·임원 소유보고 등 지분공시
│       ├── other_disclosure.py      # OTHER            자기주식·잠정실적·조회공시 등 기타공시
│       ├── audit_report.py          # AUDIT            감사보고서·연결감사보고서
│       ├── fund_disclosure.py       # FUND             집합투자규약·투자설명서(펀드)
│       ├── abs_disclosure.py        # ABS              자산유동화
│       ├── exchange_disclosure.py   # EXCHANGE         불성실공시·관리종목·매매거래정지 등
│       ├── ftc_disclosure.py        # FTC              공정위(대규모내부거래 등 기업집단) 공시
│       └── generic_disclosure.py    # GENERIC          위 어디에도 해당 안 되는 유형의 최종 폴백
```

## 핵심 설계: type_code 기반 레지스트리 패턴

DART 공시는 사업보고서 하나가 아니라 **90개+ 유형**이 있습니다. 이를 처리하기 위해
`app/registry.py`는 유형(`type_code`)마다 "요약용 압축 + 분석용 압축 + 요약 프롬프트 + 분석 프롬프트"를
한 묶음(`PipelineSpec`)으로 등록해두고, `app/services.py`는 요청에 실린 `typeCode`로 이 묶음을 조회해
실행할 뿐 유형별 분기 로직을 전혀 알 필요가 없습니다.

새 공시유형을 추가하려면:
1. `app/pipelines/`에 동일 인터페이스(`compress_for_summary`, `extract_analysis_chunks`,
   `build_analysis_input`, `resolve_offset`, `*_SYSTEM_INSTRUCTION`, `*_USER_PROMPT_TEMPLATE`)를
   갖는 모듈을 추가하고
2. `app/registry.py`에 `_register_simple(module, "TYPE_CODE", (...analysis_categories))`로 등록

## API 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| POST | `/dart/collect` | 기업명/종목코드로 DART 기업정보+공시목록 조회 |
| GET | `/dart/document/{rcept_no}` | 공시 원문(XML/HWP)을 평문으로 변환해 반환 |
| GET | `/dart/document/{rcept_no}/zip` | 공시 원문 ZIP 파일 스트리밍 다운로드 |
| POST | `/llm/summary` | typeCode에 맞는 압축 후 Gemini 요약 호출 |
| POST | `/llm/analysis` | typeCode에 맞는 좌표보존 압축 후 Gemini 분석 호출 |
| GET | `/llm/registered-types` | registry.py에 등록된 typeCode 목록 |
| GET | `/dart/debug/list` | DART list.json 원본 응답 디버그용 |
| GET | `/health` | 헬스체크 |

## 실행 방법

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# .env에 DART_API_KEY, GEMINI_API_KEY 확인/설정

uvicorn main:app --host 127.0.0.1 --port 8001
```

`config.py`는 프로세스 실행 위치(현재 디렉터리) 기준으로 `.env`를 읽으므로, 반드시 저장소 루트에서
실행해야 합니다(`main.py`, `config.py`도 같은 위치에 있습니다).
