"""Generate tool fingerprints and write to the shared domain/fingerprints.json."""
import json
import pathlib

from arms.approach4.plugin import fingerprint
from domain.server import mcp

_OUT = pathlib.Path(__file__).parent.parent.parent / "domain" / "fingerprints.json"

fps = {
    name: fingerprint(tool.name, tool.description, tool.parameters)
    for name, tool in mcp._tool_manager._tools.items()
}
_OUT.write_text(json.dumps(fps, indent=2, sort_keys=True))
print(f"wrote {len(fps)} fingerprints to {_OUT}")
