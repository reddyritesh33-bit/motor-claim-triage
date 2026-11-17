"""Routing policy (F6): fast-track / surveyor / investigation, with reasons.

Deterministic on purpose: routing is an auditable business rule, not a model
judgement. Grounds come from the policy's own claim procedure (Section IX:
intimation timelines, proximity check, CTL threshold) and the coverage agent's
verified position.
"""
from __future__ import annotations

from ..common.config import get_settings


def route_claim(claim_type: str, entities: dict, facts: dict, coverage: dict) -> tuple[str, list[str]]:
    s = get_settings()
    pos = coverage.get("coverage_position", "indeterminate")
    inv: list[str] = []
    if pos == "likely_not_covered":
        inv.append("an exclusion or limitation of use appears to apply; the facts must be established "
                   "before a handler decides")
    if claim_type == "theft":
        inv.append("theft claim: police FIR and the circumstances of loss must be verified")
    if facts.get("proximity_case"):
        inv.append(f"loss occurred {facts.get('days_since_policy_start')} days after cover commenced "
                   "(Proximity Check: within 30 days)")
    if facts.get("late_intimation"):
        inv.append(f"intimated {facts.get('intimation_delay_hours')} h after the loss, beyond the "
                   f"{facts.get('intimation_limit_hours')} h allowed by the claim procedure")
    if facts.get("licence_valid_at_loss") is False:
        inv.append("driver's licence had expired before the date of loss")
    if facts.get("loss_within_policy_period") is False:
        inv.append("date of loss is outside the policy period")
    if entities.get("intoxication_indicated"):
        inv.append("description indicates alcohol or drugs")
    if inv:
        return "investigation", inv

    sur: list[str] = []
    ratio = facts.get("estimate_to_idv_ratio")
    if claim_type == "total_loss" or (ratio is not None and ratio >= 0.75):
        sur.append("possible constructive total loss (repair estimate at or above 75% of IDV)")
    if claim_type == "third_party" or entities.get("third_party_injury"):
        sur.append("third-party liability or injury involved")
    est = facts.get("estimated_repair_cost_inr")
    if est is None:
        sur.append("no repair estimate supplied")
    elif est > s.fast_track_max_estimate_inr:
        sur.append(f"repair estimate {est} INR exceeds the fast-track limit of {s.fast_track_max_estimate_inr} INR")
    if not facts.get("cashless_requested"):
        sur.append("not a cashless repair request")
    if pos in ("indeterminate",):
        sur.append("coverage position is indeterminate")
    if sur:
        return "surveyor", sur
    return "fast_track", [f"own-damage repair estimated at or below {s.fast_track_max_estimate_inr} INR, cashless "
                          "requested, coverage position verified, no investigation triggers"]
