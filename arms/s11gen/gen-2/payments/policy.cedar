permit (
  principal is User,
  action,
  resource is Resource
);

@id("P7")
forbid (
  principal is User,
  action,
  resource is Resource
)
when {
  principal.role != "finance"
};

@id("P3")
forbid (
  principal is User,
  action == Action::"approve_expense",
  resource is Resource
)
when {
  context has expense_claimant &&
  context.expense_claimant == principal.username
};

@id("P3")
forbid (
  principal is User,
  action == Action::"approve_expense",
  resource is Resource
)
when {
  !(context has approver_is_manager && context.approver_is_manager)
};

@id("P5")
forbid (
  principal is User,
  action == Action::"pay_vendor",
  resource is Resource
)
when {
  !(context has vendor &&
    (context.vendor == "acme-hotels" ||
     context.vendor == "fastair" ||
     context.vendor == "reliable-cabs"))
};
