# Single-stage — no Node anywhere, even at build time. The Tailwind CLI is a
# downloaded standalone binary (see scripts/build-css.sh), not an npm package,
# so compiling the dashboard's CSS is just another RUN step. See docs/DESIGN.md
# §9.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN ./scripts/build-css.sh \
    && pip install --no-cache-dir . \
    && rm -rf .tailwind-bin

ENV HOST=0.0.0.0 \
    PORT=8000 \
    SPENDGAUGEAI_DB_PATH=/data/spendgaugeai.db

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s CMD curl -f http://localhost:8000/health || exit 1

CMD ["spendgaugeai", "serve"]
