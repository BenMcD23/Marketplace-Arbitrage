# Playwright base image ships Chromium + all OS deps needed for the scrapers.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENV=prod

WORKDIR /app

# Install uv for fast, reproducible dependency installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first (better layer caching).
COPY pyproject.toml README.md ./
COPY arb ./arb
COPY sources ./sources
COPY oracle ./oracle
COPY engine ./engine
COPY alerts ./alerts
COPY api ./api
COPY main.py ./

RUN uv pip install --system -e .

# The db lives on a mounted volume.
VOLUME ["/app/data"]

# Default: run continuously on the configured interval. Override the command to
# run once (`arb run`) or print stats (`arb stats`).
ENTRYPOINT ["arb"]
CMD ["schedule", "--interval", "15"]
