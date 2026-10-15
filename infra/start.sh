#!/bin/bash
# Container entrypoint: FalkorDB (localhost only) -> build graph -> render demo PDFs -> API on :7860
set -e
cd /app/backend

redis-server --loadmodule /var/lib/falkordb/bin/falkordb.so \
    TIMEOUT_DEFAULT 15000 TIMEOUT_MAX 180000 MAX_QUEUED_QUERIES 200 THREAD_COUNT 4 \
  --bind 127.0.0.1 --port 6379 --dir /tmp --save "" --appendonly no --daemonize yes \
  --maxmemory 6gb --logfile /tmp/falkordb.log

for i in $(seq 1 50); do redis-cli -p 6379 ping >/dev/null 2>&1 && break; sleep 0.2; done
echo "FalkorDB up: $(redis-cli -p 6379 MODULE LIST | tr '\n' ' ' | head -c 120)"

python -m bidbrain.ingest.load_graph
python -m bidbrain.conflicts.demo || echo "demo PDFs skipped"

exec uvicorn bidbrain.api.main:app --host 0.0.0.0 --port "${PORT:-7860}" --workers 1 --proxy-headers
