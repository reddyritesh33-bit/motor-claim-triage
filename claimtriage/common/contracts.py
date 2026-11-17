"""Published data contracts (D1). The JSON Schema in contracts/ is generated
from these models (`python -m claimtriage.tools.export_contracts`).

FNOL contract v1.0: schema + type + range + category checks are declared on the
fields; cross-field rules live in `cross_field_errors` and run at the ingestion
boundary before any agent starts.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "1.0"

REG_PATTERN = re.compile(
    r"^([A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{0,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-Z]{1,2})$"
)


class Channel(str, Enum):
    call_centre = "call_centre"
    app = "app"
    web = "web"


class VehicleClass(str, Enum):
    private_car = "private_car"
    two_wheeler = "two_wheeler"
    commercial_goods = "commercial_goods"
    commercial_passenger = "commercial_passenger"


class FuelType(str, Enum):
    petrol = "petrol"
    diesel = "diesel"
    cng = "cng"
    electric = "electric"
    hybrid = "hybrid"


class Injuries(str, Enum):
    none = "none"
    minor = "minor"
    serious = "serious"
    fatal = "fatal"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Policy(Strict):
    policy_number: str = Field(pattern=r"^[A-Z0-9/-]{6,30}$")
    product_uin: str = Field(min_length=8, max_length=40)
    policy_start_date: date
    policy_end_date: date
    vehicle_class: VehicleClass
    idv_inr: int = Field(ge=10_000, le=50_000_000)
    addons: list[str] = Field(default_factory=list, max_length=20)


class Vehicle(Strict):
    registration_number: str
    make: str = Field(min_length=2, max_length=40)
    model: str = Field(min_length=1, max_length=60)
    year_of_manufacture: int = Field(ge=1980, le=2100)
    fuel_type: FuelType

    @field_validator("registration_number")
    @classmethod
    def _reg(cls, v: str) -> str:
        v = v.upper()
        if not REG_PATTERN.match(v):
            raise ValueError("not a valid Indian registration number (e.g. KA01AB1234 or 22BH1234AA)")
        return v


class Location(Strict):
    city: str = Field(min_length=2, max_length=60)
    state: str = Field(min_length=2, max_length=60)
    pincode: str = Field(pattern=r"^[1-9][0-9]{5}$")


class Incident(Strict):
    date_of_loss: datetime
    reported_at: datetime
    location: Location
    description: str = Field(min_length=20, max_length=4000)
    police_report_filed: bool
    fir_number: Optional[str] = Field(default=None, max_length=40)
    third_party_involved: bool
    injuries: Injuries
    vehicle_driveable: bool
    estimated_repair_cost_inr: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    cashless_requested: bool = False


class Driver(Strict):
    driver_is_insured: bool
    name: str = Field(min_length=2, max_length=80)
    licence_number: str = Field(pattern=r"^[A-Z]{2}[0-9]{2}[ -]?[0-9]{4}[0-9]{7}$|^[A-Z]{2}-?[0-9]{13}$")
    licence_valid_until: date


class Claimant(Strict):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(pattern=r"^[6-9][0-9]{9}$")
    email: Optional[str] = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class FNOL(Strict):
    contract_version: Literal["1.0"] = "1.0"
    claim_reference: str = Field(pattern=r"^FNOL-[0-9]{6,12}$")
    channel: Channel
    policy: Policy
    vehicle: Vehicle
    incident: Incident
    driver: Driver
    claimant: Claimant


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cross_field_errors(f: FNOL, now: datetime | None = None) -> list[dict]:
    """Consistency rules that span fields. Each error names the fields involved."""
    now = now or datetime.now(timezone.utc)
    errs: list[dict] = []

    def err(loc: str, msg: str, rule: str) -> None:
        errs.append({"loc": loc, "msg": msg, "type": f"cross_field.{rule}"})

    p, i, v, d = f.policy, f.incident, f.vehicle, f.driver
    dol, rep = _aware(i.date_of_loss), _aware(i.reported_at)
    if p.policy_end_date <= p.policy_start_date:
        err("policy.policy_end_date", "must be after policy_start_date", "policy_period")
    if (p.policy_end_date - p.policy_start_date).days > 3 * 366:
        err("policy.policy_end_date", "policy period longer than 3 years", "policy_period_length")
    if dol > now + timedelta(minutes=5):
        err("incident.date_of_loss", "date of loss is in the future", "loss_in_future")
    if rep < dol:
        err("incident.reported_at", "reported before the loss occurred", "report_before_loss")
    if rep > now + timedelta(minutes=5):
        err("incident.reported_at", "reported_at is in the future", "report_in_future")
    if (rep - dol).days > 3 * 365:
        err("incident.reported_at", "reported more than 3 years after loss", "report_too_late")
    if v.year_of_manufacture > dol.year:
        err("vehicle.year_of_manufacture", "vehicle manufactured after the date of loss", "yom_after_loss")
    if v.year_of_manufacture > now.year + 1:
        err("vehicle.year_of_manufacture", "year of manufacture in the future", "yom_future")
    if i.police_report_filed and not i.fir_number:
        err("incident.fir_number", "required when police_report_filed is true", "fir_required")
    if not i.police_report_filed and i.fir_number:
        err("incident.police_report_filed", "fir_number given but police_report_filed is false", "fir_flag")
    if i.estimated_repair_cost_inr is not None and i.estimated_repair_cost_inr > p.idv_inr * 2:
        err("incident.estimated_repair_cost_inr", "estimate exceeds twice the IDV", "estimate_vs_idv")
    if i.injuries == Injuries.fatal and not i.police_report_filed:
        err("incident.police_report_filed", "a fatal accident must have a police report", "fatal_needs_fir")
    if d.licence_valid_until < date(1990, 1, 1):
        err("driver.licence_valid_until", "implausible licence validity date", "licence_date")
    return errs


# ---------------------------------------------------------------- result contract
ClaimType = Literal["own_damage", "third_party", "theft", "total_loss", "unknown"]
CoveragePosition = Literal["likely_covered", "covered_with_conditions", "likely_not_covered", "indeterminate"]
Route = Literal["fast_track", "surveyor", "investigation"]
RunStatus = Literal["queued", "running", "completed", "escalated", "failed"]


class Citation(BaseModel):
    clause_id: str
    doc_title: str = ""
    section: str = ""
    page: Optional[int] = None
    quote: str


class Escalation(BaseModel):
    escalated: bool
    reasons: list[str] = []
    path: str = ""


class Recommendation(BaseModel):
    claim_type: ClaimType
    entities: dict = {}
    coverage_position: CoveragePosition
    route: Optional[Route]
    route_reasons: list[str] = []
    rationale: str
    citations: list[Citation] = []
    modifiers_considered: list[str] = []
    escalation: Escalation
    verification: dict = {}
    flags: list[str] = []
    advisory_notice: str = (
        "Advisory recommendation only. The system does not approve or reject claims; "
        "a human claims handler decides."
    )


class FeedbackIn(Strict):
    satisfactory: bool
    reason: str = Field(min_length=3, max_length=1000)
    reviewer: Optional[str] = Field(default=None, max_length=80)
