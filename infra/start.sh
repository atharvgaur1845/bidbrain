#!/bin/bash
# Container entrypoint: FalkorDB (localhost only) -> build graph -> render demo PDFs -> API on :7860
#
# State lives in $BIDBRAIN_STATE_DIR (default /app/state; mount a volume there, or on Hugging Face
# Spaces with persistent storage set BIDBRAIN_STATE_DIR=/data):
#   falkordb/          FalkorDB RDB snapshots: company memory (bidbrain_org) and session graphs. Saved at
#                      most every FALKOR_SAVE (default: 300 s if anything changed) and on a clean stop.
#                      Not AOF: every new session is a GRAPH.COPY, which would grow the AOF by a whole graph
#                      and force a fork for a rewrite every few sessions (a copy racing a fork fails).
#   org_memory.json    JSON mirror of company memory, rewritten after every promote / retire / contest
#                      and at shutdown; imported at boot only if bidbrain_org did not survive
# bidbrain_base is rebuilt from facts at every boot (deterministic); nothing else is wiped.
set -e
cd /app/backend

export BIDBRAIN_STATE_DIR="${BIDBRAIN_STATE_DIR:-/app/state}"
# Rate limits key on the client IP. The API trusts X-Forwarded-For only when FORWARDED_HOPS says how many
# proxies append to it (default 0: the socket peer). Hugging Face Spaces put exactly one proxy in front and set
# SPACE_ID in the container, so there the default becomes 1. Set FORWARDED_HOPS explicitly to override.
# Shared company memory needs signed-in principals: set BIDBRAIN_PRINCIPALS and BIDBRAIN_AUTH_KEY as Space secrets
# (name:role:sha256(secret),...). Without them promote / publish / approve / retire are disabled (fail closed).
if [ -z "${BIDBRAIN_PRINCIPALS:-}" ]; then echo "auth: no BIDBRAIN_PRINCIPALS; shared publication disabled"; fi
if [ -z "${FORWARDED_HOPS:-}" ] && [ -n "${SPACE_ID:-}" ]; then export FORWARDED_HOPS=1; fi
# FALKOR_URL set (e.g. FalkorDB Cloud behind a Render web service): use that server, start nothing locally.
if [ -n "${FALKOR_URL:-}" ]; then
  echo "FalkorDB: external server from FALKOR_URL (no local FalkorDB started)"
else
  FALKOR_DIR="$BIDBRAIN_STATE_DIR/falkordb"
  mkdir -p "$FALKOR_DIR"
  PIDFILE="$FALKOR_DIR/falkordb.pid"

  start_falkor() {
    redis-server --loadmodule /var/lib/falkordb/bin/falkordb.so \
        TIMEOUT_DEFAULT 15000 TIMEOUT_MAX 180000 MAX_QUEUED_QUERIES 200 THREAD_COUNT 4 \
        RESULTSET_SIZE 10000 QUERY_MEM_CAPACITY 1073741824 \
      --bind 127.0.0.1 --port 6379 --dir "$FALKOR_DIR" \
      --appendonly no --save "${FALKOR_SAVE:-300 1}" \
      --daemonize yes --pidfile "$PIDFILE" \
      --maxmemory 6gb --logfile "$FALKOR_DIR/falkordb.log"
    # loading a large AOF can take a while: wait for PONG (not just any reply, e.g. LOADING)
    for i in $(seq 1 600); do
      [ "$(redis-cli -p 6379 ping 2>/dev/null)" = "PONG" ] && return 0
      if [ "$i" -gt 15 ] && ! kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then return 1; fi
      sleep 0.2
    done
    return 1
  }

  if ! start_falkor; then
    # unreadable data dir (e.g. an on-disk format change after an image upgrade): keep it for
    # inspection, start empty, and let the JSON mirror restore company memory
    echo "FalkorDB could not load $FALKOR_DIR; moving it aside and starting empty"
    redis-cli -p 6379 shutdown nosave >/dev/null 2>&1 || true
    mv "$FALKOR_DIR" "$FALKOR_DIR.unreadable-$(date +%s)"
    mkdir -p "$FALKOR_DIR"
    start_falkor
  fi
  echo "FalkorDB up: $(redis-cli -p 6379 MODULE LIST | tr '\n' ' ' | head -c 120)"
  echo "FalkorDB graphs kept from the last run: $(redis-cli -p 6379 GRAPH.LIST | grep -v '^bidbrain_base$' | grep -c . || true)"

fi

python -m bidbrain.ingest.load_graph          # rebuilds bidbrain_base only
python -m bidbrain.conflicts.demo || echo "demo PDFs skipped"

# uvicorn's lifespan imports org_memory.json if bidbrain_org is missing and exports it on shutdown.
# On SIGTERM (docker stop, Space restart): stop uvicorn first, then FalkorDB with a final snapshot.
stop() {
  trap - TERM INT
  set +e
  [ -n "$API_PID" ] && kill -TERM "$API_PID" 2>/dev/null && wait "$API_PID" 2>/dev/null
  redis-cli -p 6379 shutdown save >/dev/null 2>&1 || true
  exit 0
}
trap stop TERM INT
uvicorn bidbrain.api.main:app --host 0.0.0.0 --port "${PORT:-7860}" --workers 1 --proxy-headers &
API_PID=$!
status=0
wait "$API_PID" || status=$?
redis-cli -p 6379 shutdown save >/dev/null 2>&1 || true
exit $status
