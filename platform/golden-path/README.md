# Golden path: create and release a service

This directory is the first usable slice of an Internal Developer Platform
(IDP) for the D2C platform. It turns the platform contract into a repeatable
service creation path instead of asking each team to copy deployment files by
hand.

## What the path provides

```text
service request
  -> scaffolded repository contract
  -> CI: tests, dependency audit, image build, manifest render
  -> immutable image reference
  -> Argo CD sync
  -> Argo Rollouts canary + Prometheus analysis
  -> stable promotion or automatic rollback
```

The generated service includes:

- an owner and service catalog entry;
- a non-root, read-only-root-filesystem container baseline;
- health probes, resource requests, and a canary Rollout;
- stable/canary Services, a ServiceMonitor, and Prometheus error-rate/p95
  latency analysis templates;
- a pull-request/push workflow that validates Python, Docker, and Kustomize.

## Try it locally

```bash
python scripts/scaffold_service.py \
  --name catalog-api \
  --owner team-d2c-platform \
  --port 8080 \
  --output-dir /tmp/catalog-api

cd /tmp/catalog-api
python -m pytest -q
kubectl kustomize k8s/base >/tmp/catalog-api.yaml
```

The scaffold refuses to overwrite a non-empty directory unless `--force` is
explicitly supplied. The generated image is intentionally a placeholder. A
real repository onboarding PR must replace it with the registry's immutable
commit-SHA image and add the Argo CD `Application` manifest for its environment.

## Platform guardrails

The scaffold is deliberately small, but it encodes the rules that matter at
the platform boundary:

1. Git is the source of truth for application and deployment changes.
2. CI owns tests and manifest validation; CD owns promotion.
3. Images are promoted by immutable digest/SHA, never by `latest`.
4. Pods run without a service-account token unless they explicitly need one.
5. Rollouts require health, error-rate, latency, and data-integrity analysis.
6. A missing or unavailable integrity signal fails the canary closed.
7. Each service declares an owner so operational alerts have a human route.

The generated service implements the generic HTTP signals. A transactional
service must add its domain-specific integrity metric and AnalysisTemplate
before production onboarding; the platform must never treat a synthetic
`1` gauge as proof that business data is correct.

This is an IDP seed rather than a claim that a full Backstage installation is
already running. The next platform increment would register this scaffold as a
Backstage/Port template and connect the generated repository to the central
reusable workflows and Argo CD application set.
