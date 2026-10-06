// P7: Finance only — permit all actions for finance users
permit (
  principal is User,
  action,
  resource
)
when {
  principal.role == "finance"
};

// P3: Forbid self-approval and non-manager approval
@id("P3")
forbid (
  principal is User,
  action == Action::"approve_expense",
  resource
)
when {
  context has expense_claimant &&
  (
    principal.username == context.expense_claimant ||
    !(context has approver_is_manager) ||
    context.approver_is_manager == false
  )
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

@id("P5")
forbid (
  principal is User,
  action == Action::"pay_vendor",
  resource
)
unless {
  context has vendor
};

// P7: Refuse non-finance users for every action
@id("P7")
forbid (
  principal is User,
  action,
  resource
)
when {
  principal.role != "finance"
};
