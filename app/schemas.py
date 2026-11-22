"""Request and response models.

Timestamps in requests are ISO-8601 strings with a timezone.  They are parsed
by the service layer so that we can enforce UTC-day bounds and second
precision with clear error messages.  Responses always use ``...Z``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class NodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=200)


class SegmentIn(BaseModel):
    start: str = Field(min_length=1, max_length=40)
    end: str = Field(min_length=1, max_length=40)
    state: Literal["on", "off"]


class BranchIn(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    node_a: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)
    node_b: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)
    segments: list[SegmentIn] = Field(default_factory=list)


class DayRegistration(BaseModel):
    buildings: list[NodeIn] = Field(min_length=1)
    power_sources: list[NodeIn] = Field(min_length=1)
    branches: list[BranchIn] = Field(default_factory=list)


class CorrectionIn(BaseModel):
    expected_version: int = Field(ge=1)
    segments: list[SegmentIn] = Field(default_factory=list)


class IntervalOut(BaseModel):
    start: str
    end: str


class BuildingReportOut(BaseModel):
    code: str
    name: str
    status: Literal["powered", "confirmed_outage", "needs_verification"]
    outages: list[IntervalOut]
    pending: list[IntervalOut]


class ReportOut(BaseModel):
    day: str
    version: int
    generated_at: str
    timezone: Literal["UTC"] = "UTC"
    buildings: list[BuildingReportOut]


class SegmentOut(BaseModel):
    start: str
    end: str
    state: Literal["on", "off"]


class TopologyBranchOut(BaseModel):
    code: str
    name: str
    node_a: str
    node_b: str
    segments: list[SegmentOut]


class TopologyNodeOut(BaseModel):
    code: str
    name: str


class TopologyOut(BaseModel):
    day: str
    current_version: int
    buildings: list[TopologyNodeOut]
    power_sources: list[TopologyNodeOut]
    branches: list[TopologyBranchOut]


class ReportVersionOut(BaseModel):
    version: int
    generated_at: str
    current: bool


class ReportVersionsOut(BaseModel):
    day: str
    versions: list[ReportVersionOut]
