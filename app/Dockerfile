FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip==26.2.1 \
    && python -m pip install --requirement requirements.txt

COPY --chown=10001:10001 app.py draw_winner.py migrate.py schema.py ./
COPY --chown=10001:10001 templates/ ./templates/
COPY --chown=10001:10001 static/ ./static/

USER 10001:10001
EXPOSE 8080

# Keep one Prometheus registry per pod. Scale horizontally with Rollout/HPA
# rather than using Gunicorn workers that would expose split counter state.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--worker-tmp-dir", "/dev/shm", "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
