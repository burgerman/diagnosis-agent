import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException
from .schemas import UptimeKumaJobCreate, JobCreatedResponse
from .memory.store import memory_db
from .core.worker import AgentWorker

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create the worker task
    task = asyncio.create_task(worker.run())
    yield
    # Shutdown: Signal the worker to stop and wait for it
    await worker.stop()
    await task

worker = AgentWorker()
app = FastAPI(title="Reasoning Agent API (In-Memory)", lifespan=lifespan)

@app.post("/api/v1/jobs", response_model=JobCreatedResponse)
async def create_job(payload: UptimeKumaJobCreate):
    data = payload.to_internal()
    job = memory_db.create_job({
        "incident_id": data.incident_id,
        "request_payload": data.model_dump(mode="json")
    })
    return {"job_id": job["id"], "status": "queued"}

@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    job = memory_db.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/api/v1/jobs/{job_id}/result")
async def get_result(job_id: str):
    report = memory_db.get_report(job_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report

@app.get("/health")
def health():
    return {"status": "alive", "storage": "in-memory"}
