"""
Compute reviewed tool fingerprints and write them into the three config files.

Run once against the clean domain server, then commit the output:

    uv run python -m arms.c_hook.write_fingerprints

The fingerprint function is imported from hook.py so the script and the hook
always hash tool specs the same way.
"""
import pathlib
import tempfile

import yaml
from strands.tools.mcp import MCPClient

from arms.c_hook.hook import fingerprint
from runner._server import domain_server

_CONFIG_DIR = pathlib.Path(__file__).parent / "config"
_AGENTS = ["expense-assistant", "travel-assistant", "payments-agent"]


def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        scratch_log = tmp.name

    with domain_server(scratch_log) as (port, base_url):
        mcp_url = f"{base_url}/mcp"
        with MCPClient(url=mcp_url) as client:
            tools = list(client.list_tools_sync())

    fps: dict[str, str] = {t.tool_name: fingerprint(t.tool_spec) for t in tools}

    for agent_name in _AGENTS:
        path = _CONFIG_DIR / f"{agent_name}.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        allowed = config.get("allowed_tools", [])
        config["fingerprints"] = {name: fps[name] for name in allowed if name in fps}
        path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False), encoding="utf-8")
        print(f"Updated {path.name}: {list(config['fingerprints'].keys())}")


if __name__ == "__main__":
    main()
