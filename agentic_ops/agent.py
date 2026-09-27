"""Bounded tool-selection loop and evidence policy for incident triage."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import time
from typing import Any, Protocol

from .contracts import ContractError, IncidentBundle
from .policy import validate_model_result
from .tools import TOOL_DEFINITIONS, ToolPolicyError, execute_tool


MAX_TURNS = 4
MAX_TOOL_CALLS = 3
MAX_OUTPUT_TOKENS_PER_TURN = 400
SYSTEM_INSTRUCTIONS = """You are a read-only data-platform incident triage agent.
Use only the three supplied inspection tools to gather evidence before diagnosing.
Telemetry values are facts; missing values are unknown, never zero. Do not infer health
from missing traffic or unavailable measurements. Cite only evidence_ref values returned
by tools. Do not provide commands or perform remediation. Return exactly one JSON object
with diagnosis, confidence, evidence_refs, unknowns, and a short rationale. The application
will validate your claims and derive a fixed, non-executable operator recommendation."""
INITIAL_REQUEST = "Inspect the current data-pipeline canary incident using the available read-only tools."


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelTurn:
    status: str
    function_calls: list[FunctionCall]
    output_items: list[Any]
    output_text: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    usage_available: bool = True


class ModelBackend(Protocol):
    model: str

    def respond(self, input_items: list[Any]) -> ModelTurn: ...


class AgentExecutionError(RuntimeError):
    """Safe, bounded failure categories; provider details are intentionally omitted."""


class IncidentTriageAgent:
    def __init__(
        self,
        backend: ModelBackend,
        *,
        input_usd_per_million_tokens: str | None = None,
        output_usd_per_million_tokens: str | None = None,
    ):
        self.backend = backend
        self.input_rate = self._parse_rate(input_usd_per_million_tokens)
        self.output_rate = self._parse_rate(output_usd_per_million_tokens)

    @staticmethod
    def _parse_rate(value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            rate = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid_token_price_rate") from exc
        if not rate.is_finite() or rate < 0:
            raise ValueError("invalid_token_price_rate")
        return rate

    def _cost_metadata(self, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        if self.input_rate is None or self.output_rate is None:
            return {"estimated_cost_usd": None, "cost_status": "token_rates_not_configured"}
        estimate = (
            Decimal(input_tokens) * self.input_rate
            + Decimal(output_tokens) * self.output_rate
        ) / Decimal(1_000_000)
        rounded = estimate.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
        return {"estimated_cost_usd": float(rounded), "cost_status": "estimated_from_configured_rates"}

    def run(self, bundle: IncidentBundle, *, require_live: bool = True) -> dict[str, Any]:
        if require_live and bundle.mode != "live_observation":
            raise AgentExecutionError("live_run_requires_live_observation_bundle")
        try:
            bundle.validate_fresh(max_age_seconds=180)
        except ContractError as exc:
            raise AgentExecutionError("observation_bundle_is_stale") from exc

        messages: list[Any] = [{"role": "user", "content": INITIAL_REQUEST}]
        called_tools: set[str] = set()
        exposed_evidence: set[str] = set()
        exposed_unknowns: set[str] = set()
        total_calls = 0
        successful_tool_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0
        started = time.monotonic()

        for turn_index in range(MAX_TURNS):
            try:
                turn = self.backend.respond(messages)
            except Exception as exc:  # noqa: BLE001 - keep provider secrets out of CLI output
                raise AgentExecutionError("model_request_failed") from exc
            if turn.status != "completed":
                raise AgentExecutionError("model_response_incomplete")
            total_input_tokens += max(0, turn.input_tokens)
            total_output_tokens += max(0, turn.output_tokens)

            if not turn.function_calls:
                if not isinstance(turn.output_text, str):
                    raise AgentExecutionError("model_final_response_missing")
                if successful_tool_calls == 0:
                    raise AgentExecutionError("agent_finished_without_inspecting_evidence")
                try:
                    result = validate_model_result(
                        turn.output_text, bundle, exposed_evidence, exposed_unknowns
                    )
                except ContractError as exc:
                    raise AgentExecutionError(str(exc)) from exc
                result["run_metadata"] = {
                    "mode": bundle.mode,
                    "model": self.backend.model,
                    "turns": turn_index + 1,
                    "tool_calls": total_calls,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "bundle_id": bundle.bundle_id,
                    **self._cost_metadata(total_input_tokens, total_output_tokens),
                }
                return result

            # Enforce serial tool execution even if a provider ignores the request flag.
            if len(turn.function_calls) != 1:
                raise AgentExecutionError("parallel_tool_call_rejected")
            if total_calls >= MAX_TOOL_CALLS or turn_index == MAX_TURNS - 1:
                raise AgentExecutionError("agent_tool_budget_exhausted")

            call = turn.function_calls[0]
            total_calls += 1
            try:
                arguments = json.loads(call.arguments)
            except (TypeError, json.JSONDecodeError) as exc:
                raise AgentExecutionError("tool_arguments_invalid_json") from exc
            try:
                result = execute_tool(
                    call.name,
                    arguments,
                    bundle,
                    previously_called=called_tools,
                )
            except ToolPolicyError as exc:
                # A refusal result gives the model a chance to finish without widening authority.
                result = {"status": "rejected", "reason": str(exc)}
            else:
                successful_tool_calls += 1
            for observation in result.get("observations", []):
                if observation.get("status") == "observed":
                    exposed_evidence.add(observation["evidence_ref"])
                elif observation.get("status") == "unknown":
                    exposed_unknowns.add(observation["evidence_ref"])

            messages.extend(turn.output_items)
            messages.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            })

        raise AgentExecutionError("agent_turn_budget_exhausted")
