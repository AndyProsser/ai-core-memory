# Memory hub image. Build context: this directory (hub/).
FROM python:3.12-slim AS builder
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 hub && mkdir /data && chown hub:hub /data
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels ai-core-memory-hub && rm -rf /wheels
USER hub
WORKDIR /home/hub
ENV MEMORY_HUB_DB_PATH=/data/hub.sqlite3 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["acm", "serve", "--host", "0.0.0.0", "--port", "8000"]
