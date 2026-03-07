from __future__ import annotations
import asyncio
import logging
from typing import Any

from google import genai
from google.genai import types

from ..config import get_settings
from ..tools.agent_tools import (
    AGENT_TOOLS, 
    read_incident_context, 
    fetch_dynamic_log_snippet
)

logger = logging.getLogger(__name__)

class ReasoningAgent:
    """
    A 'real' AI agent that uses a reasoning loop (ReAct)
    via Google's native function calling.
    """
    def __init__(self):
        self.settings = get_settings()
        self.client = genai.Client(api_key=self.settings.gemini_api_key)
        self.model_id = self.settings.gemini_model
        
        self.system_instruction = (
            "You are a Senior Production Incident Engineer specializing in automated triage and root cause analysis. "
            "Your objective is to autonomously investigate production incidents with surgical precision, "
            "minimizing Mean Time to Recovery (MTTR) by providing actionable insights to the on-call team.\n\n"
            "OPERATIONAL PROTOCOL:\n"
            "1. CONTEXT ACQUISITION: Begin by retrieving the incident payload using `read_incident_context`. "
            "Extract the exact timestamp and monitor metadata to understand the scope.\n"
            "2. DYNAMIC LOG ANALYSIS: Use `fetch_dynamic_log_snippet` to perform a targeted temporal search. "
            "Analyze log lines immediately surrounding the failure to identify error patterns, stack traces, or state transitions.\n"
            "3. CODEBASE CORRELATION: Identify critical functions, classes, or modules mentioned in logs. "
            "Use `search_code` to retrieve relevant logic and understand how code behavior matches the observed failures.\n"
            "4. CAUSAL REASONING: Correlate logs with code logic. Identify whether the failure is a regression, "
            "a misconfiguration, an external dependency failure, or a logic error.\n"
            "5. FINAL REPORTING: Use `update_investigation_report` to deliver your findings. Your report must include:\n"
            "   - A concise summary of the incident impact and flow.\n"
            "   - Evidence-backed hypotheses with quantified confidence levels.\n"
            "   - Specific, low-risk remediation actions (e.g., config changes, rollback suggestions, or targeted code fixes).\n\n"
            "CONSTRAINTS:\n"
            "- Maintain professional, technical language appropriate for a high-stakes IT environment.\n"
            "- Never hallucinate code or log entries; rely strictly on tool outputs.\n"
            "- Prioritize safety: suggest non-destructive investigation or remediation steps first.\n"
            "- If evidence is inconclusive, state exactly what information is missing."
        )

    async def investigate(self, incident_id: str):
        """The main autonomous loop."""
        logger.info("Starting investigation for %s", incident_id)
        
        # Pre-fetch initial context and logs to reduce the number of agent turns
        incident_context = read_incident_context(incident_id)
        initial_logs = ""
        
        if "error" not in incident_context:
            # Try to fetch logs if we have a timestamp
            # Based on schemas.py, AnalysisJobCreate has detected_at
            # But the stored job in MemoryStore might have it in request_payload
            payload = incident_context.get("request_payload", {})
            target_time = payload.get("detected_at") or payload.get("time")
            
            if target_time:
                try:
                    initial_logs = fetch_dynamic_log_snippet(
                        log_directory=self.settings.log_directory,
                        target_timestamp_str=str(target_time)
                    )
                except Exception as e:
                    logger.warning("Failed to pre-fetch log snippets: %s", e)

        # We use 'tools' configuration in the SDK
        chat = self.client.aio.chats.create(
            model=self.model_id,
            config=types.GenerateContentConfig(
                system_instruction=self.system_instruction,
                tools=AGENT_TOOLS,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=False
                )
            )
        )

        prompt = f"Investigate incident: {incident_id}."
        if initial_logs:
            prompt += f"\n\nHere are some relevant log snippets found around the event time:\n{initial_logs}"
        
        prompt += "\n\nGather evidence and update the report."
        
        try:
            # The 'automatic_function_calling' handles the loop of tool executions
            response = await chat.send_message(prompt)
            return response.text
        except Exception as e:
            logger.exception("Agent loop failed")
            return f"Investigation failed: {str(e)}"
