"""Single authoritative map from tool name to required OAuth scope."""

TOOL_SCOPE_MAP: dict[str, str] = {
    "read_receipt":        "expenses:read",
    "submit_expense":      "expenses:submit",
    "approve_expense":     "expenses:approve",
    "book_travel":         "travel:book",
    "pay_vendor":          "payments:pay",
    "ask_payments_agent":  "agents:payments",
}
