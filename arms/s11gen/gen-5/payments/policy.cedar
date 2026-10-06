// P7: Every action of the payments agent is for users in the finance role.
@id("P7")
forbid(
  principal is User,
  action,
  resource
)
unless {
  principal.role == "finance"
};

// P3: Nobody approves their own expense. The approver must be the claimant's manager.
@id("P3")
forbid(
  principal is User,
  action == Action::"approve_expense",
  resource
)
unless {
  context has expense_claimant &&
  context has approver_is_manager &&
  context.approver_is_manager == true &&
  context.expense_claimant != principal.username
};

// P5: The agent pays only approved vendors: acme-hotels, fastair, reliable-cabs.
@id("P5")
forbid(
  principal is User,
  action == Action::"pay_vendor",
  resource
)
unless {
  context has vendor &&
  (context.vendor == "acme-hotels" ||
   context.vendor == "fastair" ||
   context.vendor == "reliable-cabs")
};

// Permit all actions not refused by a forbid.
permit(
  principal is User,
  action,
  resource
);
