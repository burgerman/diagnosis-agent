from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from ..memory.store import memory_db


def search_code(query: Annotated[str, "The keyword or symbol to search for in the codebase"]) -> list:
    """Search the codebase for specific keywords and return matching excerpts."""
    from .retriever_logic import SelectiveCodeRetriever
    from ..schemas import AnalysisJobCreate, UptimeStatus
    from datetime import datetime, timezone

    retriever = SelectiveCodeRetriever()
    dummy_payload = AnalysisJobCreate(
        incident_id="agent-search",
        service_name=query,
        device_or_node=query,
        uptime_status=UptimeStatus.down,
        uptime_description=f"Automated search for {query}",
        detected_at=datetime.now(timezone.utc)

    )
    results = retriever.retrieve(dummy_payload)
    return [r.model_dump() for r in results]

def read_incident_context(incident_id: str) -> dict:
    """Read the full incident details and logs from the in-memory store."""
    job = memory_db.get_job_by_incident(incident_id)
    return job if job else {"error": "Incident not found"}

def update_investigation_report(
    incident_id: str,
    summary: str,
    hypotheses: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    summary_markdown: str,
) -> str:
    """Save or update the final analysis report in the in-memory store."""
    job = memory_db.get_job_by_incident(incident_id)
    if not job:
        return "Error: Could not find job for incident."

    normalized_hypotheses = [item for item in hypotheses if isinstance(item, dict)]
    normalized_actions = [item for item in actions if isinstance(item, dict)]

    confidence_scores: list[float] = []
    for item in normalized_hypotheses:
        try:
            confidence_scores.append(float(item.get("confidence", 0)))
        except (TypeError, ValueError):
            confidence_scores.append(0.0)

    cleaned_markdown = str(summary_markdown).strip()

    report_data = {
        "job_id": job["id"],
        "incident_id": incident_id,
        "summary_text": summary,
        "report_status": "completed",
        "confidence": max(confidence_scores) if confidence_scores else 0.0,
        "model_info": {"provider": "google-genai", "agent": "ReasoningAgentV2"},
        "report_json": {
            "root_cause_hypotheses": normalized_hypotheses,
            "suggested_actions": normalized_actions,
            "summary_text": summary,
            "summary_markdown": cleaned_markdown or summary,
        },
    }
    memory_db.upsert_report(report_data)
    return "Report updated successfully in memory."

def normalize_timestamp(ts_str: str) -> datetime:
    """
    Parses an ISO string, handles the 'Z' (UTC) indicator, and
    forces it to be a timezone-naive datetime object for safe comparisons.
    """
    # Replace 'Z' with '+00:00' for broader Python version compatibility
    clean_str = ts_str.replace("Z", "+00:00")
    # Parse and strip timezone info entirely
    return datetime.fromisoformat(clean_str).replace(tzinfo=None)

def fetch_dynamic_log_snippet(log_directory: str, target_timestamp_str: str, window_seconds: int = 5) -> str:
    """
    Dynamically locates log files and fetches logs from T-window to T+window.
    Intelligently handles 'T', spaces, and 'Z' timezone indicators safely.
    """
    # Safely parse the incoming target timestamp
    target_time = normalize_timestamp(target_timestamp_str)
    start_time = target_time - timedelta(seconds=window_seconds)
    end_time = target_time + timedelta(seconds=window_seconds)

    start_date_str = start_time.strftime("%Y-%m-%d")
    end_date_str = end_time.strftime("%Y-%m-%d")

    dates_to_check = [start_date_str]
    if start_date_str != end_date_str:
        dates_to_check.append(end_date_str)

    snippet = []
    in_window = False
    log_dir_path = Path(log_directory)

    for date_str in dates_to_check:
        log_filename = f"app_{date_str}.log"
        log_path = log_dir_path / log_filename

        if not log_path.exists():
            snippet.append(f"[System Warning: Log file {log_filename} not found]")
            continue

        with open(log_path, 'r') as file:
            for line in file:
                first_peek = line.split(" ", 1)[0]

                try:
                    if "T" in first_peek:
                        # Format: 2026-03-07T04:36:12 [INFO]...
                        parts = line.split(" ", 1)
                        timestamp_str = parts[0]
                    else:
                        # Format: 2026-03-07 04:36:12 [INFO]...
                        parts = line.split(" ", 2)
                        # Ensure the line actually has enough parts to be a space-separated timestamp
                        if len(parts) >= 2:
                            timestamp_str = f"{parts[0]} {parts[1]}"
                        else:
                            timestamp_str = parts[0]

                    # Safely parse the log's timestamp
                    log_time = normalize_timestamp(timestamp_str)

                    if start_time <= log_time <= end_time:
                        in_window = True
                        snippet.append(line.strip())
                    elif log_time > end_time:
                        break
                    else:
                        in_window = False

                except ValueError:
                    # If it's not a valid timestamp (like a stack trace), capture if in window
                    if in_window:
                        snippet.append(line.strip())

    return "\n".join(snippet)


AGENT_TOOLS = [search_code, read_incident_context, update_investigation_report, fetch_dynamic_log_snippet]
