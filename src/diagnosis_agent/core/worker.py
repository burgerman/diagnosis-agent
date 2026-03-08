from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Any
from ..memory.store import memory_db
from ..agent.core import ReasoningAgent

logger = logging.getLogger(__name__)


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


CONNECTIVITY_ERROR_HINTS = (
    "connection refused",
    "connecterror",
    "timed out",
    "timeout",
    "name or service not known",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "network is unreachable",
    "dns",
)


def _looks_like_connectivity_error(error_text: str) -> bool:
    normalized = error_text.lower()
    return any(token in normalized for token in CONNECTIVITY_ERROR_HINTS)


def _looks_like_agent_error_output(agent_output: str) -> bool:
    normalized = agent_output.strip().lower()
    return normalized.startswith("investigation failed:") or normalized.startswith(
        "gemini investigation request failed:"
    )


def _recommended_actions_for_agent_error(error_text: str) -> list[dict[str, str]]:
    if _looks_like_connectivity_error(error_text):
        return [
            {
                "title": "Validate outbound Gemini connectivity",
                "description": (
                    "Check DNS resolution and HTTPS egress from the API host to Gemini endpoints "
                    "(for example `generativelanguage.googleapis.com`)."
                ),
                "suggested_command": "",
            },
            {
                "title": "Confirm active Gemini credentials",
                "description": (
                    "Verify `GEMINI_API_KEY` is loaded in the API process and restart the service "
                    "after any key rotation."
                ),
                "suggested_command": "",
            },
            {
                "title": "Retry after transport recovers",
                "description": (
                    "Submit the incident again once connectivity checks pass to obtain a model-authored report."
                ),
                "suggested_command": "",
            },
        ]
    return [
        {
            "title": "Review Gemini request error",
            "description": "Inspect backend logs for the exact model/tool error and stack trace.",
            "suggested_command": "",
        },
        {
            "title": "Validate model configuration",
            "description": "Confirm model ID, API key, and environment variables are valid.",
            "suggested_command": "",
        },
        {
            "title": "Retry incident analysis",
            "description": "Re-run the same incident once configuration issues are corrected.",
            "suggested_command": "",
        },
    ]


def _build_structured_fallback_markdown(
    *,
    service_name: str,
    summary_text: str,
    uptime_description: str,
    source_error: str | None = None,
    suggested_actions: list[dict[str, str]] | None = None,
) -> str:
    investigation_steps = [f"Attempted automated investigation for `{service_name}`."]
    if source_error:
        error_line = _clean_text(source_error, max_chars=260)
        if error_line:
            investigation_steps.append(f"Captured agent runtime error: `{error_line}`.")
    investigation_steps.append("Reviewed available incident logs and payload metadata only.")

    problems_found = [summary_text]
    monitor_signal = _clean_text(uptime_description, max_chars=260)
    if monitor_signal:
        problems_found.append(monitor_signal)

    other_info = [
        "This report was generated by backend fallback logic because Gemini markdown output was unavailable."
    ]
    if source_error and _looks_like_connectivity_error(source_error):
        other_info.append(
            "Error signature indicates a network/DNS transport problem while calling Gemini."
        )

    solutions = []
    for action in suggested_actions or []:
        title = _clean_text(action.get("title"), max_chars=180) or "Suggested action"
        description = _clean_text(action.get("description"), max_chars=360)
        command = _clean_text(action.get("suggested_command"), max_chars=260)
        detail = description or "Review and execute this step carefully."
        if command:
            solutions.append(f"**{title}**: {detail} (`{command}`)")
        else:
            solutions.append(f"**{title}**: {detail}")
    if not solutions:
        solutions.append("Validate model connectivity and retry this incident analysis.")

    return "\n".join(
        [
            "## Investigation Steps",
            *[f"- {item}" for item in investigation_steps],
            "",
            "## Problems Found",
            *[f"- {item}" for item in problems_found],
            "",
            "## Other Important Info",
            *[f"- {item}" for item in other_info],
            "",
            "## Solution Suggestions",
            *[f"- {item}" for item in solutions],
        ]
    )


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

    if not agent_error and agent_output and _looks_like_agent_error_output(agent_output):
        agent_error = _clean_text(agent_output, max_chars=1200)
        agent_output = None

    model_name = "ReasoningAgentV2-fallback"
    if agent_error:
        agent_error = _clean_text(agent_error, max_chars=1200)
        connectivity_error = _looks_like_connectivity_error(agent_error)
        if connectivity_error:
            summary_text = (
                f"Gemini API request failed for {service_name} before investigation could complete."
            )
        else:
            summary_text = (
                f"Automated investigation failed for {service_name} due to a runtime model/tool error."
            )
        suggested_actions = _recommended_actions_for_agent_error(agent_error)
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
            "root_cause_hypotheses": [
                {
                    "hypothesis": summary_text,
                    "confidence": confidence,
                    "evidence_refs": ["reasoning-agent runtime error"],
                }
            ],
            "evidence": evidence,
            "suggested_actions": suggested_actions,
            "summary_text": summary_text,
            "summary_markdown": _build_structured_fallback_markdown(
                service_name=service_name,
                summary_text=summary_text,
                uptime_description=uptime_description,
                source_error=agent_error,
                suggested_actions=suggested_actions,
            ),
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
        suggested_actions: list[dict[str, str]] = [
            {
                "title": "Retry incident analysis",
                "description": "Submit the incident again to trigger another model pass.",
                "suggested_command": "",
            }
        ]
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
            "suggested_actions": suggested_actions,
            "summary_text": summary_text,
            "summary_markdown": _build_structured_fallback_markdown(
                service_name=service_name,
                summary_text=summary_text,
                uptime_description=uptime_description,
                suggested_actions=suggested_actions,
            ),
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
