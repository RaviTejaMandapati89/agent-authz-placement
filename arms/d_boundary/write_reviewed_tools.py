"""
Hash every tool in the server registry and write arms/d_boundary/reviewed_tools.json.

Run once against the clean server, then commit the output:

    uv run python -m arms.d_boundary.write_reviewed_tools

Reads the FastMCP tool manager directly — no MCPClient, no subprocess.
ENFORCEMENT must NOT be set when running this script.
"""
import json
import os
import pathlib

if os.environ.get("ENFORCEMENT") == "cedar":
    raise SystemExit(
        "ENFORCEMENT=cedar must not be set when running write_reviewed_tools — "
        "unset it first so the PEP does not try to load reviewed_tools.json before it exists."
    )

from arms.d_boundary.pep import fingerprint
from domain.server import mcp

_OUT = pathlib.Path(__file__).parent / "reviewed_tools.json"


def main() -> None:
    fps = {
        name: fingerprint(tool.name, tool.description, tool.parameters)
        for name, tool in mcp._tool_manager._tools.items()
    }
    _OUT.write_text(json.dumps(fps, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, fp in sorted(fps.items()):
        print(f"  {name}: {fp}")
    print(f"\nWrote {_OUT}")


if __name__ == "__main__":
    main()
