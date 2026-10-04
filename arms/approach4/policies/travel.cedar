permit(principal, action, resource);

@id("P1")
forbid(principal, action, resource)
when { context has claimant && context.claimant != principal.username };

@id("P2")
forbid(principal, action, resource)
when { context has amount_pence && context.amount_pence > 50000 && !(context has has_approval_ref && context.has_approval_ref) };

@id("P4")
forbid(principal, action, resource)
when { context has traveller && context.traveller != principal.username && !(context has has_travel_delegation && context.has_travel_delegation) };
