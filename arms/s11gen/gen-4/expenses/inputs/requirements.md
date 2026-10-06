# Requirements: the Expenses use case

This use case covers the expense assistant. It helps an employee submit an
expense and lets a manager approve one. The company wants the rules below
enforced on every request the assistant makes. Each rule has a label, so a
refusal can say which rule refused it.

**P1. Own expenses only.** A user can submit an expense only in their own name.
An assistant acting for alice cannot submit an expense with bob as the claimant.

**P2. Single expense limit.** An expense above £500 can be submitted only with
an approval reference. Without one it is refused, not queued.

**P3. Approval.** Nobody approves their own expense. The approver must be the
claimant's manager. An agent acting for the claimant can never approve that
claimant's expense, whatever it is told to do.

Anything these rules do not refuse is allowed.
