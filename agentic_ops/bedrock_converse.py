"""Optional, bounded Bedrock Converse adapter; no AWS calls at import time.

Protocol: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
Tool results use the original toolUseId in a subsequent user message.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .agent import FunctionCall, ModelTurn, MAX_OUTPUT_TOKENS_PER_TURN, SYSTEM_INSTRUCTIONS
from .openai_responses import FINAL_OUTPUT_SCHEMA
from .policy import HEALTH_REQUIRED
from .tools import TOOL_DEFINITIONS


MAX_REQUEST_CHARACTERS = 64_000


class BedrockConverseBackend:
    def __init__(self, model: str, *, profile: str, region: str, client: Any = None):
        if not all(isinstance(value, str) and value.strip() for value in (model, profile, region)):
            raise ValueError("bedrock_model_profile_region_required")
        self.model = model
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RuntimeError("install agentic_ops/requirements-bedrock.txt") from exc
            client = boto3.Session(profile_name=profile, region_name=region).client(
                "bedrock-runtime", region_name=region,
                config=Config(connect_timeout=5, read_timeout=25,
                              retries={"total_max_attempts": 1, "mode": "standard"}),
            )
        self.client = client

    @staticmethod
    def _messages(input_items: list[Any]) -> list[dict]:
        messages = []
        for item in input_items:
            if not isinstance(item, dict):
                raise ValueError("invalid_converse_history")
            if item.get("type") == "function_call_output":
                result = json.loads(item["output"])
                messages.append({"role": "user", "content": [{"toolResult": {
                    "toolUseId": item["call_id"],
                    "content": [{"json": result}],
                    "status": "error" if result.get("status") == "rejected" else "success",
                }}]})
            elif item.get("role") in {"user", "assistant"}:
                content = item["content"]
                messages.append({"role": item["role"], "content": (
                    [{"text": content}] if isinstance(content, str) else content
                )})
            else:
                raise ValueError("invalid_converse_history")
        if len(json.dumps(messages, allow_nan=False)) > MAX_REQUEST_CHARACTERS:
            raise ValueError("converse_input_budget_exhausted")
        return messages

    def respond(self, input_items: list[Any]) -> ModelTurn:
        response = self.client.converse(
            modelId=self.model,
            system=[{"text": SYSTEM_INSTRUCTIONS +
                     "\nRequest only one tool per turn, each tool at most once. "
                     "You have at most three tool calls and four turns total. "
                     "Final output must be JSON only, without markdown, rationale <=280 characters. "
                     "A healthy claim requires positive traffic, 5xx ratio <0.01, p95 <0.5 seconds, "
                     "source ages <=120 seconds, database readiness 1, parity gap 0, Healthy rollout, "
                     "and all pods ready with a positive pod count. Cite every health reference: "
                     + json.dumps(sorted(HEALTH_REQUIRED)) + ". "
                     "For insufficient evidence, list the unknown evidence refs returned by tools. "
                     "Use this schema: " + json.dumps(FINAL_OUTPUT_SCHEMA)}],
            messages=self._messages(input_items),
            toolConfig={"tools": [{"toolSpec": {
                "name": item["name"], "description": item["description"],
                "inputSchema": {"json": item["parameters"]},
            }} for item in TOOL_DEFINITIONS], "toolChoice": {"auto": {}}},
            inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS_PER_TURN, "temperature": 0},
        )
        return self._translate(response)

    @staticmethod
    def _translate(response: Any) -> ModelTurn:
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        input_tokens = usage.get("inputTokens", 0) if isinstance(usage, dict) else 0
        output_tokens = usage.get("outputTokens", 0) if isinstance(usage, dict) else 0
        valid_usage = (isinstance(usage, dict) and {"inputTokens", "outputTokens"}.issubset(usage)
                       and all(type(n) is int and n >= 0 for n in (input_tokens, output_tokens)))
        if not valid_usage:
            input_tokens = output_tokens = 0

        def incomplete():
            return ModelTurn("incomplete", [], [], None, input_tokens, output_tokens, valid_usage)

        if not isinstance(response, dict) or not valid_usage:
            return incomplete()
        output = response.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return incomplete()
        content = message.get("content")
        if not isinstance(content, list) or not content:
            return incomplete()
        calls, texts, ids = [], [], set()
        for block in content:
            if not isinstance(block, dict) or len(block) != 1:
                return incomplete()
            if "text" in block and isinstance(block["text"], str):
                texts.append(block["text"])
            elif "toolUse" in block:
                call = block["toolUse"]
                if not isinstance(call, dict) or set(call) != {"toolUseId", "name", "input"}:
                    return incomplete()
                call_id, name = call["toolUseId"], call["name"]
                if (not isinstance(call_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", call_id)
                        or not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
                        or call_id in ids):
                    return incomplete()
                ids.add(call_id)
                try:
                    arguments = json.dumps(call["input"], allow_nan=False)
                except (ValueError, TypeError):
                    return incomplete()
                calls.append(FunctionCall(call_id, name, arguments))
            else:
                return incomplete()
        if response.get("stopReason") != ("tool_use" if calls else "end_turn"):
            return incomplete()
        return ModelTurn("completed", calls, [message], "".join(texts) or None,
                         input_tokens, output_tokens)
