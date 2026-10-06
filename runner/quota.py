"""The read-only AWS CLI commands that show the account's Bedrock quotas for the
model the agents use, in the region they use (task 8, item 6). Prints them; calls
nothing. The model id and region come from runner.config, as the agents'."""
from runner import config as cfg


def commands() -> list[str]:
    """The agents' model id is a cross-region inference profile, so the first
    command shows which regions it routes to; the quotas are named by model
    family ("... Anthropic Claude Haiku 4.5"), applied and default."""
    query = ("\"Quotas[?contains(QuotaName, 'Haiku 4.5')]."
             "{Name:QuotaName,Code:QuotaCode,Value:Value,Unit:Unit,Adjustable:Adjustable}\"")
    common = f"--service-code bedrock --region {cfg.AWS_REGION} --query {query} --output table"
    return [
        f"aws bedrock get-inference-profile --inference-profile-identifier {cfg.MODEL_ID} "
        f"--region {cfg.AWS_REGION}",
        f"aws service-quotas list-service-quotas {common}",
        f"aws service-quotas list-aws-default-service-quotas {common}",
    ]


if __name__ == "__main__":
    print(f"model id: {cfg.MODEL_ID}\nregion:   {cfg.AWS_REGION}\n")
    print("\n\n".join(commands()))
