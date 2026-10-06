// P7: Every action of the payments agent is for users in the finance role.
@id("P7")
forbid(
  principal is User,
  action,
  resource
)
when {
  principal.role != "finance"
};

// P3: Nobody approves their own expense; approver must be the claimant's manager.
@id("P3")
forbid(
  principal is User,
  action == Action::"approve_expense",
  resource
)
when {
  context has expense_claimant &&
  (principal.username == context.expense_claimant ||
   !(context has approver_is_manager) ||
   context.approver_is_manager == false)
};

// P5: The agent pays only approved vendors.
@id("P5")
forbid(
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
forbid(
  principal is User,
  action == Action::"pay_vendor",
  resource
)
unless {
  context has vendor
};

// Permit everything not explicitly forbidden.
permit(
  principal is User,
  action,
  resource
);
