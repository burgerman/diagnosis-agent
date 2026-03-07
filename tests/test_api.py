import os
import json
import pytest
from unittest.mock import patch
from dotenv import load_dotenv

# Load the test-specific environment variables before importing the app
load_dotenv(dotenv_path=".env.test", override=True)

from fastapi.testclient import TestClient
import diagnosis_agent.main

@pytest.fixture
def client():
    # Use patch as a context manager to ensure the worker doesn't run during startup
    with patch("diagnosis_agent.core.worker.AgentWorker.run", return_value=None):
        with TestClient(diagnosis_agent.main.app) as c:
            yield c

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "storage": "in-memory"}

def test_create_job(client):
    sample_input = {
        "monitor": "test-service",
        "status": "DOWN",
        "msg": "connection refused",
        "url": "https://example.com",
        "time": "2026-03-07T12:00:00Z"
    }
    
    # 1. Post a new job
    response = client.post("/api/v1/jobs", json=sample_input)
    assert response.status_code == 200
    data = response.json()
    print(f"response: {data}")
    assert "job_id" in data
    assert data["status"] == "queued"
    
    job_id = data["job_id"]
    
    # 2. Get the job status
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    job_data = response.json()
    assert job_data["id"] == job_id
    assert job_data["status"] == "queued"
    assert job_data["incident_id"].startswith("inc-test-service-")
    assert job_data["request_payload"]["service_name"] == "test-service"

def test_create_job_from_sample_file(client):
    with open("src/sources/sample_input.json", "r") as f:
        sample_input = json.load(f)
    
    response = client.post("/api/v1/jobs", json=sample_input)
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "queued"

def test_get_nonexistent_job(client):
    response = client.get("/api/v1/jobs/nonexistent-id")
    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}

def test_get_nonexistent_result(client):
    response = client.get("/api/v1/jobs/nonexistent-id/result")
    assert response.status_code == 404
    assert response.json() == {"detail": "Report not found"}
