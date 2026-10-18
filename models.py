from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Literal, Optional, Set
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=True,
    )


class DoseUnit(str, Enum):
    MCG = "mcg"
    MG = "mg"
    G = "g"
    IU = "iu"
    ML = "ml"


class AdministrationRoute(str, Enum):
    ORAL = "oral"
    SUBLINGUAL = "sublingual"
    TRANSDERMAL = "transdermal"
    INTRANASAL = "intranasal"
    OTHER = "other"


class LabValueType(str, Enum):
    NUMERIC = "numeric"
    TEXT = "text"
    POS_NEG = "positive_negative"


class Mood(str, Enum):
    VERY_LOW = "very_low"
    LOW = "low"
    NEUTRAL = "neutral"
    GOOD = "good"
    VERY_GOOD = "very_good"


class SupplementLog(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    entry_type: Literal["supplement"] = "supplement"
    recorded_at: datetime = Field(default_factory=utc_now)
    supplement_name: str = Field(min_length=1, max_length=120)
    dose_value: float
    dose_unit: DoseUnit
    route: AdministrationRoute
    site: Optional[str] = Field(default=None, max_length=80)
    protocol_phase: Optional[str] = Field(default=None, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=2000)
    raw_text: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value

    @field_validator("dose_value")
    @classmethod
    def dose_value_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("dose_value must be positive")
        return value


class BiometricLog(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    entry_type: Literal["biometric"] = "biometric"
    recorded_at: datetime = Field(default_factory=utc_now)
    metric_name: str = Field(min_length=1, max_length=120)
    value: float
    unit: str = Field(min_length=1, max_length=32)
    context: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=2000)
    raw_text: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value


class LabResult(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    entry_type: Literal["lab_result"] = "lab_result"
    collected_at: datetime
    resulted_at: Optional[datetime] = None
    panel_name: Optional[str] = Field(default=None, max_length=120)
    marker_name: str = Field(min_length=1, max_length=120)
    value_type: LabValueType = LabValueType.NUMERIC
    value_numeric: Optional[float] = None
    value_text: Optional[str] = Field(default=None, max_length=200)
    unit: Optional[str] = Field(default=None, max_length=32)
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    lab_name: Optional[str] = Field(default=None, max_length=120)
    flagged: bool = False
    notes: Optional[str] = Field(default=None, max_length=2000)
    raw_text: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("collected_at", "resulted_at")
    @classmethod
    def lab_dates_must_include_timezone(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return value
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("lab datetimes must be timezone-aware")
        return value

    @model_validator(mode="after")
    def lab_value_must_match_type(self) -> LabResult:
        if self.value_type == LabValueType.NUMERIC and self.value_numeric is None:
            raise ValueError("value_numeric is required when value_type is numeric")
        if self.value_type != LabValueType.NUMERIC and not self.value_text:
            raise ValueError("value_text is required when value_type is not numeric")
        if (
            self.reference_low is not None
            and self.reference_high is not None
            and self.reference_low > self.reference_high
        ):
            raise ValueError("reference_low cannot be greater than reference_high")
        return self


class DailyJournal(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    entry_type: Literal["daily_journal"] = "daily_journal"
    journal_date: date = Field(default_factory=date.today)
    mood: Optional[Mood] = None
    energy_score: Optional[int] = Field(default=None, ge=1, le=10)
    sleep_hours: Optional[float] = None
    symptoms: List[str] = Field(default_factory=list, max_length=50)
    training: Optional[str] = Field(default=None, max_length=1000)
    nutrition: Optional[str] = Field(default=None, max_length=1000)
    notes: str = Field(
        min_length=1,
        max_length=5000,
        description=(
            "A synthesized, third-person clinical summary of the user's experience. "
            "Do NOT copy-paste raw verbatim text. Resolve all pronouns and vague "
            "references to their explicit subjects using conversational context "
            "(e.g., 'it' → the supplement name, 'my D' → Vitamin D3). "
            "Combine fragmented thoughts into a single cohesive observation. "
            "Do NOT include bracket annotations like [Tag text] in the notes body. "
            "All context belongs in the prose summary itself."
        ),
    )
    raw_text: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("journal_date", mode="before")
    @classmethod
    def coerce_journal_date(cls, value: object) -> date:
        """Accept a date, a plain ISO date string, or a datetime, and always store as date."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            # Accept "2026-05-25" or "2026-05-25T01:10:02+00:00"
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                raise ValueError(f"Cannot parse journal_date: {value!r}")
        raise ValueError(f"Unexpected type for journal_date: {type(value)}")

    @field_validator("sleep_hours")
    @classmethod
    def sleep_hours_must_be_in_daily_range(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not 0 <= value <= 24:
            raise ValueError("sleep_hours must be between 0 and 24")
        return value


class AIExtractionPayload(StrictModel):
    raw_input_text: str = Field(min_length=1, max_length=4000)
    log_type: List[Literal["supplement", "biometric", "lab_result", "daily_journal"]] = Field(
        default_factory=list,
        max_length=4,
    )
    supplements: List[SupplementLog] = Field(default_factory=list)
    biometrics: List[BiometricLog] = Field(default_factory=list)
    labs: List[LabResult] = Field(default_factory=list)
    journals: List[DailyJournal] = Field(default_factory=list)

    @model_validator(mode="after")
    def requested_log_types_must_have_payloads(self) -> AIExtractionPayload:
        if "supplement" in self.log_type and not self.supplements:
            raise ValueError("supplement log_type requires at least one supplement")
        if "biometric" in self.log_type and not self.biometrics:
            raise ValueError("biometric log_type requires at least one biometric")
        if "lab_result" in self.log_type and not self.labs:
            raise ValueError("lab_result log_type requires at least one lab")
        if "daily_journal" in self.log_type and not self.journals:
            raise ValueError("daily_journal log_type requires at least one journal")
        return self


# ── Knowledge Graph models ─────────────────────────────────────────────────────

class ConfidenceLevel(str, Enum):
    HYPOTHESIS = "hypothesis"
    TESTING = "testing"
    CONFIRMED = "confirmed"


class ClinicalNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[str] = None
    concept_name: str
    category: str
    summary_text: str
    confidence_level: str
    last_updated: Optional[str] = None
    expires_at: Optional[str] = None
    last_surfaced_date: Optional[str] = None
    is_archived: int = 0


class ClinicalEdge(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[str] = None
    source_node_id: Optional[str] = None
    target_node_id: Optional[str] = None
    source: Optional[str] = None   # resolved concept_name, populated by JOIN queries
    target: Optional[str] = None   # resolved concept_name, populated by JOIN queries
    relationship_type: str
    evidence_summary: Optional[str] = None
    created_at: Optional[str] = None


# ── Shared updatable-field allowlist ──────────────────────────────────────────
# Single source of truth imported by api.py and llm_service.py.
# Using sets for O(1) membership checks.
UPDATABLE_FIELDS: Dict[str, Set[str]] = {
    "biometric": {"recorded_at", "metric_name", "value", "unit", "notes"},
    "supplement":  {"recorded_at", "supplement_name", "dose_value", "dose_unit", "route", "site", "notes"},
    "lab":       {"collected_at", "resulted_at", "panel_name", "marker_name",
                  "value_numeric", "unit", "reference_low", "reference_high", "notes", "flagged"},
    "journal":   {"journal_date", "mood", "energy_score", "sleep_hours", "notes"},
}
