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
from .tools.agent_tools import (
    clean_text,
    step_from_action,
    is_destructive_action,
    structured_review_markdown,
    build_review_markdown,
)

settings = get_settings()
worker = AgentWorker()


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
    job = _get_job_or_404(job_id)
    incident_id = str(report.get("incident_id", "")).strip() or str(job.get("incident_id", ""))
    payload = job.get("request_payload", {})
    if not isinstance(payload, dict):
        payload = {}
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    report_json = report.get("report_json")
    if not isinstance(report_json, dict):
        report_json = {}

    summary_text = clean_text(report.get("summary_text"), max_chars=1200)
    summary_markdown = clean_text(report_json.get("summary_markdown"), max_chars=8000)
    suggested_actions = report_json.get("suggested_actions", [])
    action_items = suggested_actions if isinstance(suggested_actions, list) else []

    try:
        confidence = float(report.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    target_node = (
        clean_text(payload.get("device_or_node"), max_chars=200)
        or clean_text(metadata.get("node"), max_chars=200)
        or clean_text(report_json.get("target_node"), max_chars=200)
    )
    markdown = (
        summary_markdown
        if summary_markdown and structured_review_markdown(summary_markdown)
        else build_review_markdown(
            incident_id=incident_id,
            service_name=str(payload.get("service_name", "unknown-service")),
            summary_text=summary_text,
            confidence=confidence,
            hypotheses=report_json.get("root_cause_hypotheses", []),
            actions=action_items,
            evidence_items=report_json.get("evidence", []),
            uptime_description=clean_text(payload.get("uptime_description"), max_chars=320),
            target_node=target_node,
            source_markdown=summary_markdown,
        )
    )
    return {
        "incident_id": incident_id,
        "summary_text": summary_text,
        "summary_markdown": markdown or summary_text,
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
        steps = [step_from_action(item) for item in action_items]
        steps = [item for item in steps if item][:8]
        proposed_fix = None
        if report:
            summary_text = clean_text(report.get("summary_text"), max_chars=1200)
            summary_markdown_raw = (
                report_json.get("summary_markdown", "") if isinstance(report_json, dict) else ""
            )
            summary_markdown = clean_text(summary_markdown_raw, max_chars=8000)
            target_node = (
                clean_text(payload.get("device_or_node"), max_chars=200)
                or clean_text(metadata.get("node"), max_chars=200)
                or clean_text(report_json.get("target_node"), max_chars=200)
            )
            destructive_actions = [
                step_from_action(item) for item in action_items if is_destructive_action(item)
            ]
            destructive_actions = [item for item in destructive_actions if item][:8]
            if not destructive_actions and isinstance(report_json.get("destructive_actions"), list):
                destructive_actions = [
                    clean_text(item, max_chars=320)
                    for item in report_json["destructive_actions"]
                    if clean_text(item, max_chars=320)
                ][:8]

            markdown = (
                summary_markdown
                if summary_markdown and structured_review_markdown(summary_markdown)
                else build_review_markdown(
                    incident_id=str(job.get("incident_id", "")),
                    service_name=str(payload.get("service_name", "unknown-service")),
                    summary_text=summary_text,
                    confidence=confidence,
                    hypotheses=report_json.get("root_cause_hypotheses", []),
                    actions=action_items,
                    evidence_items=report_json.get("evidence", []),
                    uptime_description=clean_text(payload.get("uptime_description"), max_chars=320),
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
