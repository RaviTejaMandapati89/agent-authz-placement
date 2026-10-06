// Permit all actions by default; forbids below override per requirements.
permit (
  principal,
  action,
  resource
);

// P1: A user can submit an expense only in their own name.
@id("P1")
forbid (
  principal,
  action == Action::"submit_expense",
  resource
)
when {
  context has claimant &&
  context.claimant != principal.username
};

// P2: An expense above £500 (50000 pence) requires an approval reference.
@id("P2")
forbid (
  principal,
  action == Action::"submit_expense",
  resource
)
when {
  context has amount_pence &&
  context.amount_pence > 50000 &&
  (!(context has has_approval_ref) || !context.has_approval_ref)
};

// P3: Nobody approves their own expense; approver must be claimant's manager.
@id("P3")
forbid (
  principal,
  action == Action::"approve_expense",
  resource
)
when {
  (context has expense_claimant && context.expense_claimant == principal.username) ||
  (!(context has approver_is_manager) || !context.approver_is_manager)
};
