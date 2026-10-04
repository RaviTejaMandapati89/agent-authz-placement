permit(principal, action, resource);

@id("P3")
forbid(principal, action, resource)
when { context has expense_claimant && context.expense_claimant == principal.username };

@id("P3")
forbid(principal, action, resource)
when { context has expense_claimant && !(context has approver_is_manager && context.approver_is_manager) };

@id("P5")
forbid(principal, action, resource)
when { context has vendor && !["acme-hotels", "fastair", "reliable-cabs"].contains(context.vendor) };

@id("P7")
forbid(principal, action, resource)
when { principal.role != "finance" };
