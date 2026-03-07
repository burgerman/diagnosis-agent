from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class MemoryStore:
    """A thread-safe in-memory database replacement."""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MemoryStore, cls).__new__(cls)
            cls._instance.jobs: Dict[str, Dict[str, Any]] = {}
            cls._instance.reports: Dict[str, Dict[str, Any]] = {}
        return cls._instance

    def create_job(self, data: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **data
        }
        self.jobs[job_id] = job
        return job

    def get_job_by_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        for job in self.jobs.values():
            if job.get("incident_id") == incident_id:
                return job
        return None

    def update_job(self, job_id: str, updates: Dict[str, Any]):
        if job_id in self.jobs:
            self.jobs[job_id].update(updates)
            self.jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def get_queued_job(self) -> Optional[Dict[str, Any]]:
        for job in self.jobs.values():
            if job["status"] == "queued":
                return job
        return None

    def upsert_report(self, report_data: Dict[str, Any]):
        job_id = report_data["job_id"]
        self.reports[job_id] = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **report_data
        }

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return sorted(self.jobs.values(), key=lambda x: x["created_at"], reverse=True)[:limit]

    def get_report(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.reports.get(job_id)

memory_db = MemoryStore()