import pytest
from unittest.mock import patch
from dotenv import load_dotenv

# Load test env before importing the app module.
load_dotenv(dotenv_path=".env.test", override=True)

from fastapi.testclient import TestClient
import diagnosis_agent.main
from diagnosis_agent.memory.store import memory_db


def _uptime_payload(
    monitor: str = "test-service",
    status: str = "DOWN",
    msg: str = "connection refused",
) -> dict:
    return {
        "monitor": monitor,
        "status": status,
        "msg": msg,
        "url": "https://example.com",
        "time": "2026-03-07T12:00:00Z",
    }


@pytest.fixture(autouse=True)
def reset_memory_store():
    memory_db.jobs.clear()
    memory_db.reports.clear()
    yield
    memory_db.jobs.clear()
    memory_db.reports.clear()


@pytest.fixture
def client():
    # Prevent worker loop from running in API tests.
    with patch("diagnosis_agent.core.worker.AgentWorker.run", return_value=None):
        with TestClient(diagnosis_agent.main.app) as c:
            yield c


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "storage": "in-memory"}


def test_legacy_jobs_endpoints_still_work(client):
    create_response = client.post("/api/v1/jobs", json=_uptime_payload())
    assert create_response.status_code == 200

    data = create_response.json()
    assert "job_id" in data
    assert data["status"] == "queued"

    job_response = client.get(f"/api/v1/jobs/{data['job_id']}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["id"] == data["job_id"]
    assert job["status"] == "queued"
    assert job["incident_id"].startswith("inc-test-service-")

    assert client.get("/api/v1/jobs/missing-job").status_code == 404
    assert client.get("/api/v1/jobs/missing-job/result").status_code == 404


def test_analysis_alias_summary_result_and_download(client):
    create_response = client.post("/api/v1/analysis/jobs", json=_uptime_payload(monitor="alias-service"))
    assert create_response.status_code == 200
    job_id = create_response.json()["job_id"]

    legacy_job = client.get(f"/api/v1/jobs/{job_id}")
    analysis_job = client.get(f"/api/v1/analysis/jobs/{job_id}")
    assert legacy_job.status_code == 200
    assert analysis_job.status_code == 200
    assert analysis_job.json() == legacy_job.json()

    assert client.get(f"/api/v1/analysis/jobs/{job_id}/result").status_code == 404

    incident_id = analysis_job.json()["incident_id"]
    report_json = {
        "evidence": [{"snippet": "database lock detected"}],
        "suggested_actions": [{"suggested_command": "systemctl restart alias-service"}],
        "summary_text": "Recovered service after lock cleanup",
    }
    memory_db.upsert_report(
        {
            "job_id": job_id,
            "incident_id": incident_id,
            "summary_text": "Recovered service after lock cleanup",
            "report_status": "completed",
            "confidence": 0.91,
            "report_json": report_json,
        }
    )

    summary_response = client.get(f"/api/v1/analysis/jobs/{job_id}/summary")
    assert summary_response.status_code == 200
    assert summary_response.json() == {
        "incident_id": incident_id,
        "summary_text": "Recovered service after lock cleanup",
        "confidence": 0.91,
    }

    legacy_result = client.get(f"/api/v1/jobs/{job_id}/result")
    analysis_result = client.get(f"/api/v1/analysis/jobs/{job_id}/result")
    assert legacy_result.status_code == 200
    assert analysis_result.status_code == 200
    assert analysis_result.json() == legacy_result.json()

    download_response = client.get(f"/api/v1/analysis/jobs/{job_id}/download")
    assert download_response.status_code == 200
    assert download_response.headers["content-type"].startswith("application/json")
    assert f"analysis-report-{job_id}.json" in download_response.headers["content-disposition"]
    assert download_response.json() == report_json


def test_analysis_incidents_shape_and_mapping(client):
    running_job = memory_db.create_job(
        {
            "incident_id": "inc-running",
            "request_payload": {
                "service_name": "running-service",
                "uptime_status": "down",
                "uptime_description": "running fallback",
                "log_snippets": [{"line": "running payload log"}],
                "metadata": {"service_type": "service"},
            },
        }
    )
    memory_db.update_job(running_job["id"], {"status": "running"})

    failed_job = memory_db.create_job(
        {
            "incident_id": "inc-failed",
            "request_payload": {
                "service_name": "failed-service",
                "uptime_status": "down",
                "uptime_description": "failed fallback log",
                "log_snippets": [],
                "metadata": {"service_type": "database"},
            },
        }
    )
    memory_db.update_job(failed_job["id"], {"status": "failed"})

    degraded_job = memory_db.create_job(
        {
            "incident_id": "inc-degraded",
            "request_payload": {
                "service_name": "degraded-service",
                "uptime_status": "degraded",
                "uptime_description": "degraded fallback",
                "log_snippets": [{"line": "degraded payload log"}],
                "metadata": {},
            },
        }
    )
    memory_db.update_job(degraded_job["id"], {"status": "completed"})
    memory_db.upsert_report(
        {
            "job_id": degraded_job["id"],
            "incident_id": "inc-degraded",
            "summary_text": "degraded summary",
            "report_status": "completed",
            "confidence": 0.42,
            "report_json": {"suggested_actions": []},
        }
    )

    completed_job = memory_db.create_job(
        {
            "incident_id": "inc-completed",
            "request_payload": {
                "service_name": "completed-service",
                "uptime_status": "down",
                "uptime_description": "completed fallback",
                "log_snippets": [{"line": "completed payload log"}],
                "metadata": {"service_type": "api"},
            },
        }
    )
    memory_db.update_job(completed_job["id"], {"status": "completed"})
    memory_db.upsert_report(
        {
            "job_id": completed_job["id"],
            "incident_id": "inc-completed",
            "summary_text": "Restart completed-service after evidence review",
            "report_status": "completed",
            "confidence": 0.87,
            "report_json": {
                "evidence": [{"snippet": "critical stack trace line"}],
                "suggested_actions": [{"suggested_command": "docker restart completed-service"}],
            },
        }
    )

    response = client.get("/api/v1/analysis/incidents")
    assert response.status_code == 200

    incidents = {item["id"]: item for item in response.json()}
    assert set(incidents.keys()) == {"inc-running", "inc-failed", "inc-degraded", "inc-completed"}

    assert incidents["inc-running"]["status"] == "resolving"
    assert incidents["inc-running"]["logs"] == ["running payload log"]
    assert incidents["inc-running"]["confidence"] == 0.0
    assert incidents["inc-running"]["proposedFix"] is None

    assert incidents["inc-failed"]["status"] == "warning"
    assert incidents["inc-failed"]["logs"] == ["failed fallback log"]
    assert incidents["inc-failed"]["serviceType"] == "database"

    assert incidents["inc-degraded"]["status"] == "warning"
    assert incidents["inc-degraded"]["logs"] == ["degraded payload log"]
    assert incidents["inc-degraded"]["confidence"] == 0.42
    assert incidents["inc-degraded"]["proposedFix"] is None

    assert incidents["inc-completed"]["status"] == "issue"
    assert incidents["inc-completed"]["logs"] == ["critical stack trace line"]
    assert incidents["inc-completed"]["service"] == "completed-service"
    assert incidents["inc-completed"]["serviceType"] == "api"
    assert incidents["inc-completed"]["confidence"] == 0.87
    assert incidents["inc-completed"]["proposedFix"] == {
        "description": "Restart completed-service after evidence review",
        "steps": ["docker restart completed-service"],
    }


def test_analysis_cors_preflight(client):
    response = client.options(
        "/api/v1/analysis/incidents",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
