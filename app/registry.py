"""
공시유형(type_code)별 압축 기준 + 요약/분석 프롬프트 레지스트리.

DART에서 가져오는 문서는 사업보고서 하나가 아니라 정기공시·주요사항보고서·
지분공시·기타공시 등 90개+ 유형이 있다. 이 모듈은 type_code 하나당
"요약용 압축 + 분석용 압축 + 요약 프롬프트 + 분석 프롬프트"를 한 묶음(PipelineSpec)으로
등록해서, services.py가 어떤 문서가 들어오든 같은 흐름(레지스트리 조회 -> 압축 -> Gemini)
으로 처리할 수 있게 한다.
"""
from dataclasses import dataclass
from typing import Callable

from app.pipelines import abs_disclosure
from app.pipelines import audit_report
from app.pipelines import business_report
from app.pipelines import equity
from app.pipelines import exchange_disclosure
from app.pipelines import fund_disclosure
from app.pipelines import ftc_disclosure
from app.pipelines import generic_disclosure
from app.pipelines import issuance
from app.pipelines import major_event
from app.pipelines import other_disclosure
from app.pipelines import rights_offering

# 전용 모듈이 없는 type_code는 get_spec()에서 이 키로 등록된 범용 파이프라인으로 폴백한다.
GENERIC_TYPE_CODE = "GENERIC"


@dataclass(frozen=True)
class PipelineSpec:
    """한 공시유형(type_code)의 압축 기준 + 프롬프트 한 세트."""

    type_code: str

    compress_for_summary: Callable[[str], str]
    extract_analysis_chunks: Callable[[str], dict]
    build_analysis_input: Callable[[dict], str]
    resolve_offset: Callable[[dict, str], int]

    summary_system_instruction: str
    summary_user_prompt_template: str  # {corp_name}, {context} 두 자리

    analysis_system_instruction: str
    analysis_user_prompt_template: str  # {context} 한 자리

    analysis_categories: tuple[str, ...] = ()
    risk_labels: tuple[str, ...] = ()


_REGISTRY: dict[str, PipelineSpec] = {}


def register(spec: PipelineSpec) -> None:
    _REGISTRY[spec.type_code] = spec


def get_spec(type_code: str) -> PipelineSpec:
    spec = _REGISTRY.get(type_code)
    if spec is not None:
        return spec

    generic_spec = _REGISTRY.get(GENERIC_TYPE_CODE)
    if generic_spec is not None:
        return generic_spec

    raise KeyError(
        f"'{type_code}' 유형의 파이프라인이 registry.py에 등록되어 있지 않습니다. "
        f"PipelineSpec을 추가하고 register()로 등록하세요."
    )


def list_registered_type_codes() -> list[str]:
    return sorted(_REGISTRY.keys())


def _register_simple(module, type_code: str, analysis_categories: tuple[str, ...]) -> None:
    """4함수+4상수 인터페이스를 갖는 모듈을 PipelineSpec으로 등록하는 공통 헬퍼.
    위험라벨(risk_labels)은 모든 모듈이 동일한 Low/Neutral/High/Critical 4단계를 쓰므로 고정한다."""
    register(
        PipelineSpec(
            type_code=type_code,
            compress_for_summary=module.compress_for_summary,
            extract_analysis_chunks=module.extract_analysis_chunks,
            build_analysis_input=module.build_analysis_input,
            resolve_offset=module.resolve_offset,
            summary_system_instruction=module.SUMMARY_SYSTEM_INSTRUCTION,
            summary_user_prompt_template=module.SUMMARY_USER_PROMPT_TEMPLATE,
            analysis_system_instruction=module.ANALYSIS_SYSTEM_INSTRUCTION,
            analysis_user_prompt_template=module.ANALYSIS_USER_PROMPT_TEMPLATE,
            analysis_categories=analysis_categories,
            risk_labels=("Low", "Neutral", "High", "Critical"),
        )
    )


# =====================================================================
# 등록: 사업보고서 (BIZ_REPORT) — 정기공시
# =====================================================================
_register_simple(
    business_report, "BIZ_REPORT",
    ("Market_Competitiveness", "Financial_Health", "Risk_Exposure", "Governance", "Growth_Potential"),
)

