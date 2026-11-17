"""Unit tests for the deterministic parts: contract, guardrails, verifier, router, breaker.
Run: python -m pytest -q tests"""
import asyncio
import copy

import pytest
from pydantic import ValidationError

from claimtriage.agents.router import route_claim
from claimtriage.agents.verifier import quote_found, verify_draft
from claimtriage.common import guardrails
from claimtriage.common.contracts import FNOL, cross_field_errors
from claimtriage.common.resilience import CircuitBreaker, CircuitOpenError, RetryableError, retry_async
from claimtriage.evaluation.scenarios import build, load

BASE, SCEN = load()
BY = {s["id"]: s for s in SCEN}


def fnol(sid="S01"):
    return build(BASE, BY[sid], "FNOL-1234567")


# ---------------------------------------------------------------- contract
def test_valid_fnol_passes_schema_and_cross_field():
    f = FNOL.model_validate(fnol())
    assert cross_field_errors(f) == []


def test_schema_rejects_bad_registration_and_phone():
    with pytest.raises(ValidationError) as e:
        FNOL.model_validate(fnol("S16"))
    locs = {".".join(map(str, x["loc"])) for x in e.value.errors()}
    assert {"vehicle.registration_number", "claimant.phone"} <= locs


def test_schema_rejects_unknown_fields_and_bad_enum():
    d = fnol()
    d["incident"]["injuries"] = "a bit"
    d["surprise"] = 1
    with pytest.raises(ValidationError):
        FNOL.model_validate(d)


def test_cross_field_rules():
    errs = cross_field_errors(FNOL.model_validate(fnol("S17")))
    types = {e["type"] for e in errs}
    assert "cross_field.report_before_loss" in types and "cross_field.fir_required" in types


# ---------------------------------------------------------------- guardrails
def test_injection_detected():
    r = guardrails.detect_injection({"d": BY["S15"]["overrides"]["incident"]["description"]})
    assert not r.ok and {f["rule"] for f in r.findings} & {"override", "dictate_outcome"}


def test_benign_text_not_flagged():
    r = guardrails.detect_injection({"d": BY["S03"]["overrides"]["incident"]["description"]})
    assert r.ok


def test_toxicity_and_mask():
    text = BY["S24"]["overrides"]["incident"]["description"]
    score, hits = guardrails.toxicity_score(text)
    assert score >= guardrails.TOXICITY_THRESHOLD and hits
    assert "idiot" not in guardrails.mask_toxic(text).lower()


def test_output_filter_blocks_determinations():
    out, rules = guardrails.filter_output("The cover applies. The claim is approved. We will pay Rs 40,000 settlement.")
    assert "approved" not in out and rules


# ---------------------------------------------------------------- verifier
RET = {
    "PW-S5-001": {"text": "3 any accidental loss ... whilst the vehicle insured herein is\na. being used otherwise than "
                          "in accordance with the 'Limitations as to Use'", "dense_score": 0.5},
    "PR-S0-001": {"text": "covers Vehicles used for social, domestic and pleasure purpose ... but excluding use for "
                          "hire or reward, racing", "dense_score": 0.6},
}


def test_quote_found_tolerates_whitespace_and_quotes():
    assert quote_found("being used otherwise than in accordance with the ‘Limitations as to Use’",
                       RET["PW-S5-001"]["text"])
    assert not quote_found("the company will pay all delivery accidents in full", RET["PW-S5-001"]["text"])


def test_verifier_requires_term_resolution():
    draft = {"coverage_position": "likely_not_covered", "rationale": "x",
             "citations": [{"clause_id": "PW-S5-001",
                            "quote": "being used otherwise than in accordance with the 'Limitations as to Use'"}]}
    v = verify_draft(draft, RET, [{"query": "loss or damage"}], ["PW-S1-001"], {"commercial_use_indicated": True})
    assert not v["passed"] and {f["check"] for f in v["failures"]} >= {"V6_term_resolved"}
    draft["citations"].append({"clause_id": "PR-S0-001", "quote": "excluding use for hire or reward"})
    v = verify_draft(draft, RET, [{"query": "Limitations as to Use hire or reward"}], ["PW-S1-001"],
                     {"commercial_use_indicated": True})
    assert v["passed"], v["failures"]


def test_verifier_rejects_unretrieved_citation():
    draft = {"coverage_position": "likely_covered", "rationale": "x",
             "citations": [{"clause_id": "PW-S9-999", "quote": "invented text here today"}]}
    v = verify_draft(draft, RET, [{"query": "exclusion"}], [], {})
    assert not v["passed"] and v["failures"][0]["check"] == "V3_citation_retrieved"


# ---------------------------------------------------------------- router
def test_router_paths():
    facts = {"estimated_repair_cost_inr": 18000, "cashless_requested": True, "estimate_to_idv_ratio": 0.03}
    assert route_claim("own_damage", {}, facts, {"coverage_position": "likely_covered"})[0] == "fast_track"
    assert route_claim("own_damage", {}, {**facts, "estimated_repair_cost_inr": 90000},
                       {"coverage_position": "likely_covered"})[0] == "surveyor"
    assert route_claim("own_damage", {}, facts, {"coverage_position": "likely_not_covered"})[0] == "investigation"
    assert route_claim("theft", {}, facts, {"coverage_position": "likely_covered"})[0] == "investigation"


# ---------------------------------------------------------------- resilience
def test_breaker_opens_and_half_opens():
    b = CircuitBreaker(failure_threshold=2, reset_s=0.05)
    for _ in range(2):
        b.before_call()
        b.record_failure()
    with pytest.raises(CircuitOpenError):
        b.before_call()
    import time
    time.sleep(0.06)
    b.before_call()  # half-open trial
    b.record_success()
    assert b.state == "closed"


def test_retry_backoff_bounded():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        raise RetryableError("x")

    with pytest.raises(RetryableError):
        asyncio.run(retry_async(flaky, attempts=3, base_delay_s=0.001))
    assert calls["n"] == 3
