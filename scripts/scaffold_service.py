#!/usr/bin/env python3
"""Generate the minimum D2C platform service contract for a new repository."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from textwrap import dedent


SERVICE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,30}$")


TEMPLATES = {
    "README.md": """
        # __SERVICE_NAME__

        Owner: `__OWNER__`

        This service was created from the D2C platform golden path. Replace the
        placeholder image reference in `k8s/base/rollout.yaml` with an immutable
        registry SHA before onboarding it to Argo CD.

        ## Local verification

        ```bash
        python -m pytest -q
        docker build --pull --tag __SERVICE_NAME__:ci .
        kubectl kustomize k8s/base
        ```
    """,
    "catalog-info.yaml": """
        apiVersion: backstage.io/v1alpha1
        kind: Component
        metadata:
          name: __SERVICE_NAME__
          description: Service created from the D2C platform golden path.
          annotations:
            platform.d2c/delivery-strategy: argo-rollouts-canary
        spec:
          type: service
          lifecycle: production
          owner: group:__OWNER__
    """,
    "Dockerfile": """
        FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

        ENV PYTHONDONTWRITEBYTECODE=1 \\
            PYTHONUNBUFFERED=1
        WORKDIR /opt/service

        COPY app/requirements.txt ./requirements.txt
        RUN python -m pip install --no-cache-dir --requirement requirements.txt \\
            && useradd --create-home --uid 10001 service

        COPY app ./app
        USER 10001:10001
        EXPOSE __PORT__
        CMD ["gunicorn", "--bind", "0.0.0.0:__PORT__", "app.app:app"]
    """,
    "app/requirements.txt": """
        Flask==3.1.3
        gunicorn==23.0.0
        prometheus-client==0.22.1
    """,
    "app/requirements-dev.txt": """
        -r requirements.txt
        pytest==8.4.2
    """,
    "app/app.py": """
        from time import perf_counter

        from flask import Flask, Response, g, jsonify, request
        from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


        app = Flask(__name__)

        HTTP_REQUESTS = Counter(
            "http_requests",
            "Total HTTP requests handled by the service.",
            ("method", "route", "status"),
        )
        HTTP_REQUEST_DURATION = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route"),
        )


        def metric_route():
            return request.url_rule.rule if request.url_rule is not None else "unmatched"


        @app.before_request
        def start_request_timer():
            g.request_started_at = perf_counter()


        @app.after_request
        def record_request_metrics(response):
            if request.endpoint != "metrics":
                HTTP_REQUESTS.labels(
                    request.method, metric_route(), str(response.status_code)
                ).inc()
                HTTP_REQUEST_DURATION.labels(request.method, metric_route()).observe(
                    perf_counter() - g.get("request_started_at", perf_counter())
                )
            return response


        @app.get("/healthz")
        def healthz():
            return jsonify(status="ok")


        @app.get("/metrics")
        def metrics():
            return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)
    """,
    "app/tests/test_health.py": """
        from app.app import app


        def test_healthz():
            response = app.test_client().get("/healthz")

            assert response.status_code == 200
            assert response.get_json() == {"status": "ok"}
    """,
    ".github/workflows/ci.yaml": """
        name: Service CI

        on:
          pull_request:
          push:
            branches: [main]

        permissions:
          contents: read

        jobs:
          validate:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
                with:
                  python-version: "3.12"
                  cache: pip
                  cache-dependency-path: app/requirements*.txt
              - run: python -m pip install -r app/requirements-dev.txt
              - run: python -m pytest -q
              - run: docker build --pull --tag __SERVICE_NAME__:ci .
              - run: kubectl kustomize k8s/base >/tmp/__SERVICE_NAME__.yaml
    """,
    "k8s/base/kustomization.yaml": """
        apiVersion: kustomize.config.k8s.io/v1beta1
        kind: Kustomization
        resources:
          - analysis-template.yaml
          - rollout.yaml
          - service-stable.yaml
          - service-canary.yaml
          - service-monitor.yaml
    """,
    "k8s/base/rollout.yaml": """
        apiVersion: argoproj.io/v1alpha1
        kind: Rollout
        metadata:
          name: __SERVICE_NAME__
          labels:
            app.kubernetes.io/name: __SERVICE_NAME__
        spec:
          replicas: 2
          selector:
            matchLabels:
              app: __SERVICE_NAME__
          strategy:
            canary:
              canaryService: __SERVICE_NAME__-canary
              stableService: __SERVICE_NAME__-stable
              steps:
                - setWeight: 10
                - pause: {duration: 60s}
                - analysis:
                    templates:
                      - templateName: __SERVICE_NAME__-canary
                    args:
                      - name: service-name
                        value: __SERVICE_NAME__-canary
                - setWeight: 50
                - pause: {duration: 120s}
                - analysis:
                    templates:
                      - templateName: __SERVICE_NAME__-canary
                    args:
                      - name: service-name
                        value: __SERVICE_NAME__-canary
          template:
            metadata:
              labels:
                app: __SERVICE_NAME__
            spec:
              automountServiceAccountToken: false
              securityContext:
                runAsNonRoot: true
                seccompProfile:
                  type: RuntimeDefault
              containers:
                - name: __SERVICE_NAME__
                  image: ghcr.io/__OWNER__/__SERVICE_NAME__:replace-with-commit-sha
                  imagePullPolicy: IfNotPresent
                  securityContext:
                    allowPrivilegeEscalation: false
                    readOnlyRootFilesystem: true
                    capabilities:
                      drop: [ALL]
                  ports:
                    - name: http
                      containerPort: __PORT__
                  readinessProbe:
                    httpGet:
                      path: /healthz
                      port: http
                  livenessProbe:
                    httpGet:
                      path: /healthz
                      port: http
                  resources:
                    requests:
                      cpu: 100m
                      memory: 128Mi
                    limits:
                      cpu: 500m
                      memory: 512Mi
    """,
    "k8s/base/service-monitor.yaml": """
        apiVersion: monitoring.coreos.com/v1
        kind: ServiceMonitor
        metadata:
          name: __SERVICE_NAME__
          labels:
            monitoring: __SERVICE_NAME__
        spec:
          selector:
            matchLabels:
              app.kubernetes.io/name: __SERVICE_NAME__
          endpoints:
            - port: http
              path: /metrics
              interval: 15s
              scrapeTimeout: 5s
    """,
    "k8s/base/service-stable.yaml": """
        apiVersion: v1
        kind: Service
        metadata:
          name: __SERVICE_NAME__-stable
          labels:
            app.kubernetes.io/name: __SERVICE_NAME__
            app.kubernetes.io/component: stable
        spec:
          ports:
            - name: http
              port: 80
              targetPort: http
          selector:
            app: __SERVICE_NAME__
    """,
    "k8s/base/service-canary.yaml": """
        apiVersion: v1
        kind: Service
        metadata:
          name: __SERVICE_NAME__-canary
          labels:
            app.kubernetes.io/name: __SERVICE_NAME__
            app.kubernetes.io/component: canary
        spec:
          ports:
            - name: http
              port: 80
              targetPort: http
          selector:
            app: __SERVICE_NAME__
    """,
    "k8s/base/analysis-template.yaml": """
        apiVersion: argoproj.io/v1alpha1
        kind: AnalysisTemplate
        metadata:
          name: __SERVICE_NAME__-canary
        spec:
          metrics:
            - name: error-rate
              interval: 30s
              count: 3
              failureLimit: 1
              successCondition: result[0] < 0.01
              provider:
                prometheus:
                  address: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
                  query: >-
                    (sum(rate(http_requests_total{service="{{args.service-name}}",status=~"5.."}[5m])) or vector(0))
                    /
                    clamp_min((sum(rate(http_requests_total{service="{{args.service-name}}"}[5m])) or vector(0)), 1)
            - name: p95-latency
              interval: 30s
              count: 3
              failureLimit: 1
              successCondition: result[0] < 0.5
              provider:
                prometheus:
                  address: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
                  query: >-
                    histogram_quantile(
                      0.95,
                      sum by (le) (rate(http_request_duration_seconds_bucket{service="{{args.service-name}}"}[5m]))
                    ) or vector(0)
    """,
}


def _render(template: str, values: dict[str, str]) -> str:
    rendered = dedent(template).lstrip()
    for key, value in values.items():
        rendered = rendered.replace(f"__{key}__", value)
    return rendered.rstrip() + "\n"


def validate_service_name(name: str) -> str:
    if not SERVICE_NAME_PATTERN.fullmatch(name):
        raise ValueError("service name must match ^[a-z][a-z0-9-]{2,30}$")
    return name


def scaffold_service(*, name: str, owner: str, port: int, output_dir: Path, force: bool = False) -> list[Path]:
    validate_service_name(name)
    if not owner or "/" in owner or ".." in owner:
        raise ValueError("owner must be a non-empty platform team identifier")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")

    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise FileExistsError(f"refusing to overwrite non-empty directory: {output_dir}")

    values = {"SERVICE_NAME": name, "OWNER": owner, "PORT": str(port)}
    written: list[Path] = []
    for relative_path, template in TEMPLATES.items():
        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_render(template, values), encoding="utf-8")
        written.append(destination)
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="lowercase service name")
    parser.add_argument("--owner", required=True, help="owning platform team")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true", help="allow writing into a non-empty directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        written = scaffold_service(
            name=args.name,
            owner=args.owner,
            port=args.port,
            output_dir=args.output_dir,
            force=args.force,
        )
    except (FileExistsError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(f"generated {len(written)} files under {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
