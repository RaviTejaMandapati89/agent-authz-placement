#!/usr/bin/env bash
# Run the approach-3 generation pipeline for generation N.
# Usage: bash arms/approach3/generate.sh <N>
#   N = 0: pilot run into ~/checkpoint-gen/run-0; never imported.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <N>" >&2
    exit 1
fi

GEN_N=$1

if ! [[ "$GEN_N" =~ ^[0-9]+$ ]]; then
    echo "N must be a non-negative integer" >&2
    exit 1
fi

if [[ "$GEN_N" == "0" ]]; then
    echo "NOTE: run-0 is a pilot run and will not be imported." >&2
fi

KIT=~/checkpoint-gen/run-$GEN_N
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODEL="global.anthropic.claude-sonnet-4-6"

SPECKIT_VERSION="unknown"
CLAUDE_VERSION="unknown"
START_TIME=""
END_TIME=""
CURRENT_STEP="start"

_write_metadata() {
    local exit_code=$1
    local fs=""
    [[ $exit_code -ne 0 ]] && fs="$CURRENT_STEP"
    python3 - "$GEN_N" "$SPECKIT_VERSION" "$CLAUDE_VERSION" \
        "${START_TIME:-}" "${END_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" \
        "$fs" "$exit_code" <<'PYEOF'
import json, pathlib, sys

gen_n               = int(sys.argv[1])
speckit_version     = sys.argv[2]
claude_code_version = sys.argv[3]
start_time          = sys.argv[4]
end_time            = sys.argv[5]
failed_step         = sys.argv[6]
failed_exit         = int(sys.argv[7])

steps = {}
model = "unknown"

for name in ["constitution", "specify", "plan", "tasks", "implement"]:
    p = pathlib.Path(f"logs/step-{name}.jsonl")
    if not p.exists():
        continue
    events = []
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    except Exception:
        continue
    result_event = next((e for e in events if e.get("type") == "result"), None)
    if result_event is None:
        continue
    usage = result_event.get("usage", {})
    steps[name] = {
        "input_tokens":       usage.get("input_tokens", 0),
        "output_tokens":      usage.get("output_tokens", 0),
        "cost_usd":           result_event.get("total_cost_usd"),
        "permission_denials": result_event.get("permission_denials", []),
    }
    if model == "unknown":
        model_usage = result_event.get("modelUsage", {})
        if model_usage:
            model = next(iter(model_usage))

meta = {
    "gen":                 gen_n,
    "speckit_version":     speckit_version,
    "claude_code_version": claude_code_version,
    "model":               model,
    "start_time":          start_time,
    "end_time":            end_time,
    "steps":               steps,
}
if failed_step:
    meta["failed_step"]      = failed_step
    meta["failed_exit_code"] = failed_exit

pathlib.Path("metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
print("metadata.json written")
PYEOF
}

_on_exit() {
    local code=$?
    [[ -d "$KIT" ]] || return 0
    mkdir -p "$KIT/logs"
    pushd "$KIT" > /dev/null 2>&1 || return 0
    _write_metadata "$code" || true
    popd > /dev/null 2>&1 || true
}
trap _on_exit EXIT

# --- step 0: capture start time and versions ---
CURRENT_STEP="versions"
START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CLAUDE_VERSION=$(claude --version 2>&1 | head -1)

# --- step 1: install Spec Kit at the pinned tag ---
CURRENT_STEP="install-speckit"
uv tool install specify-cli \
    --from "git+https://github.com/github/spec-kit.git@v1.1.0" \
    --force
SPECKIT_VERSION=$(specify --version 2>&1 | head -1)

# --- step 2: build kit and init ---
CURRENT_STEP="build-kit"
uv run python "$REPO_ROOT/arms/approach3/make_kit.py" "$GEN_N"
cd "$KIT"
CURRENT_STEP="speckit-init"
specify init . --integration claude --force --non-interactive
mkdir -p logs

# --- step 2.5: install kit dependencies ---
CURRENT_STEP="uv-sync"
uv sync

# --- step 3: run the five SDD skills ---
TOOLS_BASE="Read(./**),Glob(./**),Grep(./**),Write(./checkpoint.py),Write(./specs/**),Write(./.specify/memory/**),Write(./.specify/feature.json),Write(./tests/generated/**),Edit(./checkpoint.py),Edit(./specs/**),Edit(./.specify/memory/**),Edit(./.specify/feature.json),Edit(./tests/generated/**),Bash(.specify/scripts/bash/*),Bash(./.specify/scripts/bash/*),Bash(bash .specify/scripts/bash/*),Bash(git init),Bash(git add *),Bash(git commit *),Bash(git checkout -b *),Bash(mkdir -p *),Bash(uv run pytest*)"

run_step() {
    local name="$1"
    local tools="$2"
    local prompt="$3"
    CURRENT_STEP="step-$name"
    printf '%s' "$prompt" | claude --print --output-format stream-json --verbose \
        --model "$MODEL" \
        --allowedTools "$tools" \
        > "logs/step-${name}.jsonl"
}

run_step constitution "$TOOLS_BASE" \
    '/speckit-constitution This is a Python agent-side policy hook. It enforces policy rules P1-P7 via a BeforeToolCallEvent callback before every tool call. Read spec_input.md for the full specification. The module must produce a single file checkpoint.py containing a PolicyHook class. No server or MCP code is involved.'

run_step specify "$TOOLS_BASE" \
    '/speckit-specify Read spec_input.md and implement the PolicyHook class in checkpoint.py. The class must read allowed_tools and fingerprints from the agent config file at run time (never hard-coded), check the bearer token scope, apply rules P1-P7, log each decision, and let directory 5xx errors propagate.'

run_step plan "$TOOLS_BASE" '/speckit-plan'

run_step tasks "$TOOLS_BASE" '/speckit-tasks'

run_step implement "$TOOLS_BASE" '/speckit-implement'

END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CURRENT_STEP=""

echo "Generation $GEN_N complete: $KIT"
