#!/usr/bin/env python3
"""Collect a bounded, read-only incident snapshot for the triage agent."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(1, str(REPO_ROOT / "scripts"))
import platform_doctor  # noqa: E402

from agentic_ops.bundle import build_incident_bundle  # noqa: E402
from agentic_ops.prometheus import collect_prometheus  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Explicit kubectl context name")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--prometheus-port", type=int, default=9090,
                        help="Loopback port of an operator-started Prometheus port-forward")
    parser.add_argument("--output", help="Optional JSON file; otherwise print to stdout")
    args = parser.parse_args(argv)
    if not 1 <= args.prometheus_port <= 65535:
        parser.error("prometheus-port must be between 1 and 65535")
    # Reuse the existing safe context/namespace validation without permitting AWS calls.
    platform_doctor.parse_args(["--context", args.context, "--namespace", args.namespace])
    return args


def collect(args, *, doctor_runner=platform_doctor.run_json, prometheus_reader=None):
    doctor_args = platform_doctor.parse_args([
        "--context", args.context,
        "--namespace", args.namespace,
        "--execute",
    ])
    platform_report = platform_doctor.collect(doctor_args, runner=doctor_runner)
    kwargs = {"port": args.prometheus_port}
    if prometheus_reader is not None:
        kwargs["read_json"] = prometheus_reader
    prom_observations = collect_prometheus(**kwargs)
    return build_incident_bundle(platform_report, prom_observations).to_dict()


def write_private_output(path: str, rendered: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(rendered)


def main(argv=None):
    args = parse_args(argv)
    bundle = collect(args)
    rendered = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            write_private_output(args.output, rendered)
        except FileExistsError:
            print(json.dumps({"status": "failed", "reason": "refusing_to_overwrite_output"}), file=sys.stderr)
            return 2
        except OSError:
            print(json.dumps({"status": "failed", "reason": "output_write_failed"}), file=sys.stderr)
            return 2
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
