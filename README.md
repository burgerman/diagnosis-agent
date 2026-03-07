# Diagnosis Agent

A log diagnosis agent that leverages Google's Generative AI to autonomously investigate production incidents, analyze logs, search codebases, and formulate root causes.

## Table of Contents
- [Features](#features)
- [Architecture](#architecture)
- [Setup and Installation](#setup-and-installation)
- [Configuration](#configuration)
- [API Endpoints](#api-endpoints)
- [Usage](#usage)

## Features

*   **Autonomous Incident Triage:** Utilizes a Reasoning Agent (ReAct pattern) to investigate incidents.
*   **Log Analysis:** Processes log snippets to extract relevant keywords and context.
*   **Codebase Search:** Employs a `SelectiveCodeRetriever` to search for relevant code logic based on incident context.
*   **Root Cause Formulation:** Iteratively gathers evidence to confidently formulate root cause hypotheses.
*   **API Interface:** Provides a FastAPI application for submitting incident jobs and retrieving investigation reports.
*   **In-Memory Store:** Uses an in-memory database for job management and report storage.

## Architecture

The project is structured around several key components:

*   **`main.py`**: The entry point for the FastAPI application, defining API routes for job creation, status retrieval, and report fetching.
*   **`core/worker.py`**: Contains the `AgentWorker` responsible for polling the in-memory store for new jobs and orchestrating the `ReasoningAgent` to investigate incidents.
*   **`agent/core.py`**: Implements the `ReasoningAgent`, an AI agent that uses Google's Generative AI and function calling to perform the core incident investigation logic. It follows a ReAct (Reasoning and Acting) pattern.
*   **`memory/store.py`**: Provides an `InMemoryStore` singleton for managing incident jobs and investigation reports.
*   **`tools/agent_tools.py`**: Defines the tools (`search_code`, `read_incident_context`, `update_investigation_report`) that the `ReasoningAgent` can invoke during its investigation loop.
*   **`tools/retriever_logic.py`**: Implements the `SelectiveCodeRetriever`, which acts as the "eyes" of the agent, searching the codebase for relevant files and extracting code excerpts.
*   **`config.py`**: Manages application settings, including API keys, model names, and context limits, loaded from environment variables or a `.env` file.
*   **`schemas.py`**: Defines Pydantic models for data validation, including `UptimeKumaJobCreate` (for incoming incident data), `AnalysisJobCreate` (internal representation), and `JobCreatedResponse`.

## Setup and Installation

### Prerequisites

*   Python 3.11 or higher.

### Dependencies

This project uses `uv` for dependency management.

1.  **Install `uv`**: If you don't have `uv` installed, you can install it using pip:
    ```bash
    pip install uv
    ```
    Or refer to the [uv documentation](https://astral.sh/uv/install/) for other installation methods.

2.  **Install project dependencies**:
    ```bash
    uv sync
    ```

### Environment Variables

Create a `.env` file in the project root directory and populate it with the necessary environment variables:

```
GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
```

Replace `"YOUR_GEMINI_API_KEY"` with your actual Google Gemini API key. You can obtain one from the [Google AI Studio](https://makersuite.google.com/app/apikey).

## Configuration

The `config.py` file defines the application's settings. These settings can be configured via environment variables or a `.env` file. Key configurable settings include:

*   `GEMINI_API_KEY`: Your API key for Google Gemini.
*   `GEMINI_MODEL`: The Gemini model to use (default: `gemini-2.0-flash`).
*   `MAX_LOG_SNIPPETS`: Maximum number of log snippets to process.
*   `MAX_CONTEXT_FILES`: Maximum number of context files to retrieve.
*   `MAX_CONTEXT_EXCERPT_CHARS`: Maximum characters for code excerpts.
*   `ALLOWED_READ_ROOTS`: Comma-separated list of directories the agent is allowed to read (e.g., `src,services,config`).

## API Endpoints

The API is built using FastAPI and provides the following endpoints:

*   **`POST /api/v1/jobs`**
    *   **Description:** Submits a new incident investigation job.
    *   **Request Body:** `UptimeKumaJobCreate` schema (e.g., from an Uptime Kuma webhook).
    *   **Response:** `JobCreatedResponse` containing the `job_id` and `status` ("queued").

*   **`GET /api/v1/jobs/{job_id}`**
    *   **Description:** Retrieves the current status and details of a specific investigation job.
    *   **Response:** Job details from the in-memory store.

*   **`GET /api/v1/jobs/{job_id}/result`**
    *   **Description:** Retrieves the final investigation report for a completed job.
    *   **Response:** Investigation report details.

*   **`GET /health`**
    *   **Description:** Health check endpoint.
    *   **Response:** `{"status": "alive", "storage": "in-memory"}`

## Usage

### Running the Application

To run the FastAPI application, ensure you have set up your environment variables and installed dependencies.

```bash
uvicorn src.diagnosis_agent.main:app --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`.

### Submitting an Incident (Example)

You can use `curl` or any API client to submit an incident job:

```bash
curl -X POST "http://localhost:8000/api/v1/jobs" 
     -H "Content-Type: application/json" 
     -d '{
           "monitor": "my-service-monitor",
           "status": "down",
           "msg": "Service is down, critical errors in logs.",
           "url": "http://my-service.com",
           "time": "2023-10-27T10:00:00Z",
           "log_snippets": [
             {
               "timestamp": "2023-10-27T09:59:00Z",
               "source": "backend-service",
               "line": "ERROR: Database connection failed: Connection refused"
             },
             {
               "timestamp": "2023-10-27T09:59:05Z",
               "source": "backend-service",
               "line": "CRITICAL: Unable to process request, shutting down."
             }
           ],
           "metadata": {"team": "devops", "severity": "P1"}
         }'
```