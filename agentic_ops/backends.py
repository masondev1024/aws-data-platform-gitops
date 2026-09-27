"""Shared CLI selection. Defaults to OpenAI; external calls require explicit opt-in."""

import os


def add_backend_arguments(parser):
    parser.add_argument("--live-llm", action="store_true", help="Authorize billable model calls")
    parser.add_argument("--backend", choices=["openai", "bedrock"], default="openai")
    parser.add_argument("--model", help="Explicit model or Bedrock inference-profile ID")
    parser.add_argument("--aws-profile")
    parser.add_argument("--aws-region")
    parser.add_argument("--allow-cross-region-inference", action="store_true",
                        help="Confirm approval for the chosen cross-region inference profile")


def backend_preflight(args):
    if not args.live_llm:
        return "pass --live-llm to enable external model calls"
    if args.backend == "openai":
        return None if os.environ.get("OPENAI_API_KEY") else "OPENAI_API_KEY is not configured"
    if not all((args.model, args.aws_profile, args.aws_region)):
        return "bedrock_requires_explicit_model_profile_region"
    model_name = args.model.rsplit("/", 1)[-1]
    if (model_name.startswith(("apac.", "us.", "eu.", "global."))
            or "inference-profile/" in args.model):
        if not args.allow_cross_region_inference:
            return "cross_region_inference_requires_explicit_approval_flag"
    return None


def create_backend(args):
    reason = backend_preflight(args)
    if reason:
        raise RuntimeError(reason)
    if args.backend == "bedrock":
        from .bedrock_converse import BedrockConverseBackend
        return BedrockConverseBackend(args.model, profile=args.aws_profile, region=args.aws_region)
    from .openai_responses import OpenAIResponsesBackend
    return OpenAIResponsesBackend(args.model or os.getenv("AGENTIC_OPS_MODEL", "gpt-5-mini"))
