#!/bin/bash
#SBATCH --job-name=bbqwen
#SBATCH --partition=gpu_h200_8
#SBATCH --gres=gpu:1
#SBATCH --time=2-00:00:00
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --output=/scratch/pabitra/bidbrain/%x-%j.out
# Serves Qwen3-32B (bf16, one H200) with tool calling for BidBrain's benchmark.
#
# Same model family the live demo uses on Groq / Cerebras, so benchmark numbers
# describe the deployed agent. Toolchain setup is reused verbatim from the
# Gemma-4 competition's serve_clean.sbatch (pinned CUDA 13.0 trio, venv's own
# ninja/nvcc first on PATH) -- the root-cause fix documented there.
set -u
S=/scratch/pabitra/gemma4comp
V=$S/venv
N=$V/lib/python3.11/site-packages/nvidia/cu13
H=$S/cuda_home
B=/scratch/pabitra/bidbrain
M=/scratch/pabitra/models/Qwen3-32B
PORT=${PORT:-8011}

export HF_HOME=/scratch/pabitra/hf_home TMPDIR=$B/tmp OUTLINES_CACHE_DIR=$S/outlines
export VLLM_CACHE_ROOT=$S/vllm_cache TRITON_CACHE_DIR=$S/triton_cache
export FLASHINFER_WORKSPACE_BASE=$S/flashinfer
mkdir -p "$TMPDIR"
bash "$S/make_cuda_home.sh"
export PATH="$V/bin:$N/bin:$PATH"
export CUDA_HOME="$H"
export LIBRARY_PATH="$H/lib64${LIBRARY_PATH:+:$LIBRARY_PATH}"
export LD_LIBRARY_PATH="$H/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "===== node: $(hostname)   started: $(date)   port: $PORT ====="
nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv,noheader

for i in $(seq 1 60); do
  used=$(nvidia-smi -i "${CUDA_VISIBLE_DEVICES%%,*}" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
         || nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
  [ "$used" -lt 2000 ] && break
  echo "waiting for the GPU: ${used} MiB still in use (${i}/60)"; sleep 10
done

READY=$B/serve_ready_${SLURM_JOB_ID}.txt
rm -f "$READY"
echo "$(hostname) $PORT" > "$B/serve_endpoint.txt"

# Qwen3: hermes-style tool calls; thinking is switched off per request by the
# client (chat_template_kwargs.enable_thinking=false) -- routing turns do not
# need a hidden chain of thought. 40k context covers the longest agent traces.
"$V/bin/vllm" serve "$M" --served-model-name Qwen/Qwen3-32B \
    --host 0.0.0.0 --port $PORT \
    --max-model-len 40960 --tensor-parallel-size 1 \
    --gpu-memory-utilization 0.90 --max-num-seqs 64 \
    --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3 &
PID=$!

for i in $(seq 1 150); do
  kill -0 $PID 2>/dev/null || { echo "!!! server exited during startup"; wait $PID; exit 1; }
  curl -sf "http://127.0.0.1:$PORT/v1/models" -o /dev/null 2>/dev/null && break
  sleep 10
done
echo "--- /v1/models answers; gating on a real TOOL CALL ---"
BODY=$(curl -s --max-time 300 "http://127.0.0.1:$PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-32B",
       "messages":[{"role":"user","content":"Find the completed works for the client National Expressway Development Authority."}],
       "tools":[{"type":"function","function":{"name":"find_portfolio","description":"Find completed works",
         "parameters":{"type":"object","properties":{"client":{"type":"string"}},"required":["client"]}}}],
       "tool_choice":"auto","max_tokens":256,"temperature":0,
       "chat_template_kwargs":{"enable_thinking":false}}')
echo "$BODY" | head -c 900; echo
if ! echo "$BODY" | grep -q '"tool_calls"'; then
  echo "!!! tool-calling generation FAILED -- not a usable server"
  kill $PID 2>/dev/null; wait $PID 2>/dev/null; exit 1
fi
echo "$(hostname) $PORT $(date)" > "$READY"
echo "===== ACCEPTED: tool-calling generation works. serving. ====="
wait $PID
