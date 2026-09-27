"""Optional OpenAI Responses API adapter; imported only for explicit live runs."""

from __future__ import annotations

from typing import Any

from .agent import FunctionCall, ModelTurn, MAX_OUTPUT_TOKENS_PER_TURN, SYSTEM_INSTRUCTIONS
from .policy import CONFIDENCE, DIAGNOSES
from .tools import TOOL_DEFINITIONS


FINAL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "enum": sorted(DIAGNOSES)},
        "confidence": {"type": "string", "enum": sorted(CONFIDENCE)},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["diagnosis", "confidence", "evidence_refs", "unknowns", "rationale"],
    "additionalProperties": False,
}


class OpenAIResponsesBackend:
    def __init__(self, model: str, *, client: Any | None = None):
        if not model:
            raise ValueError("model_required")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("install agentic_ops/requirements.txt for live model calls") from exc
            client = OpenAI(max_retries=0, timeout=25.0)
        self.client = client
        self.model = model

    def respond(self, input_items: list[Any]) -> ModelTurn:
        response = self.client.responses.create(
            model=self.model,
            instructions=SYSTEM_INSTRUCTIONS,
            input=input_items,
            tools=TOOL_DEFINITIONS,
            parallel_tool_calls=False,
            max_output_tokens=MAX_OUTPUT_TOKENS_PER_TURN,
            text={"format": {
                "type": "json_schema",
                "name": "incident_diagnosis",
                "strict": True,
                "schema": FINAL_OUTPUT_SCHEMA,
            }},
            store=False,
        )
        output_items = list(response.output)
        calls = []
        for item in output_items:
            if getattr(item, "type", None) == "function_call":
                calls.append(FunctionCall(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=item.arguments,
                ))
        usage = getattr(response, "usage", None)
        return ModelTurn(
            status=getattr(response, "status", "unknown"),
            function_calls=calls,
            output_items=output_items,
            output_text=getattr(response, "output_text", None),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )
