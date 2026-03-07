from __future__ import annotations
import asyncio
import logging
from typing import Any

from google import genai
from google.genai import types

from ..config import get_settings
from ..tools.agent_tools import AGENT_TOOLS

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
            "You are an autonomous Production Incident Triage Agent. "
            "Your goal is to investigate an incident ID by gathering context, "
            "searching the codebase for relevant logic, and formulating a root cause. "
            "1. Start by reading the incident context using `read_incident_context`. "
            "2. Identify keywords from logs and search the code using `search_code`. "
            "3. Iterate until you have high confidence. "
            "4. Finalize by calling `update_investigation_report`. "
            "Be precise, surgical, and prioritize safety."
        )

    async def investigate(self, incident_id: str):
        """The main autonomous loop."""
        logger.info("Starting investigation for %s", incident_id)
        
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

        prompt = f"Investigate incident: {incident_id}. Gather evidence and update the report."
        
        try:
            # The 'automatic_function_calling' handles the loop of tool executions
            response = await chat.send_message(prompt)
            return response.text
        except Exception as e:
            logger.exception("Agent loop failed")
            return f"Investigation failed: {str(e)}"
