FROM python:3.13-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY requirements.txt ./
RUN python -m pip install --upgrade pip==26.2.1 \
    && python -m pip install --no-cache-dir --target=/opt/python --requirement requirements.txt

FROM gcr.io/distroless/python3-debian13:nonroot@sha256:8ee214843129f43e2ebf5e0ca9f2e4e6d8292143d1b8a6787f169b5898578884

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/python:/app

WORKDIR /app

COPY --from=builder /opt/python /opt/python

COPY --chown=65532:65532 app.py db.py catalog_cache.py draw_winner.py migrate.py schema.py ./
COPY --chown=65532:65532 templates/ ./templates/
COPY --chown=65532:65532 static/ ./static/

USER 65532:65532
EXPOSE 8080

# Keep one Prometheus registry per pod. Scale horizontally with Rollout/HPA
# rather than using Gunicorn workers that would expose split counter state.
CMD ["-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--worker-tmp-dir", "/dev/shm", "--timeout", "30", "--keep-alive", "75", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
