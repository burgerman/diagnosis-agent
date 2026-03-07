from __future__ import annotations
from typing import Annotated, Any
from ..memory.store import memory_db

def search_code(query: Annotated[str, "The keyword or symbol to search for in the codebase"]) -> list[dict]:
    """Search the codebase for specific keywords and return matching excerpts."""
    from .retriever_logic import SelectiveCodeRetriever
    from ..schemas import AnalysisJobCreate, UptimeStatus
    from datetime import datetime
    
    retriever = SelectiveCodeRetriever()
    dummy_payload = AnalysisJobCreate(
        incident_id="agent-search",
        service_name=query,
        device_or_node=query,
        uptime_status=UptimeStatus.down,
        uptime_description=query,
        detected_at=datetime.utcnow()
    )
    results = retriever.retrieve(dummy_payload)
    return [r.model_dump() for r in results]

def read_incident_context(incident_id: Annotated[str, "The ID of the incident to investigate"]) -> dict:
    """Read the full incident details and logs from the in-memory store."""
    job = memory_db.get_job_by_incident(incident_id)
    return job if job else {"error": "Incident not found"}

def update_investigation_report(
    incident_id: Annotated[str, "The incident ID to update"],
    summary: Annotated[str, "Current summary of findings"],
    hypotheses: Annotated[list[dict], "List of {hypothesis, confidence, evidence_refs}"],
    actions: Annotated[list[dict], "List of {title, description, suggested_command}"]
) -> str:
    """Save or update the final analysis report in the in-memory store."""
    job = memory_db.get_job_by_incident(incident_id)
    if not job:
        return "Error: Could not find job for incident."
        
    report_data = {
        "job_id": job["id"],
        "incident_id": incident_id,
        "summary_text": summary,
        "report_status": "completed",
        "confidence": max([h.get("confidence", 0) for h in hypotheses]) if hypotheses else 0,
        "model_info": {"provider": "google-genai", "agent": "ReasoningAgentV2"},
        "report_json": {
            "root_cause_hypotheses": hypotheses,
            "suggested_actions": actions,
            "summary_text": summary
        }
    }
    memory_db.upsert_report(report_data)
    return "Report updated successfully in memory."

AGENT_TOOLS = [search_code, read_incident_context, update_investigation_report]
