from __future__ import annotations
import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator

class UptimeStatus(str, Enum):
    down = "down"
    degraded = "degraded"

class LogSnippet(BaseModel):
    timestamp: datetime
    source: str = Field(min_length=1, max_length=400)
    line: str = Field(min_length=1, max_length=20000)

class AnalysisJobCreate(BaseModel):
    incident_id: str = Field(min_length=1, max_length=200)
    service_name: str = Field(min_length=1, max_length=200)
    device_or_node: str = Field(min_length=1, max_length=200)
    uptime_status: UptimeStatus
    uptime_description: str = Field(min_length=1, max_length=4000)
    detected_at: datetime
    log_snippets: list[LogSnippet] = Field(default_factory=list, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

class UptimeKumaJobCreate(BaseModel):
    monitor: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=40)
    msg: str = Field(min_length=1, max_length=4000)
    url: str = Field(min_length=1, max_length=2000)
    time: datetime
    log_snippets: list[LogSnippet] = Field(default_factory=list, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {UptimeStatus.down.value, UptimeStatus.degraded.value}:
            raise ValueError("status must be DOWN or DEGRADED")
        return normalized

    def to_internal(self) -> AnalysisJobCreate:
        node = self.metadata.get("node") or urlparse(self.url).hostname or "unknown-node"
        return AnalysisJobCreate(
            incident_id=f"inc-{self.monitor}-{hashlib.sha1(str(self.time).encode()).hexdigest()[:8]}",
            service_name=self.monitor,
            device_or_node=str(node),
            uptime_status=UptimeStatus(self.status),
            uptime_description=self.msg,
            detected_at=self.time,
            log_snippets=self.log_snippets,
            metadata=self.metadata,
            idempotency_key=self.idempotency_key,
        )

class JobCreatedResponse(BaseModel):
    job_id: str
    status: str

class CodeContextItem(BaseModel):
    file_path: str
    line_start: int
    line_end: int
    excerpt: str