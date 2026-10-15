#!/bin/bash
# Waits for the Sharanga vLLM job, tunnels it to localhost:8011, then runs the whole benchmark matrix.
#
#   setsid nohup scripts/bench_when_ready.sh > /dev/null 2>&1 &      # progress: data/eval/bench_runner.log
#
# Safe to re-run after any interruption: eval.run skips questions it already answered, the GraphRAG-SDK
# ingest is skipped once its graph exists, and every completion is cached. Stop it with
#   pkill -f bench_when_ready.sh; pkill -f "ssh -N -L 8011"
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOG=$ROOT/data/eval/bench_runner.log
JOBS=${JOBS:-"397261 397259"}          # our pending vLLM jobs (H100 fp8, H200 bf16); first one up wins
REMOTE=/scratch/pabitra/bidbrain
PY=$ROOT/backend/.venv/bin/python
SDK_PY=$ROOT/backend/.venv-sdk/bin/python
SETS=${SETS:-"S21 Q333 ABS TND"}
SYSTEMS=${SYSTEMS:-"bidbrain text2cypher vector_rag"}
WORKERS=${WORKERS:-16}
mkdir -p "$ROOT/data/eval"
cd "$ROOT/backend" || exit 1
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
remote() { ssh -o BatchMode=yes -o ConnectTimeout=20 sharanga "$@" 2>/dev/null; }

# ------------------------------------------------------------------ 1. wait for a server that passed its tool-call gate
log "waiting for one of jobs: $JOBS"
while :; do
  ready=$(remote "for j in $JOBS; do
                    st=\$(squeue -h -j \$j -o %T 2>/dev/null)
                    [ \"\$st\" = RUNNING ] && [ -f $REMOTE/serve_ready_\$j.txt ] && { echo \$j \$(cat $REMOTE/serve_ready_\$j.txt); break; }
                  done")
  if [ -n "$ready" ]; then break; fi
  sleep 300
done
read -r JOB NODE PORT _ <<< "$ready"
log "server ready: job $JOB on $NODE:$PORT"
for j in $JOBS; do
  [ "$j" != "$JOB" ] && remote "scancel $j" && log "cancelled the other pending job $j"
done

# ------------------------------------------------------------------ 2. keep a tunnel up for the rest of the run
(
  while :; do
    ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -L 8011:"$NODE":"$PORT" sharanga 2>/dev/null
    sleep 5
  done
) &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null; pkill -f "ssh -N .* -L 8011:$NODE" 2>/dev/null' EXIT
for i in $(seq 1 60); do
  curl -sf http://localhost:8011/v1/models -o /dev/null && break
  sleep 5
done
curl -sf http://localhost:8011/v1/models -o /dev/null || { log "tunnel never came up"; exit 1; }
log "tunnel up: localhost:8011 -> $NODE:$PORT"

export LLM_PROVIDERS=vllm VLLM_BASE_URL=http://localhost:8011/v1 LLM_MAX_WAIT=300

# ------------------------------------------------------------------ 3. smoke test before spending hours
"$PY" -m eval.run --system bidbrain --set S21 --limit 3 --workers 3 >> "$LOG" 2>&1
ok=$("$PY" -c "
import json; rs=[json.loads(l) for l in open('$ROOT/data/eval/results/bidbrain__S21.jsonl')]
print(sum(1 for r in rs if not r.get('error')))" 2>/dev/null || echo 0)
if [ "${ok:-0}" -lt 2 ]; then
  log "smoke test failed ($ok of 3 answered without error); see the log above. Stopping."
  exit 1
fi
log "smoke test passed ($ok/3)"

# ------------------------------------------------------------------ 4. the matrix (S21 first everywhere = early sanity)
for set in $SETS; do
  for sys in $SYSTEMS; do
    log "run $sys on $set"
    "$PY" -m eval.run --system "$sys" --set "$set" --workers "$WORKERS" >> "$LOG" 2>&1 || log "  $sys/$set exited non-zero"
    "$PY" -m eval.run --summarize > /dev/null 2>&1
  done
done

# ------------------------------------------------------------------ 5. GraphRAG-SDK baseline (needs its own venv)
if [ -x "$SDK_PY" ]; then
  if ! docker exec bidbrain-falkordb redis-cli GRAPH.LIST 2>/dev/null | grep -qx grsdk_bidbrain; then
    log "GraphRAG-SDK ingest (auto schema over the corpus)"
    "$SDK_PY" -m eval.baselines.graphrag_sdk ingest >> "$LOG" 2>&1 || log "  ingest exited non-zero"
  fi
  for set in $SETS; do
    log "run graphrag_sdk on $set"
    "$SDK_PY" -m eval.baselines.graphrag_sdk run --set "$set" >> "$LOG" 2>&1 || log "  graphrag_sdk/$set exited non-zero"
    "$PY" -m eval.run --summarize > /dev/null 2>&1
  done
else
  log "no $SDK_PY; GraphRAG-SDK baseline skipped"
fi

# ------------------------------------------------------------------ 6. LLM-read gradings (after the eval, so the eval
# graph is the deterministic one); the loader picks them up on the next build
if [ ! -f "$ROOT/data/llm_gradings.json" ]; then
  log "LLM grading extraction"
  "$PY" -m bidbrain.ingest.llm_grading >> "$LOG" 2>&1 || log "  llm_grading exited non-zero"
fi

"$PY" -m eval.run --summarize >> "$LOG" 2>&1
log "benchmark finished; summary in data/eval/qa_summary.json"
