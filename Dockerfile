FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    AIRGUS_HOST=0.0.0.0 AIRGUS_PORT=8080 AIRGUS_LOGS=/logs AIRGUS_DATA=/data
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY airgus ./airgus
RUN pip install --no-cache-dir . \
    && useradd --system --uid 1000 airgus \
    && mkdir -p /data /logs && chown airgus /data

USER airgus
VOLUME ["/logs", "/data"]
EXPOSE 8080
ENTRYPOINT ["airgus"]
