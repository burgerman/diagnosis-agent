import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from .schemas import UptimeKumaJobCreate, JobCreatedResponse
from .memory.store import memory_db
from .core.worker import AgentWorker
from .config import get_settings

settings = get_settings()
worker = AgentWorker()
DESTRUCTIVE_ACTION_HINTS = (
    " rm ",
    "delete",
    "drop ",
    "truncate",
    "destroy",
    "wipe",
    "reset --hard",
    "mkfs",
    "shutdown",
    "reboot",
    "kill -9",
    "format ",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create the worker task
    task = asyncio.create_task(worker.run())
    yield
    # Shutdown: Signal the worker to stop and wait for it
    await worker.stop()
    await task

app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_job_or_404(job_id: str) -> dict[str, Any]:
    job = memory_db.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _get_report_or_404(job_id: str) -> dict[str, Any]:
    report = memory_db.get_report(job_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _clean_text(value: Any, *, max_chars: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if max_chars is not None and len(text) > max_chars:
        return f"{text[: max_chars - 3]}..."
    return text


def _step_from_action(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    command = _clean_text(item.get("suggested_command"), max_chars=360)
    title = _clean_text(item.get("title") or item.get("action"), max_chars=180)
    description = _clean_text(item.get("description") or item.get("details"), max_chars=400)
    if command:
        return command
    if title and description:
        return f"{title}: {description}"
    return title or description


def _action_details(item: Any) -> tuple[str, str, str]:
    if not isinstance(item, dict):
        return "", "", ""
    title = _clean_text(item.get("title") or item.get("action"), max_chars=180)
    description = _clean_text(item.get("description") or item.get("details"), max_chars=500)
    command = _clean_text(item.get("suggested_command") or item.get("command"), max_chars=360)
    return title, description, command


def _is_destructive_action(item: Any) -> bool:
    title, description, command = _action_details(item)
    haystack = f" {title} {description} {command} ".lower()
    return any(token in haystack for token in DESTRUCTIVE_ACTION_HINTS)


def _structured_review_markdown(value: str) -> bool:
    normalized = value.lower()
    required_sections = (
        "investigation steps",
        "problems found",
        "other important info",
        "solution suggestions",
    )
    return all(section in normalized for section in required_sections)


def _build_review_markdown(
    *,
    incident_id: str,
    service_name: str,
    summary_text: str,
    confidence: float,
    hypotheses: Any,
    actions: Any,
    evidence_items: Any,
    uptime_description: str,
    target_node: str,
    source_markdown: str,
) -> str:
    investigation_steps = [
        f"Reviewed incident context for `{incident_id}` on service `{service_name}`.",
    ]
    if uptime_description:
        investigation_steps.append(f"Analyzed monitor signal: {uptime_description}.")

    evidence_snippets: list[str] = []
    if isinstance(evidence_items, list):
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            snippet = _clean_text(item.get("snippet"), max_chars=180)
            if snippet:
                evidence_snippets.append(snippet)
            if len(evidence_snippets) >= 2:
                break
    if evidence_snippets:
        investigation_steps.append(f"Correlated evidence snippets: {', '.join(evidence_snippets)}.")

    normalized_actions: list[Any] = actions if isinstance(actions, list) else []
    if normalized_actions:
        investigation_steps.append(
            f"Prepared {len(normalized_actions)} remediation suggestion(s) from available evidence."
        )
    else:
        investigation_steps.append(
            "No concrete remediation command was produced; follow-up investigation is required."
        )

    problems_found: list[str] = []
    if isinstance(hypotheses, list):
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            hypothesis_text = _clean_text(
                hypothesis.get("hypothesis") or hypothesis.get("summary") or hypothesis.get("title"),
                max_chars=320,
            )
            if not hypothesis_text:
                continue
            try:
                hypothesis_confidence = float(hypothesis.get("confidence", 0.0))
            except (TypeError, ValueError):
                hypothesis_confidence = 0.0
            confidence_pct = int(round(min(max(hypothesis_confidence, 0.0), 1.0) * 100))
            if confidence_pct > 0:
                problems_found.append(f"{hypothesis_text} (confidence: {confidence_pct}%).")
            else:
                problems_found.append(hypothesis_text)

    if not problems_found:
        fallback_problem = summary_text or "Root cause is currently inconclusive from available data."
        problems_found.append(_clean_text(fallback_problem, max_chars=320))

    other_info = [
        f"Confidence score: {int(round(min(max(confidence, 0.0), 1.0) * 100))}%.",
    ]
    if target_node:
        other_info.append(f"Target node: `{target_node}`.")

    destructive_actions = [
        _step_from_action(action)
        for action in normalized_actions
        if _is_destructive_action(action)
    ]
    destructive_actions = [item for item in destructive_actions if item]
    if destructive_actions:
        other_info.append(f"Potentially destructive actions: {'; '.join(destructive_actions)}.")
    else:
        other_info.append("No destructive actions were detected in the proposed plan.")

    if source_markdown and source_markdown != summary_text and not _structured_review_markdown(source_markdown):
        other_info.append(_clean_text(source_markdown, max_chars=500))

    solution_suggestions: list[str] = []
    for action in normalized_actions:
        title, description, command = _action_details(action)
        if not (title or description or command):
            continue
        label = title or "Suggested action"
        details = description or "Review this action before execution."
        if command:
            solution_suggestions.append(f"- **{label}**: {details} (`{command}`)")
        else:
            solution_suggestions.append(f"- **{label}**: {details}")
    if not solution_suggestions:
        solution_suggestions.append(
            "- Validate Gemini tool output, capture fresh evidence, and rerun the investigation."
        )

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
            *solution_suggestions,
        ]
    )


@app.post("/api/v1/jobs", response_model=JobCreatedResponse)
@app.post("/api/v1/analysis/jobs", response_model=JobCreatedResponse)
async def create_job(payload: UptimeKumaJobCreate):
    data = payload.to_internal()
    job = memory_db.create_job(
        {
            "incident_id": data.incident_id,
            "request_payload": data.model_dump(mode="json"),
        }
    )
    return {"job_id": job["id"], "status": "queued"}


@app.get("/api/v1/jobs/{job_id}")
@app.get("/api/v1/analysis/jobs/{job_id}")
async def get_job(job_id: str):
    return _get_job_or_404(job_id)


@app.get("/api/v1/jobs/{job_id}/result")
@app.get("/api/v1/analysis/jobs/{job_id}/result")
async def get_result(job_id: str):
    return _get_report_or_404(job_id)


@app.get("/api/v1/analysis/jobs/{job_id}/summary")
async def get_analysis_summary(job_id: str):
    report = _get_report_or_404(job_id)
    incident_id = str(report.get("incident_id", "")).strip() or str(_get_job_or_404(job_id).get("incident_id", ""))
    try:
        confidence = float(report.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "incident_id": incident_id,
        "summary_text": str(report.get("summary_text", "")),
        "confidence": confidence,
    }


@app.get("/api/v1/analysis/jobs/{job_id}/download")
async def download_analysis_report(job_id: str):
    report = _get_report_or_404(job_id)
    report_json = report.get("report_json")
    content_obj = report_json if isinstance(report_json, dict) else report
    content = json.dumps(content_obj, indent=2).encode("utf-8")
    filename = f"analysis-report-{job_id}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/v1/analysis/incidents")
async def list_analysis_incidents(limit: int = 50):
    safe_limit = min(max(limit, 1), 200)
    output: list[dict[str, Any]] = []

    for job in memory_db.list_jobs(limit=safe_limit):
        payload = job.get("request_payload", {})
        if not isinstance(payload, dict):
            payload = {}
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        report = memory_db.get_report(job["id"])
        report_json = report.get("report_json") if report else {}
        if not isinstance(report_json, dict):
            report_json = {}

        job_status = str(job.get("status", ""))
        uptime_status = str(payload.get("uptime_status", "")).lower()
        if job_status in {"queued", "running"}:
            ui_status = "resolving"
        elif job_status == "failed" or (job_status == "completed" and uptime_status == "degraded"):
            ui_status = "warning"
        else:
            ui_status = "issue"

        evidence_items = report_json.get("evidence", [])
        logs: list[str] = []
        if isinstance(evidence_items, list):
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
                evidence_type = str(item.get("type", "")).strip().lower()
                if evidence_type == "agent_output":
                    continue
                snippet = str(item.get("snippet", "")).strip()
                if not snippet:
                    continue
                if len(snippet) > 320:
                    snippet = f"{snippet[:317]}..."
                logs.append(snippet)
        if not logs:
            raw_logs = payload.get("log_snippets", [])
            logs = [
                str(item.get("line", "")).strip()
                for item in raw_logs
                if isinstance(item, dict) and str(item.get("line", "")).strip()
            ] if isinstance(raw_logs, list) else []
            logs = [f"{line[:317]}..." if len(line) > 320 else line for line in logs]
        if not logs:
            fallback_line = str(payload.get("uptime_description", "No logs provided")).strip()
            logs = [fallback_line or "No logs provided"]
        logs = logs[:12]

        try:
            confidence = float(report.get("confidence", 0.0)) if report else 0.0
        except (TypeError, ValueError):
            confidence = 0.0

        suggested_actions = report_json.get("suggested_actions", [])
        action_items = suggested_actions if isinstance(suggested_actions, list) else []
        steps = [_step_from_action(item) for item in action_items]
        steps = [item for item in steps if item][:8]
        proposed_fix = None
        if report:
            summary_text = _clean_text(report.get("summary_text"), max_chars=1200)
            summary_markdown_raw = (
                report_json.get("summary_markdown", "") if isinstance(report_json, dict) else ""
            )
            summary_markdown = _clean_text(summary_markdown_raw, max_chars=8000)
            target_node = (
                _clean_text(payload.get("device_or_node"), max_chars=200)
                or _clean_text(metadata.get("node"), max_chars=200)
                or _clean_text(report_json.get("target_node"), max_chars=200)
            )
            destructive_actions = [
                _step_from_action(item) for item in action_items if _is_destructive_action(item)
            ]
            destructive_actions = [item for item in destructive_actions if item][:8]
            if not destructive_actions and isinstance(report_json.get("destructive_actions"), list):
                destructive_actions = [
                    _clean_text(item, max_chars=320)
                    for item in report_json["destructive_actions"]
                    if _clean_text(item, max_chars=320)
                ][:8]

            markdown = (
                summary_markdown
                if summary_markdown and _structured_review_markdown(summary_markdown)
                else _build_review_markdown(
                    incident_id=str(job.get("incident_id", "")),
                    service_name=str(payload.get("service_name", "unknown-service")),
                    summary_text=summary_text,
                    confidence=confidence,
                    hypotheses=report_json.get("root_cause_hypotheses", []),
                    actions=action_items,
                    evidence_items=report_json.get("evidence", []),
                    uptime_description=_clean_text(payload.get("uptime_description"), max_chars=320),
                    target_node=target_node,
                    source_markdown=summary_markdown,
                )
            )

            if summary_text or markdown or steps:
                description = summary_text or "Suggested remediation plan."
                proposed_fix = {
                    "description": description,
                    "steps": steps,
                    "markdown": markdown or description,
                    "destructiveActions": destructive_actions,
                    "targetNode": target_node,
                }

        output.append(
            {
                "id": str(job.get("incident_id", "")),
                "service": str(payload.get("service_name", "unknown-service")),
                "serviceType": str(metadata.get("service_type", "service")),
                "status": ui_status,
                "logs": logs,
                "confidence": confidence,
                "proposedFix": proposed_fix,
            }
        )

    return output


@app.get("/health")
def health():
    return {"status": "alive", "storage": "in-memory"}
