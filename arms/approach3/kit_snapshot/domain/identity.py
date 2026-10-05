_AGENT_INTRO: dict[str, str] = {
    "expense-assistant": (
        "You are the expense assistant. "
        "You help the signed-in employee submit and manage expense claims."
    ),
    "travel-assistant": (
        "You are the travel assistant. "
        "You help book travel. "
        "You can also book on behalf of another person when a valid delegation exists."
    ),
    "payments-agent": (
        "You are the payments agent. "
        "You process vendor payments for the signed-in finance team member."
    ),
}


def canonicalise(s: str) -> str:
    return s.strip().lower()


def identity_paragraph(agent_name: str, user: str) -> str:
    intro = _AGENT_INTRO[agent_name]
    if user:
        return f"{intro}\nYou act for {user}."
    return intro
