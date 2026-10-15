#!/bin/bash
# Push the current commit to a Hugging Face Docker Space.
#
#   hf auth login                                   # once, with a WRITE token (done by you)
#   infra/deploy_space.sh <hf-username> [space-name]
#
# Exports HEAD (git archive, so only committed files), swaps in the Space README
# (YAML front-matter), and force-pushes to the Space repo. Secrets are NOT pushed:
# set GROQ_API_KEY / CEREBRAS_API_KEY in the Space's Settings -> Variables and secrets.
set -euo pipefail
USER_NS="${1:?usage: deploy_space.sh <hf-username> [space-name]}"
SPACE="${2:-bidbrain}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HF="${HF_CLI:-$ROOT/backend/.venv/bin/hf}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"$HF" repo create "$USER_NS/$SPACE" --repo-type space --space-sdk docker --exist-ok >/dev/null 2>&1 || "$HF" repo create "$USER_NS/$SPACE" --repo-type space --space-sdk docker >/dev/null 2>&1 || true

git -C "$ROOT" archive HEAD | tar -x -C "$TMP"
cp "$ROOT/infra/space/README.md" "$TMP/README.md"
# include the recorded LLM cache if present, so canned demo questions answer instantly
[ -f "$ROOT/data/llm_cache.sqlite" ] && cp "$ROOT/data/llm_cache.sqlite" "$TMP/data/llm_cache.sqlite"

"$HF" upload "$USER_NS/$SPACE" "$TMP" . --repo-type space \
  --commit-message "Deploy $(git -C "$ROOT" rev-parse --short HEAD)" \
  --delete "*" --exclude ".git/*"
echo "Pushed. Build logs: https://huggingface.co/spaces/$USER_NS/$SPACE?logs=build"
echo "App:              https://$(echo "$USER_NS-$SPACE" | tr '[:upper:]_' '[:lower:]-').hf.space"
