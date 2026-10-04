permit(principal, action, resource);

@id("P1")
forbid(principal, action, resource)
when { context has claimant && context.claimant != principal.username };

@id("P2")
forbid(principal, action, resource)
when {
    context has amount_pence &&
    context has expense_limit_pence &&
    context.amount_pence > context.expense_limit_pence &&
    !(context has has_approval_ref && context.has_approval_ref)
};

@id("P3")
forbid(principal, action, resource)
when { context has expense_claimant && context.expense_claimant == principal.username };

@id("P3")
forbid(principal, action, resource)
when { context has expense_claimant && !(context has approver_is_manager && context.approver_is_manager) };

@id("P4")
forbid(principal, action, resource)
when {
    context has traveller &&
    context.traveller != principal.username &&
    !(context has has_travel_delegation && context.has_travel_delegation)
};

@id("P5")
forbid(principal, action, resource)
when { context has vendor && !(context has vendor_is_approved && context.vendor_is_approved) };
