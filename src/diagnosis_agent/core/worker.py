from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Any
from ..memory.store import memory_db
from ..agent.core import ReasoningAgent

logger = logging.getLogger(__name__)


def _fallback_suggested_actions(service_name: str) -> list[dict[str, str]]:
    safe_service = "".join(ch for ch in service_name if ch.isalnum() or ch in "-_.")
    status_target = safe_service or "service"
    return [
        {
            "title": "Verify monitor context",
            "description": (
                f"Confirm alert metadata and affected scope for {status_target} before applying changes."
            ),
            "suggested_command": "",
        },
        {
            "title": "Collect focused evidence",
            "description": (
                f"Capture logs and service telemetry for {status_target} around the detection window."
            ),
            "suggested_command": "",
        }
    ]


def _clean_text(value: Any, *, max_chars: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if max_chars is not None and len(text) > max_chars:
        return f"{text[: max_chars - 3]}..."
    return text


def _agent_output_snippet(agent_output: str, *, max_chars: int = 600) -> str:
    snippet = " ".join(agent_output.strip().split())
    return _clean_text(snippet, max_chars=max_chars)


def _extract_json_dict(agent_output: str) -> dict[str, Any] | None:
    stripped = agent_output.strip()
    if not stripped:
        return None

    candidates: list[str] = [stripped]
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", stripped, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if block:
            candidates.append(block)

    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(stripped[first_brace : last_brace + 1])

    seen: set[str] = set()
    for candidate in candidates:
        payload = candidate.strip()
        if not payload or payload in seen:
            continue
        seen.add(payload)
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _normalize_hypotheses(hypotheses: Any) -> list[dict[str, Any]]:
    if not isinstance(hypotheses, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in hypotheses:
        if not isinstance(item, dict):
            continue
        hypothesis = _clean_text(
            item.get("hypothesis") or item.get("title") or item.get("summary"),
            max_chars=500,
        )
        if not hypothesis:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        evidence_refs = item.get("evidence_refs", [])
        refs: list[str] = []
        if isinstance(evidence_refs, list):
            refs = [
                _clean_text(ref, max_chars=160)
                for ref in evidence_refs
                if _clean_text(ref, max_chars=160)
            ][:12]
        normalized.append(
            {
                "hypothesis": hypothesis,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    return normalized


def _normalize_actions(actions: Any) -> list[dict[str, str]]:
    if not isinstance(actions, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title") or item.get("action"), max_chars=180)
        description = _clean_text(item.get("description") or item.get("details"), max_chars=500)
        command = _clean_text(item.get("suggested_command") or item.get("command"), max_chars=300)
        if not (title or description or command):
            continue
        if not title:
            title = f"Action {len(normalized) + 1}"
        normalized.append(
            {
                "title": title,
                "description": description or "Review and execute this remediation action carefully.",
                "suggested_command": command,
            }
        )
    return normalized


def _normalize_evidence(evidence: Any) -> list[dict[str, str]]:
    if not isinstance(evidence, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        snippet = _clean_text(item.get("snippet") or item.get("evidence"), max_chars=600)
        if not snippet:
            continue
        normalized.append(
            {
                "type": _clean_text(item.get("type") or "agent_evidence", max_chars=80) or "agent_evidence",
                "source": _clean_text(item.get("source") or "reasoning-agent", max_chars=120) or "reasoning-agent",
                "snippet": snippet,
            }
        )
    return normalized


def _summary_from_agent_output(agent_output: str, service_name: str) -> str:
    lines: list[str] = []
    for raw_line in agent_output.splitlines():
        line = raw_line.strip()
        if not line:
            if lines:
                break
            continue
        if line.startswith("```"):
            continue
        cleaned = _clean_text(line.lstrip("#-* ").strip(), max_chars=260)
        if not cleaned:
            continue
        lines.append(cleaned)
        if len(" ".join(lines)) >= 260:
            break
    summary = " ".join(lines)
    if summary:
        return _clean_text(summary, max_chars=320)
    return f"Automated investigation returned unstructured output for {service_name}."


def _build_report_from_agent_output(
    *,
    agent_output: str,
    service_name: str,
    uptime_description: str,
) -> dict[str, Any]:
    parsed = _extract_json_dict(agent_output)
    hypotheses: list[dict[str, Any]] = []
    suggested_actions: list[dict[str, str]] = []
    extra_evidence: list[dict[str, str]] = []
    summary_markdown = _clean_text(agent_output, max_chars=8000)
    summary_text = ""

    if parsed:
        summary_text = _clean_text(
            parsed.get("summary_text") or parsed.get("summary"),
            max_chars=500,
        )
        summary_markdown = _clean_text(
            parsed.get("summary_markdown") or summary_markdown,
            max_chars=8000,
        )
        hypotheses = _normalize_hypotheses(
            parsed.get("root_cause_hypotheses") or parsed.get("hypotheses")
        )
        suggested_actions = _normalize_actions(
            parsed.get("suggested_actions") or parsed.get("actions")
        )
        extra_evidence = _normalize_evidence(parsed.get("evidence"))

    if not summary_text:
        summary_text = _summary_from_agent_output(agent_output, service_name)
    if not suggested_actions:
        suggested_actions = _fallback_suggested_actions(service_name)

    evidence: list[dict[str, str]] = [
        {
            "type": "agent_output",
            "source": "reasoning-agent",
            "snippet": _agent_output_snippet(agent_output),
        }
    ]
    evidence.extend(extra_evidence)
    evidence.append(
        {
            "type": "monitor",
            "source": "uptime_description",
            "snippet": _clean_text(uptime_description, max_chars=600) or "No monitor description provided.",
        }
    )

    confidence = max((item.get("confidence", 0.0) for item in hypotheses), default=0.3)
    return {
        "summary_text": summary_text,
        "confidence": confidence,
        "report_json": {
            "root_cause_hypotheses": hypotheses,
            "evidence": evidence,
            "suggested_actions": suggested_actions,
            "summary_text": summary_text,
            "summary_markdown": summary_markdown or summary_text,
        },
    }


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
    model_name = "ReasoningAgentV2-fallback"
    if agent_error:
        summary_text = (
            f"Automated investigation failed for {service_name}. "
            "Use captured evidence and retry once API/tool configuration is validated."
        )
        evidence: list[dict[str, str]] = [
            {
                "type": "agent_error",
                "source": "reasoning-agent",
                "snippet": _clean_text(agent_error, max_chars=600),
            },
            {
                "type": "monitor",
                "source": "uptime_description",
                "snippet": _clean_text(uptime_description, max_chars=600) or "No monitor description provided.",
            },
        ]
        confidence = 0.1
        report_json = {
            "root_cause_hypotheses": [],
            "evidence": evidence,
            "suggested_actions": _fallback_suggested_actions(service_name),
            "summary_text": summary_text,
        }
    elif agent_output and agent_output.strip():
        parsed_report = _build_report_from_agent_output(
            agent_output=agent_output,
            service_name=service_name,
            uptime_description=uptime_description,
        )
        summary_text = parsed_report["summary_text"]
        confidence = parsed_report["confidence"]
        report_json = parsed_report["report_json"]
        model_name = "ReasoningAgentV2-fallback-parser"
    else:
        summary_text = (
            f"Automated investigation completed without a structured report for {service_name}. "
            "Use captured evidence for manual triage."
        )
        confidence = 0.2
        report_json = {
            "root_cause_hypotheses": [],
            "evidence": [
                {
                    "type": "monitor",
                    "source": "uptime_description",
                    "snippet": _clean_text(uptime_description, max_chars=600)
                    or "No monitor description provided.",
                }
            ],
            "suggested_actions": _fallback_suggested_actions(service_name),
            "summary_text": summary_text,
        }

    memory_db.upsert_report(
        {
            "job_id": job_id,
            "incident_id": incident_id,
            "summary_text": summary_text,
            "report_status": "completed",
            "confidence": confidence,
            "model_info": {"provider": "google-genai", "agent": model_name},
            "report_json": report_json,
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
