# BidBrain — one container: FalkorDB + FastAPI API + MCP + the static Next.js UI.
# Built for Hugging Face Spaces (Docker SDK, port 7860, runs as uid 1000) and
# equally runnable anywhere:  docker build -t bidbrain . && docker run -p 7860:7860 --env-file .env bidbrain
#
# The base graph is rebuilt from facts at every boot (~2 s) instead of shipping an RDB
# snapshot: deterministic, and immune to on-disk format changes. Runtime state (company
# memory, session graphs) persists in $BIDBRAIN_STATE_DIR (default /app/state; see infra/start.sh).

# ---------- 1. the UI (static export) ----------
FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ARG NEXT_PUBLIC_API_BASE=""
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
RUN npm run build

# ---------- 2. runtime: pinned FalkorDB image + Python ----------
FROM falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

ENV HOME=/home/user UV_PYTHON_INSTALL_DIR=/opt/python UV_CACHE_DIR=/tmp/uv \
    FASTEMBED_CACHE_PATH=/opt/models HF_HOME=/opt/models/hf PYTHONUNBUFFERED=1
RUN (useradd -m -u 1000 user || true) && mkdir -p /home/user /app /opt/models && chown 1000:1000 /home/user /app /opt/models
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN uv venv --python 3.12 /opt/venv && uv pip install --python /opt/venv/bin/python -r backend/requirements.txt
ENV PATH=/opt/venv/bin:$PATH

USER 1000
# embedding model baked into the image (no download at boot); fetched as the runtime user so its cache is writable
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/models')"

COPY --chown=1000:1000 backend/ backend/
COPY --chown=1000:1000 data/ data/
COPY --chown=1000:1000 infra/start.sh /app/start.sh
COPY --chown=1000:1000 --from=ui /ui/out frontend/out

# the MIT-licensed synthetic corpus, pinned (its documents are byte-identical to the copy facts.json was built from)
RUN rm -rf backend/vendor_bidintel/dataset && mkdir -p backend/vendor_bidintel/dataset \
    && cd backend/vendor_bidintel/dataset && git init -q \
    && git remote add origin https://github.com/satvikGIKA/BITS-Hackathon-Dataset.git \
    && git fetch -q --depth 1 origin 707c80fc7a57dad0c0f58cb4b305f4ecfba27c8a \
    && git checkout -q FETCH_HEAD && rm -rf .git
RUN mkdir -p /app/data/uploads /app/data/conflicts/pdf /app/state/falkordb
ENV BIDBRAIN_STATE_DIR=/app/state
EXPOSE 7860
ENTRYPOINT []
CMD ["/app/start.sh"]
