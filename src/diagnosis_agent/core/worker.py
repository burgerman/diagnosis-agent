from __future__ import annotations
import asyncio
import logging
from ..memory.store import memory_db
from ..agent.core import ReasoningAgent

logger = logging.getLogger(__name__)

class AgentWorker:
    """Decoupled worker that polls the in-memory store."""
    def __init__(self, poll_interval_sec: float = 1.0):
        self.poll_interval = poll_interval_sec
        self.stop_event = asyncio.Event()
        self.agent = ReasoningAgent()

    async def run(self):
        logger.info("Agent Worker started (In-Memory Context)")
        while not self.stop_event.is_set():
            job = memory_db.get_queued_job()
            if job:
                job_id = job["id"]
                incident_id = job["incident_id"]
                try:
                    memory_db.update_job(job_id, {"status": "running", "started_at": asyncio.get_event_loop().time()})
                    
                    # Run the reasoning agent
                    await self.agent.investigate(incident_id)
                    
                    memory_db.update_job(job_id, {"status": "completed", "progress": 100})
                except Exception as e:
                    logger.exception("Agent job %s failed", job_id)
                    memory_db.update_job(job_id, {"status": "failed", "error": str(e)})
            else:
                await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self.stop_event.set()
