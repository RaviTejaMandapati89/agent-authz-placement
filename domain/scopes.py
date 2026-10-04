"""Single authoritative tool-to-scope map shared by the gateway and approach 2."""

TOOL_SCOPE_MAP: dict[str, str] = {
    "read_receipt":    "expenses:read",
    "submit_expense":  "expenses:submit",
    "approve_expense": "expenses:approve",
    "book_travel":     "travel:book",
    "pay_vendor":      "payments:pay",
}
