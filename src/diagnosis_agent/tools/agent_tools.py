from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from ..memory.store import memory_db

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


def clean_text(value: Any, *, max_chars: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if max_chars is not None and len(text) > max_chars:
        return f"{text[: max_chars - 3]}..."
    return text


def step_from_action(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    command = clean_text(item.get("suggested_command"), max_chars=360)
    title = clean_text(item.get("title") or item.get("action"), max_chars=180)
    description = clean_text(item.get("description") or item.get("details"), max_chars=400)
    if command:
        return command
    if title and description:
        return f"{title}: {description}"
    return title or description


def action_details(item: Any) -> tuple[str, str, str]:
    if not isinstance(item, dict):
        return "", "", ""
    title = clean_text(item.get("title") or item.get("action"), max_chars=180)
    description = clean_text(item.get("description") or item.get("details"), max_chars=500)
    command = clean_text(item.get("suggested_command") or item.get("command"), max_chars=360)
    return title, description, command


def is_destructive_action(item: Any) -> bool:
    title, description, command = action_details(item)
    haystack = f" {title} {description} {command} ".lower()
    return any(token in haystack for token in DESTRUCTIVE_ACTION_HINTS)


def structured_review_markdown(value: str) -> bool:
    normalized = value.lower()
    required_sections = (
        "investigation steps",
        "problems found",
        "other important info",
        "solution suggestions",
    )
    return all(section in normalized for section in required_sections)


def build_review_markdown(
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
            snippet = clean_text(item.get("snippet"), max_chars=180)
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
            hypothesis_text = clean_text(
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
        problems_found.append(clean_text(fallback_problem, max_chars=320))

    other_info = [
        f"Confidence score: {int(round(min(max(confidence, 0.0), 1.0) * 100))}%.",
    ]
    if target_node:
        other_info.append(f"Target node: `{target_node}`.")

    destructive_actions = [
        step_from_action(action)
        for action in normalized_actions
        if is_destructive_action(action)
    ]
    destructive_actions = [item for item in destructive_actions if item]
    if destructive_actions:
        other_info.append(f"Potentially destructive actions: {'; '.join(destructive_actions)}.")
    else:
        other_info.append("No destructive actions were detected in the proposed plan.")

    if source_markdown and source_markdown != summary_text and not structured_review_markdown(source_markdown):
        other_info.append(clean_text(source_markdown, max_chars=500))

    solution_suggestions: list[str] = []
    for action in normalized_actions:
        title, description, command = action_details(action)
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
            "- No model-generated remediation actions were returned for this incident."
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


AGENT_TOOLS = [search_code, read_incident_context, update_investigation_report, fetch_dynamic_log_snippet]
