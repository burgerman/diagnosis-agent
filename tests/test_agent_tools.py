import os
from typing import Any

import pytest
from dotenv import load_dotenv
from google.genai import types

from diagnosis_agent.memory.store import memory_db
from diagnosis_agent.tools.agent_tools import search_code, update_investigation_report


load_dotenv(dotenv_path=".env.test", override=True)
os.environ.setdefault("GEMINI_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def reset_memory_store():
    memory_db.jobs.clear()
    memory_db.reports.clear()
    yield
    memory_db.jobs.clear()
    memory_db.reports.clear()


def test_update_report_annotations_include_array_items_for_gemini_tooling():
    declaration = types.FunctionDeclaration.from_callable_with_api_option(
        callable=update_investigation_report,
        api_option="GEMINI_API",
    )
    params = declaration.parameters
    assert params is not None
    assert params.properties is not None

    hypotheses_schema = params.properties["hypotheses"]
    actions_schema = params.properties["actions"]
    markdown_schema = params.properties["summary_markdown"]

    assert hypotheses_schema.items is not None
    assert actions_schema.items is not None
    assert markdown_schema.type == types.Type.STRING


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
        summary_markdown=(
            "## Investigation Steps\n"
            "- Checked DB saturation indicators.\n\n"
            "## Problems Found\n"
            "- DB pool contention.\n\n"
            "## Other Important Info\n"
            "- Confidence score: 60%.\n\n"
            "## Solution Suggestions\n"
            "- Scale workers."
        ),
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
    assert "## Investigation Steps" in report["report_json"]["summary_markdown"]


def test_search_code_tool_executes_without_import_errors():
    results = search_code("api-service")
    assert isinstance(results, list)
