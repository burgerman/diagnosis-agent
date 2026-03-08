import os
from typing import Annotated, Any, get_args, get_origin, get_type_hints

import pytest
from dotenv import load_dotenv

from diagnosis_agent.memory.store import memory_db
from diagnosis_agent.tools.agent_tools import update_investigation_report


load_dotenv(dotenv_path=".env.test", override=True)
os.environ.setdefault("GEMINI_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def reset_memory_store():
    memory_db.jobs.clear()
    memory_db.reports.clear()
    yield
    memory_db.jobs.clear()
    memory_db.reports.clear()


def _annotated_base(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def test_update_report_annotations_are_runtime_safe_for_tooling():
    annotations = get_type_hints(update_investigation_report, include_extras=True)
    hypotheses_type = _annotated_base(annotations["hypotheses"])
    actions_type = _annotated_base(annotations["actions"])

    assert isinstance([], hypotheses_type)
    assert isinstance([], actions_type)


def test_update_report_filters_invalid_items_and_coerces_confidence():
    job = memory_db.create_job(
        {
            "incident_id": "inc-report-tool",
            "request_payload": {"service_name": "api-service"},
        }
    )

    result = update_investigation_report(
        incident_id="inc-report-tool",
        summary="Tool-driven investigation summary",
        hypotheses=[
            {"hypothesis": "DB saturation", "confidence": "0.6"},
            "bad-hypothesis-item",
        ],
        actions=[
            {"title": "Scale workers", "description": "Increase worker count."},
            "bad-action-item",
        ],
    )

    assert result == "Report updated successfully in memory."
    report = memory_db.get_report(job["id"])
    assert report is not None
    assert report["confidence"] == pytest.approx(0.6, abs=1e-6)
    assert report["report_json"]["root_cause_hypotheses"] == [
        {"hypothesis": "DB saturation", "confidence": "0.6"}
    ]
    assert report["report_json"]["suggested_actions"] == [
        {"title": "Scale workers", "description": "Increase worker count."}
    ]
