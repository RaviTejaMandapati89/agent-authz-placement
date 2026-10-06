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
# Permission rules (Claude Code 2.1.286 docs): "Edit rules apply to all built-in tools that edit
# files" and "If you write a path rule for Write ... Claude Code accepts the rule but never consults
# it", so the one writable file is an Edit rule. "path or ./path: relative to current directory".
ALLOW="Read(./**),Edit(./policy.cedar)"
# --tools restricts the built-in tool set: the model gets Read and Write and nothing else.
TOOL_SET="Read,Write"
# Provider route: Bedrock, as approach 3's generations ran. Region is not a secret;
# credentials come from the owner's own AWS configuration and are never set here.
AWS_REGION_FOR_CALL="${S11_AWS_REGION:-us-west-2}"
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
# the Bedrock route is set inside this subshell, so it applies to this one call only
(
    export CLAUDE_CODE_USE_BEDROCK=1
    export AWS_REGION="$AWS_REGION_FOR_CALL"
    cat prompt.md | claude --print --output-format stream-json --verbose \
        --model "$MODEL" --tools "$TOOL_SET" --allowedTools "$ALLOW" \
        --permission-prompts none
) > "$LOG"
CODE=$?
set -e
END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

set +e
python3 - "$META" "$PAIR" "$UC" "$MODEL" "$ALLOW" "$CLAUDE_VERSION" "$START_TIME" "$END_TIME" "$CODE" \
    "$AWS_REGION_FOR_CALL" "$LOG" "$WS" "$TOOL_SET" <<'PYEOF'
import json, pathlib, sys
meta, pair, uc, model, tools, version, start, end, code, region, log, ws, tool_set = sys.argv[1:]
result = None
models = set()
offered = None
denials = []
canon = lambda name: name.split("anthropic.")[-1]   # global.anthropic.X and X are one model
for line in pathlib.Path(log).read_text().splitlines():
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("type") == "system" and event.get("subtype") == "init":
        offered = event.get("tools")
        if event.get("model"):
            models.add(event["model"])
    if event.get("type") == "assistant" and (event.get("message") or {}).get("model"):
        models.add(event["message"]["model"])
    if event.get("type") == "result":
        result = event
        models.update((event.get("modelUsage") or {}).keys())
        denials = [{"tool_name": d.get("tool_name"),
                    "path": (d.get("tool_input") or {}).get("file_path")}
                   for d in event.get("permission_denials") or []]
is_error = result is None or bool(result.get("is_error"))
policy = pathlib.Path(ws) / "policy.cedar"
wrong_models = sorted(m for m in models if canon(m) != canon(model))
extra_tools = sorted(set(offered or []) - set(tool_set.split(",")))
problems = []
if result is None:
    problems.append(f"the stream has no result line ({log})")
elif is_error:
    problems.append(f"the stream's result is an error: {str(result.get('result'))[:500]} ({log})")
if not policy.is_file() or not policy.read_text().strip():
    problems.append(f"policy.cedar is missing or empty ({policy})")
if denials:
    problems.append(f"permission denials: {denials}")
if wrong_models:
    problems.append(f"replies from a model other than the pinned {model}: {wrong_models}")
if extra_tools:
    problems.append(f"tools offered beyond {tool_set}: {extra_tools}")
pathlib.Path(meta).write_text(json.dumps({
    "pair": int(pair), "use_case": uc, "model": model,
    "provider": "bedrock", "aws_region": region,
    "settings": {"command": "claude --print", "output_format": "stream-json",
                 "tools": tool_set, "allowed_tools": tools, "permission_prompts": "none",
                 "claude_code_version": version},
    "start_time": start, "end_time": end, "exit_code": int(code),
    "result_is_error": is_error,
    "models_answered": sorted(models), "permission_denials": denials,
    "tools_offered": offered, "failures": problems,
}, indent=2) + "\n")
for p in problems:
    print(f"FAILED: {p}", file=sys.stderr)
sys.exit(3 if problems else 0)
PYEOF
RESULT_CODE=$?
set -e

if [[ "$RESULT_CODE" -ne 0 ]]; then
    echo "Generation $UC pair $PAIR FAILED: $WS" >&2
    exit "$RESULT_CODE"
fi
echo "Generation $UC pair $PAIR complete (exit $CODE): $WS"
exit "$CODE"
