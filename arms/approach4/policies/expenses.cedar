permit(principal, action, resource);

@id("P1")
forbid(principal, action, resource)
when { context has claimant && context.claimant != principal.username };

@id("P2")
forbid(principal, action, resource)
when { context has amount_pence && context.amount_pence > 50000 && !(context has has_approval_ref && context.has_approval_ref) };

@id("P3")
forbid(principal, action, resource)
when { context has expense_claimant && context.expense_claimant == principal.username };

@id("P3")
forbid(principal, action, resource)
when { context has expense_claimant && !(context has approver_is_manager && context.approver_is_manager) };
