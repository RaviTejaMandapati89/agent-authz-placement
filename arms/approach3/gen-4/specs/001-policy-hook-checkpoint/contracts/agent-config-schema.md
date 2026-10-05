# Contract: Agent Configuration YAML Schema

**Location**: `{config_dir}/{agent_name}.yaml`

**Default config_dir**: `<module_dir>/config/agents/`

---

## Schema

```yaml
agent: <string>                    # required — agent name (informational)
acts_for: <string>                 # required — "any" or a role name
allowed_tools:                     # required — list of permitted tool names (may be empty)
  - <tool_name>
  - ...
fingerprints:                      # optional — map of tool name to SHA-256 hex digest
  <tool_name>: <hex_string>
  ...
expense_limit: <integer>           # optional — max submit_expense amount without approval_ref
```

---

## Field Details

### `agent` (required)

Informational identifier. Should match the filename stem (e.g., `expense-assistant` → `expense-assistant.yaml`).

### `acts_for` (required)

Controls the role restriction check (step 3 in the check order):

- `"any"` — no role restriction; any user may use this agent
- Any other string (e.g., `"finance"`) — only users whose `role` in the directory matches
  this value may proceed; others are denied with P7

### `allowed_tools` (required)

List of tool names this agent is permitted to call. An empty list (`[]`) means no tools are
permitted (all calls denied with P7 at step 2).

**Example**:
```yaml
allowed_tools:
  - read_receipt
  - submit_expense
  - approve_expense
```

### `fingerprints` (optional)

Map from tool name to its expected SHA-256 fingerprint. The fingerprint is computed over:
```python
json.dumps(
    {"name": spec["name"], "description": spec["description"],
     "inputSchema": spec["inputSchema"]},
    sort_keys=True,
).encode()
```

If a tool name appears in `fingerprints`, the fingerprint check (P6, step 4) is performed for
that tool. If no entry exists for a tool, the fingerprint check is skipped for that tool.

**Example**:
```yaml
fingerprints:
  read_receipt: 80d48b7490ae5b989880a39a257d8eadf182d3d6bed8e55783b8560a8912eabb
  submit_expense: 4548fbca0d1440ee2f3bb0aa3fdc4514a38e452fd5b421c898528a0a5b486207
```

### `expense_limit` (optional)

Integer. Maximum amount for a `submit_expense` call that does not require an `approval_ref`.
If the submitted amount exceeds this value and `approval_ref` is absent or null, the call is
denied with P2.

If this field is absent from the config, the P2 limit check is treated as a no-op (no limit
applies). A `limit_override` passed to the `PolicyHook` constructor takes precedence over this
field.

---

## Complete Example

```yaml
agent: expense-assistant
acts_for: any
expense_limit: 500
allowed_tools:
  - read_receipt
  - submit_expense
  - approve_expense
fingerprints:
  read_receipt: 80d48b7490ae5b989880a39a257d8eadf182d3d6bed8e55783b8560a8912eabb
  submit_expense: 4548fbca0d1440ee2f3bb0aa3fdc4514a38e452fd5b421c898528a0a5b486207
  approve_expense: fe14fa2f110a789720b2a8b5c3a64b3c45cb57684fabcc9a8af8a101275e378f
```
