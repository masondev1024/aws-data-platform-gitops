#!/usr/bin/env python3
"""Run an explicitly enabled live model triage over a fresh observation bundle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentic_ops.agent import AgentExecutionError, IncidentTriageAgent  # noqa: E402
from agentic_ops.contracts import ContractError, IncidentBundle  # noqa: E402
from agentic_ops.backends import add_backend_arguments, backend_preflight, create_backend  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="Fresh JSON output from collect_agentic_incident.py")
    add_backend_arguments(parser)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    reason = backend_preflight(args)
    if reason:
        print(json.dumps({"status": "not_run", "reason": reason}))
        return 2
    try:
        bundle = IncidentBundle.from_dict(json.loads(Path(args.bundle).read_text(encoding="utf-8")))
        if bundle.mode != "live_observation":
            raise ContractError("live_run_requires_live_observation_bundle")
        backend = create_backend(args)
        result = IncidentTriageAgent(
            backend,
            input_usd_per_million_tokens=os.getenv("AGENTIC_OPS_INPUT_USD_PER_1M"),
            output_usd_per_million_tokens=os.getenv("AGENTIC_OPS_OUTPUT_USD_PER_1M"),
        ).run(bundle, require_live=True)
    except Exception as exc:  # Provider setup errors may contain credential details.
        safe_error = str(exc) if isinstance(exc, (ContractError, AgentExecutionError)) else "agent_setup_failed"
        print(json.dumps({"status": "failed", "reason": safe_error}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "completed", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
