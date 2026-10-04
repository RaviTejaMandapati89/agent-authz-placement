// P7: base grant — tool is in the agent's allowed set and user meets the role requirement
@id("P7")
permit(principal, action, resource)
when {
    resource.allowed_tools.contains(context.action_name) &&
    (resource.acts_for == "any" || principal.role == resource.acts_for)
};

// P7: blank user is always denied
@id("P7")
forbid(principal, action, resource)
when { principal.username == "" };

// P6: tool definition must match the reviewed manifest
@id("P6")
forbid(principal, action, resource)
when { !context.tool_reviewed };

// P1: user may only submit expenses as their own claimant
@id("P1")
forbid(principal, action, resource)
when {
    context has claimant &&
    context.claimant != principal.username
};

// P2: expenses above the limit require an approval reference (limit: £500)
@id("P2")
forbid(principal, action, resource)
when {
    context has amount_pence &&
    context.amount_pence > 50000 &&
    !(context has has_approval_ref && context.has_approval_ref)
};

// P3: a user cannot approve their own expense
@id("P3")
forbid(principal, action, resource)
when {
    context has expense_claimant &&
    context.expense_claimant == principal.username
};

// P3: the approver must be the claimant's manager
@id("P3")
forbid(principal, action, resource)
when {
    context has expense_claimant_manager &&
    context.expense_claimant_manager != principal.username
};

// P4: booking travel for another person requires an active delegation
@id("P4")
forbid(principal, action, resource)
when {
    context has traveller &&
    context.traveller != principal.username &&
    !(context has travel_delegators && context.travel_delegators.contains(context.traveller))
};

// P5: vendor must be on the approved list
@id("P5")
forbid(principal, action, resource)
when {
    context has vendor_approved &&
    !context.vendor_approved
};
