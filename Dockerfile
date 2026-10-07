FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY subset.py subset_ui.py sampler.py repo_discovery.py LH2-DataLabs.svg ./
RUN mkdir -p /app/outputs /data/repos

ENV SUBSET_DOCKER=1 \
    DEFAULT_LOCAL_REPOS_DIR=/data/repos \
    DEFAULT_TOKENS_FILE=/app/secrets/tokens \
    PYTHONUNBUFFERED=1 \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0=*

EXPOSE 8768
CMD ["python", "subset.py", "--ui", "--ui-host", "0.0.0.0", "--ui-port", "8768"]