# =====================================================================
# 등록: 유상증자결정 (RIGHTS_OFFERING) — 주요사항보고서 중 고빈도 유형
# =====================================================================
_register_simple(
    rights_offering, "RIGHTS_OFFERING",
    ("Dilution_Risk", "Funding_Purpose", "Pricing_Fairness", "Major_Shareholder_Impact"),
)

# =====================================================================
# 등록: 주요사항보고서 일반 (MAJOR_EVENT) — RIGHTS_OFFERING을 제외한 나머지
# =====================================================================
_register_simple(
    major_event, "MAJOR_EVENT",
    ("Decision_Scale", "Counterparty_Risk", "Financial_Impact", "Execution_Risk"),
)

# =====================================================================
# 등록: 발행공시 (ISSUANCE) — 증권신고서·투자설명서
# =====================================================================
_register_simple(
    issuance, "ISSUANCE",
    ("Offering_Terms", "Investment_Risk", "Use_Of_Proceeds"),
)

# =====================================================================
# 등록: 지분공시 (EQUITY) — 대량보유상황보고서·임원 및 주요주주 소유보고서
# =====================================================================
_register_simple(
    equity, "EQUITY",
    ("Holding_Purpose", "Governance_Impact", "Market_Signal"),
)

# =====================================================================
# 등록: 기타공시 (OTHER) — 자기주식취득/처분, 잠정실적, 조회공시 답변 등
# =====================================================================
_register_simple(
    other_disclosure, "OTHER",
    ("Key_Fact", "Market_Impact", "Uncertainty"),
)

# =====================================================================
# 등록: 외부감사관련 (AUDIT) — 감사보고서·연결감사보고서
# =====================================================================
_register_simple(
    audit_report, "AUDIT",
    ("Audit_Opinion", "Key_Audit_Risk", "Internal_Control"),
)

# =====================================================================
# 등록: 펀드공시 (FUND) — 집합투자규약·투자설명서
# =====================================================================
_register_simple(
    fund_disclosure, "FUND",
    ("Strategy_Risk", "Fee_Reasonableness", "Redemption_Protection"),
)

# =====================================================================
# 등록: 자산유동화 (ABS)
# =====================================================================
_register_simple(
    abs_disclosure, "ABS",
    ("True_Sale_Risk", "Credit_Enhancement", "Asset_Quality"),
)

# =====================================================================
# 등록: 거래소공시 (EXCHANGE) — 불성실공시법인·관리종목·매매거래정지 등
# =====================================================================
_register_simple(
    exchange_disclosure, "EXCHANGE",
    ("Action_Severity", "Trading_Impact", "Listing_Risk"),
)

# =====================================================================
# 등록: 공정위공시 (FTC) — 대규모내부거래 등 기업집단 공시
# =====================================================================
_register_simple(
    ftc_disclosure, "FTC",
    ("Transaction_Fairness", "Approval_Process", "Tunneling_Risk"),
)

# =====================================================================
# 등록: 범용(GENERIC) — 위 10개 대분류 중 그 어디에도 type_code가 등록되지 않은 경우의 최종 폴백
# =====================================================================
register(
    PipelineSpec(
        type_code=GENERIC_TYPE_CODE,
        compress_for_summary=generic_disclosure.compress_for_summary,
        extract_analysis_chunks=generic_disclosure.extract_analysis_chunks,
        build_analysis_input=generic_disclosure.build_analysis_input,
        resolve_offset=generic_disclosure.resolve_offset,
        summary_system_instruction=generic_disclosure.SUMMARY_SYSTEM_INSTRUCTION,
        summary_user_prompt_template=generic_disclosure.SUMMARY_USER_PROMPT_TEMPLATE,
        analysis_system_instruction=generic_disclosure.ANALYSIS_SYSTEM_INSTRUCTION,
        analysis_user_prompt_template=generic_disclosure.ANALYSIS_USER_PROMPT_TEMPLATE,
        risk_labels=("Low", "Neutral", "High", "Critical"),
        # analysis_categories는 비워둔다 — 범용 유형은 카테고리를 자유 문자열로 받고 검증을 건너뛴다.
    )
)
