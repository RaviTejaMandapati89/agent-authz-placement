// P7: Finance only — permit all actions for finance users
permit (
  principal is User,
  action,
  resource
)
when {
  principal.role == "finance"
};

// P3: No self-approval — forbid approve_expense when the approver is the claimant
@id("P3")
forbid (
  principal is User,
  action == Action::"approve_expense",
  resource
)
when {
  context has expense_claimant &&
  context.expense_claimant == principal.username
};

// P3: Approver must be the claimant's manager
@id("P3")
forbid (
  principal is User,
  action == Action::"approve_expense",
  resource
)
when {
  !(context has approver_is_manager) ||
  !context.approver_is_manager
};

// P5: Approved vendors only
@id("P5")
forbid (
  principal is User,
  action == Action::"pay_vendor",
  resource
)
when {
  context has vendor &&
  context.vendor != "acme-hotels" &&
  context.vendor != "fastair" &&
  context.vendor != "reliable-cabs"
};

// P5: Forbid pay_vendor when vendor attribute is absent
@id("P5")
forbid (
  principal is User,
  action == Action::"pay_vendor",
  resource
)
unless {
  context has vendor
};

// P7: Finance only — forbid all actions for non-finance users
@id("P7")
forbid (
  principal is User,
  action,
  resource
)
when {
  principal.role != "finance"
};
