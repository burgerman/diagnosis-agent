from __future__ import annotations
import asyncio
import logging
from typing import Any
from ..memory.store import memory_db
from ..agent.core import ReasoningAgent

logger = logging.getLogger(__name__)


def _fallback_suggested_actions(service_name: str) -> list[dict[str, str]]:
    safe_service = "".join(ch for ch in service_name if ch.isalnum() or ch in "-_.")
    status_target = safe_service or "service"
    return [
        {
            "title": "Inspect recent logs",
            "description": "Review logs around the failure window for startup/runtime errors.",
            "suggested_command": (
                f"journalctl -u {status_target} --since '15 minutes ago' --no-pager | tail -n 200"
            ),
        }
    ]


def ensure_report_with_fallback(
    job: dict[str, Any],
    *,
    agent_output: str | None = None,
    agent_error: str | None = None,
) -> None:
    job_id = str(job.get("id", ""))
    if not job_id or memory_db.get_report(job_id):
        return

    payload = job.get("request_payload", {})
    if not isinstance(payload, dict):
        payload = {}

    incident_id = str(job.get("incident_id", "")).strip()
    service_name = str(payload.get("service_name", "unknown-service")).strip() or "unknown-service"
    uptime_description = (
        str(payload.get("uptime_description", "No monitor description provided.")).strip()
        or "No monitor description provided."
    )

    if agent_error:
        summary_text = (
            f"Automated investigation failed for {service_name}. "
            "Use captured evidence and retry once API/tool configuration is validated."
        )
    elif agent_output and agent_output.strip():
        summary_text = (
            f"Automated investigation completed without a structured report for {service_name}. "
            "Use captured evidence for manual triage."
        )
    else:
        summary_text = (
            f"Automated investigation completed without a structured report for {service_name}. "
            "Use captured evidence for manual triage."
        )

    evidence: list[dict[str, str]] = [
        {
            "type": "monitor",
            "source": "uptime_description",
            "snippet": uptime_description,
        }
    ]
    if agent_output and agent_output.strip():
        agent_output_snippet = " ".join(agent_output.strip().split())
        if len(agent_output_snippet) > 600:
            agent_output_snippet = f"{agent_output_snippet[:597]}..."
        evidence.insert(
            0,
            {
                "type": "agent_output",
                "source": "reasoning-agent",
                "snippet": agent_output_snippet,
            },
        )
    if agent_error:
        evidence.insert(
            0,
            {
                "type": "agent_error",
                "source": "reasoning-agent",
                "snippet": agent_error,
            },
        )

    memory_db.upsert_report(
        {
            "job_id": job_id,
            "incident_id": incident_id,
            "summary_text": summary_text,
            "report_status": "completed",
            "confidence": 0.1 if agent_error else 0.2,
            "model_info": {"provider": "google-genai", "agent": "ReasoningAgentV2-fallback"},
            "report_json": {
                "root_cause_hypotheses": [],
                "evidence": evidence,
                "suggested_actions": _fallback_suggested_actions(service_name),
                "summary_text": summary_text,
            },
        }
    )


class AgentWorker:
    """Decoupled worker that polls the in-memory store."""
    def __init__(self, poll_interval_sec: float = 1.0):
        self.poll_interval = poll_interval_sec
        self.stop_event = asyncio.Event()
        self.agent = ReasoningAgent()

    async def run(self):
        logger.info("Agent Worker started (In-Memory Context)")
        while not self.stop_event.is_set():
            job = memory_db.get_queued_job()
            if job:
                job_id = job["id"]
                incident_id = job["incident_id"]
                try:
                    memory_db.update_job(job_id, {"status": "running", "started_at": asyncio.get_event_loop().time()})
                    
                    # Run the reasoning agent
                    agent_output = await self.agent.investigate(incident_id)
                    ensure_report_with_fallback(job, agent_output=agent_output)
                    
                    memory_db.update_job(job_id, {"status": "completed", "progress": 100})
                except Exception as e:
                    logger.exception("Agent job %s failed", job_id)
                    ensure_report_with_fallback(job, agent_error=str(e))
                    memory_db.update_job(job_id, {"status": "failed", "error": str(e)})
            else:
                await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self.stop_event.set()
