#!/usr/bin/env bash
# One S11 policy generation: a fresh workspace, one claude --print call.
# Usage: bash arms/s11gen/generate.sh <pair> <use_case>
#   pair 0 is a pilot into ~/s11-gen; it is never imported.
#   use_case is expenses or payments. Run a pair's two use cases separately.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <pair> <expenses|payments>" >&2
    exit 1
fi
PAIR=$1
UC=$2
[[ "$PAIR" =~ ^[0-9]+$ ]] || { echo "pair must be a non-negative integer" >&2; exit 1; }
[[ "$UC" == "expenses" || "$UC" == "payments" ]] || { echo "use_case must be expenses or payments" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODEL="global.anthropic.claude-sonnet-4-6"
TOOLS="Read(./**),Write(./policy.cedar)"
FROZEN_TAG="s11-pipeline-frozen"
ROOT=~/s11-gen
WS="$ROOT/$UC-$PAIR"
LOG="$ROOT/logs/$UC-$PAIR.jsonl"
META="$ROOT/logs/$UC-$PAIR.meta.json"

if [[ "$PAIR" == "0" ]]; then
    echo "NOTE: pair 0 is a pilot run and will not be imported." >&2
else
    # a counted generation runs only from the frozen pipeline (read-only git)
    git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$FROZEN_TAG" > /dev/null \
        || { echo "tag $FROZEN_TAG does not exist: freeze the pipeline first" >&2; exit 1; }
    git -C "$REPO_ROOT" diff --quiet "$FROZEN_TAG" -- \
        arms/s11gen/generate.sh arms/s11gen/make_workspace.py arms/s11gen/inputs arms/s11gen/requirements \
        || { echo "the pipeline differs from tag $FROZEN_TAG" >&2; exit 1; }
fi

[[ ! -e "$WS" ]] || { echo "$WS exists: every generation gets a fresh workspace" >&2; exit 1; }
mkdir -p "$ROOT/logs"
[[ ! -e "$LOG" ]] || { echo "$LOG exists" >&2; exit 1; }

START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CLAUDE_VERSION=$(claude --version 2>&1 | head -1)
(cd "$REPO_ROOT" && uv run python -m arms.s11gen.make_workspace "$WS" "$UC")

cd "$WS"
set +e
cat prompt.md | claude --print --output-format stream-json --verbose \
    --model "$MODEL" --allowedTools "$TOOLS" > "$LOG"
CODE=$?
set -e
END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

python3 - "$META" "$PAIR" "$UC" "$MODEL" "$TOOLS" "$CLAUDE_VERSION" "$START_TIME" "$END_TIME" "$CODE" <<'PYEOF'
import json, pathlib, sys
meta, pair, uc, model, tools, version, start, end, code = sys.argv[1:]
pathlib.Path(meta).write_text(json.dumps({
    "pair": int(pair), "use_case": uc, "model": model,
    "settings": {"command": "claude --print", "output_format": "stream-json",
                 "allowed_tools": tools, "claude_code_version": version},
    "start_time": start, "end_time": end, "exit_code": int(code),
}, indent=2) + "\n")
PYEOF

echo "Generation $UC pair $PAIR complete (exit $CODE): $WS"
exit "$CODE"
