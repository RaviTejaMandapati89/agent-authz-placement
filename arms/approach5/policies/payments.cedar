permit(principal, action, resource);

@id("P7")
forbid(principal, action, resource)
when { principal.role != "finance" };
