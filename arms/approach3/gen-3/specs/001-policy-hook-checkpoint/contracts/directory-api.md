# Contract: Directory HTTP API

The checkpoint fetches live facts from the directory service on every tool-call evaluation. No response is cached. All routes are accessed via `GET` over the `base_url` passed to `PolicyHook.__init__`.

---

## GET /directory/users/{username}

Retrieves the role and manager for a user.

**Used by**: acts_for role check (step 3), P3 no-self-approval (second lookup)

**Request**
```
GET {base_url}/directory/users/{username}
```
- `username`: canonicalised username string

**Response 200**
```json
{
  "role": "employee",
  "manager": "bob"
}
```
- `role` (string): user's role, compared against `acts_for` config field
- `manager` (string | null): username of direct manager; `null` if no manager

**Response 404**
```json
{"error": "user 'x' not found"}
```
The checkpoint treats 404 as "user not found" and applies the applicable deny rule (P7 for acts_for check, P3 for manager lookup).

**Response 5xx**
Must propagate to the framework — do not catch.

---

## GET /directory/delegations

Retrieves active delegations filtered by the directory (expired delegations may still appear; the checkpoint checks `active` field only).

**Used by**: P4 live travel delegation check

**Request**
```
GET {base_url}/directory/delegations
```

**Response 200**
```json
{
  "delegations": [
    {
      "id": "del-001",
      "delegator": "carol",
      "delegate": "dan",
      "scope": ["travel"],
      "expires": "2027-12-31T23:59:59Z",
      "active": true
    }
  ],
  "sim_time": 1234567890.0
}
```
- `delegations` (array): list of delegation objects; may be empty
- `sim_time` (float): simulation clock value at the moment of the response — **the only directory endpoint that returns this field**; propagated to the audit log entry
- Each delegation has:
  - `delegator` (string): the person who granted the delegation (the traveller)
  - `delegate` (string): the person who received the delegation (the booking agent)
  - `scope` (array of strings): must contain `"travel"` for a valid travel delegation
  - `active` (boolean): **the only field the checkpoint checks for validity**; must be `true`
  - `expires` (string): ISO-8601 datetime; the directory pre-filters but the checkpoint does **not** compare timestamps

**Valid travel delegation for booking user U to travel for traveller T**:
```
delegator == T AND delegate == U AND active == true AND "travel" in scope
```

**Response 5xx**
Must propagate to the framework — do not catch.

---

## GET /directory/vendors

Retrieves the approved vendor list.

**Used by**: P5 approved vendor list check

**Request**
```
GET {base_url}/directory/vendors
```

**Response 200**
```json
["acme-hotels", "fastair", "reliable-cabs"]
```
A plain JSON array of vendor name strings. The checkpoint compares `canonicalise(vendor_argument)` against this list.

**Response 5xx**
Must propagate to the framework — do not catch.

---

## GET /directory/expenses/{expense_id}

Retrieves the claimant and status of an expense record.

**Used by**: P3 no-self-approval check

**Request**
```
GET {base_url}/directory/expenses/{expense_id}
```
- `expense_id`: the expense identifier from the `approve_expense` tool call arguments

**Response 200**
```json
{
  "claimant": "alice",
  "status": "pending"
}
```
- `claimant` (string): the user who submitted the expense; compared against the acting user
- `status` (string): not used by the checkpoint for policy decisions

**Response 404**
```json
{"error": "expense 'x' not found"}
```
Treated as "expense not found"; deny with P3.

**Response 5xx**
Must propagate to the framework — do not catch.

---

## Error handling summary

| Status code | Action |
|-------------|--------|
| 200 | Parse JSON, continue evaluation |
| 404 | Treat as "not found", apply deny rule for the check in progress |
| 4xx (not 404) | `raise_for_status()` — propagates to framework |
| 5xx | `raise_for_status()` — propagates to framework |
