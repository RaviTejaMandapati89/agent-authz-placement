"""Shared agent grant table used by the gateway across all approaches."""

AGENT_GRANTS: dict[str, list[str]] = {
    "expense-assistant": ["read_receipt", "submit_expense", "approve_expense", "ask_payments_agent"],
    "travel-assistant":  ["book_travel", "submit_expense", "ask_payments_agent"],
    "payments-agent":    ["pay_vendor", "approve_expense"],
}
