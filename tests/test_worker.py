import asyncio
import os

import pytest
from dotenv import load_dotenv

# Keep worker construction deterministic in tests.
load_dotenv(dotenv_path=".env.test", override=True)
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from diagnosis_agent.core.worker import AgentWorker, ensure_report_with_fallback
from diagnosis_agent.memory.store import memory_db


@pytest.fixture(autouse=True)
def reset_memory_store():
    memory_db.jobs.clear()
    memory_db.reports.clear()
    yield
    memory_db.jobs.clear()
    memory_db.reports.clear()


def _job(service_name: str = "api-service") -> dict:
    return memory_db.create_job(
        {
            "incident_id": f"inc-{service_name}",
            "request_payload": {
                "service_name": service_name,
                "uptime_description": "healthcheck timeout from monitor",
            },
        }
    )


def test_ensure_report_with_fallback_uses_agent_json_payload():
    job = _job()
    agent_output = """
The analysis is complete.
```json
{
  "summary_text": "Database connection pool starvation caused API timeouts.",
  "root_cause_hypotheses": [
    {
      "hypothesis": "A spike exhausted DB pool.",
      "confidence": 0.77,
      "evidence_refs": ["db pool metrics"]
    }
  ],
  "suggested_actions": [
    {
      "title": "Restart API workers",
      "description": "Drain stuck requests and refresh DB sessions.",
      "suggested_command": "kubectl rollout restart deployment/api-service"
    }
  ]
}
```
"""

    ensure_report_with_fallback(job, agent_output=agent_output)
    report = memory_db.get_report(job["id"])

    assert report is not None
    assert report["summary_text"] == "Database connection pool starvation caused API timeouts."
    assert "without a structured report" not in report["summary_text"]
    assert report["confidence"] == pytest.approx(0.77, abs=1e-6)
    assert report["report_json"]["suggested_actions"][0]["suggested_command"] == (
        "kubectl rollout restart deployment/api-service"
    )


def test_ensure_report_with_fallback_uses_plain_text_summary():
    job = _job("payments-api")
    agent_output = """
Payment API requests fail with upstream timeout after queue growth.

Evidence indicates worker saturation and stale DB sessions.
"""

    ensure_report_with_fallback(job, agent_output=agent_output)
    report = memory_db.get_report(job["id"])

    assert report is not None
    assert report["summary_text"].startswith(
        "Payment API requests fail with upstream timeout after queue growth."
    )
    assert "without a structured report" not in report["summary_text"]
    fallback_actions = report["report_json"]["suggested_actions"]
    assert fallback_actions == []


def test_ensure_report_with_fallback_promotes_error_like_output_to_error_report():
    job = _job("api-connectivity")
    ensure_report_with_fallback(
        job,
        agent_output="Investigation failed: connection refused while contacting Gemini API",
    )
    report = memory_db.get_report(job["id"])

    assert report is not None
    assert report["summary_text"].startswith("Gemini API request failed for api-connectivity")
    assert report["confidence"] == pytest.approx(0.1, abs=1e-6)
    assert report["report_json"]["suggested_actions"] != []
    assert "## Investigation Steps" in report["report_json"]["summary_markdown"]
    assert "## Solution Suggestions" in report["report_json"]["summary_markdown"]


def test_agent_worker_calls_investigate_and_writes_report():
    job = _job("checkout-api")
    worker = AgentWorker(poll_interval_sec=0.01)
    calls: list[str] = []

    class StubAgent:
        async def investigate(self, incident_id: str) -> str:
            calls.append(incident_id)
            worker.stop_event.set()
            return "Checkout API crashed after DB failover event."

    worker.agent = StubAgent()
    asyncio.run(asyncio.wait_for(worker.run(), timeout=1.0))

    assert calls == [job["incident_id"]]
    assert memory_db.jobs[job["id"]]["status"] == "completed"
    report = memory_db.get_report(job["id"])
    assert report is not None
    assert report["summary_text"].startswith("Checkout API crashed after DB failover event.")


def test_agent_worker_marks_job_failed_when_agent_raises():
    job = _job("search-api")
    worker = AgentWorker(poll_interval_sec=0.01)

    class FailingAgent:
        async def investigate(self, incident_id: str) -> str:
            worker.stop_event.set()
            raise RuntimeError("Gemini investigation request failed: connection refused")

    worker.agent = FailingAgent()
    asyncio.run(asyncio.wait_for(worker.run(), timeout=1.0))

    stored_job = memory_db.jobs[job["id"]]
    assert stored_job["status"] == "failed"
    assert "Gemini investigation request failed" in stored_job["error"]
    report = memory_db.get_report(job["id"])
    assert report is not None
    assert report["summary_text"].startswith("Gemini API request failed for search-api")
    assert "## Problems Found" in report["report_json"]["summary_markdown"]
