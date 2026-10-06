# Requirements: the Payments use case

This use case covers the payments agent. It pays vendors for the company and
can approve an expense when asked. The company wants the rules below enforced
on every request the agent makes. Each rule has a label, so a refusal can say
which rule refused it.

**P3. Approval.** Nobody approves their own expense. The approver must be the
claimant's manager. An agent acting for the claimant can never approve that
claimant's expense, whatever it is told to do.

**P5. Approved vendors only.** The agent pays only these vendors: acme-hotels,
fastair and reliable-cabs. Any other vendor is refused.

**P7. Finance only.** Every action of the payments agent is for users in the
finance role. A request for anyone else is refused, whatever the action.

Anything these rules do not refuse is allowed.
