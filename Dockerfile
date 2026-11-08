FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && useradd --system --no-create-home --uid 10001 ispq \
    && install -d -o ispq /data

# Measurements are written to /data (a volume); the configuration is read from /data/ispq.toml if it exists.
# The collector needs no privileges (it opens ordinary TCP/UDP connections): run it with a read-only root filesystem
# and no capabilities.
USER ispq
VOLUME ["/data"]
ENTRYPOINT ["ispq"]
CMD ["run", "--db", "/data/ispq.db"]
