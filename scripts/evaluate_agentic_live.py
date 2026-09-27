#!/usr/bin/env python3
"""Opt-in real-model evaluation on replay fixtures OR a fresh collected observation.

No live calls are made without --live-llm. Replay success is not live validation.
"""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentic_ops.backends import add_backend_arguments, backend_preflight, create_backend  # noqa: E402
from agentic_ops.contracts import ContractError, IncidentBundle, parse_timestamp, utc_now  # noqa: E402
from agentic_ops.live_evaluation import CASES, evaluate_case, replay_bundle  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["replay-model-eval", "fresh-real-observation"], required=True)
    parser.add_argument("--case", choices=[case.name for case in CASES], action="append",
                        help="Replay subset; omitted means exactly six fixed cases, once each")
    parser.add_argument("--bundle", help="Fresh collector JSON; never rebased or rewritten")
    parser.add_argument("--private-output", help="Optional safe metrics JSON, mode 0600, exclusive create")
    add_backend_arguments(parser)
    args = parser.parse_args(argv)
    if args.mode == "fresh-real-observation" and (not args.bundle or args.case):
        parser.error("fresh mode requires --bundle and disallows --case")
    if args.mode == "replay-model-eval" and args.bundle:
        parser.error("replay mode disallows --bundle")
    return args


def main(argv=None):
    args = parse_args(argv)
    reason = backend_preflight(args)
    if reason:
        print(json.dumps({"status": "not_run", "reason": reason}))
        return 2
    output = None
    try:
        bundle = None
        if args.mode == "fresh-real-observation":
            bundle = IncidentBundle.from_dict(json.loads(Path(args.bundle).read_text(encoding="utf-8")))
            if bundle.mode != "live_observation":
                raise ContractError("live_run_requires_live_observation_bundle")
            bundle.validate_fresh()
            age = (utc_now() - parse_timestamp(bundle.collected_at)).total_seconds()
            if not -30 <= age <= 180:
                raise ContractError("observation_bundle_is_stale")
        # Reserve private output before any billable call; never overwrite a prior run.
        if args.private_output:
            fd = os.open(args.private_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            output = os.fdopen(fd, "w", encoding="utf-8")
        backend = create_backend(args)
        if bundle is not None:
            rows = [evaluate_case(backend, bundle)]
        else:
            selected = [case for case in CASES if not args.case or case.name in args.case]
            rows = [evaluate_case(backend, replay_bundle(case), case=case) for case in selected]
        report = {"status": "completed", "mode": args.mode, "backend": args.backend,
                  "model": backend.model, "pass": all(row["pass"] for row in rows), "cases": rows}
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if output:
            output.write(rendered + "\n")
        print(rendered)
        return 0 if report["pass"] else 1
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ContractError) else "evaluation_setup_failed"
        print(json.dumps({"status": "failed", "reason": reason}))
        return 2
    finally:
        if output:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
