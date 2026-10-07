# Where should an AI agent's permission check sit? Results

Ravi Teja Mandapati, October 2026

Six ways of placing the check, sixteen scenarios, 1,900 runs, all on one frozen version of the code, run on 6 October 2026. The scenarios and the pass/fail criteria were written down before any run; they are in [DESIGN.md](DESIGN.md), and anything changed afterwards is listed at the end of it with a date. This page gives the findings in plain words. The figures behind them can be checked against the run records in `results/official-2026-10-06/`: `summary.md` has the verdict for every approach and scenario, and `results.jsonl` has every run.

## The question

Where should an AI agent's "are you allowed to do this?" check sit? The guidance I could find documents three places: inside the agent's own code, at a gateway in front of the tools, and in a central service holding the rules every team shares. Each is documented somewhere. I found nothing that compares them.

In an earlier project of mine, agents that run know-your-customer checks, I had put the check in front of the systems and never tested whether that was the right place. So I built one small made-up company, gave six versions of the same agents the same tasks and the same attacks, and recorded where each held and where each failed. Three questions:

1. If the agent's own harness checks every tool call, is a gateway still needed?
2. If a gateway enforces policy, is a shared central source of rules still needed?
3. When one agent asks another to act, does the check still follow the person the work is for?

## Six places to put the check

Two approaches check inside the agent, one written by hand and one generated from a spec. Three check at a gateway and differ in where the shared rules come from. The sixth, rules in the prompt alone, is where most teams start.

| # | Approach | Where the decision is made |
| --- | --- | --- |
| 1 | Instructions to the agent | In the model, from rules in its prompt |
| 2 | Checkpoint inside the agent, written by hand | In the agent's runtime, before each tool call |
| 3 | Checkpoint inside the agent, generated from a spec | Same place as 2; generated five times from one spec |
| 4 | Policy at a gateway | At a gateway, from use-case policies and claims in the token |
| 5 | Gateway with central policy, called per decision | Use-case policies at the gateway; shared rules and facts from a central service, asked on every person-level decision |
| 6 | Gateway with central policy, evaluated locally | As 5, but the gateway evaluates a published copy and acts on pushed revocation events |

## How it was tested

Same company, same tokens, same scenarios for every approach.

- One company: expenses and travel, a handful of people, seven rules, five tools.
- Every call carries a signed token. An agent acting for someone holds an on-behalf-of token from token exchange (RFC 8693): the person is the subject, the agent the actor.
- Only the instructions-only agents are shown the rules. Every other approach gets the same identity-only prompt, so where the check sits is the only difference.
- Sixteen scenarios, from a plain expense claim to a revoked delegation, a tool that changes after review, a script holding a leaked token, and one agent asking another to pay a vendor.
- Every scenario ran ten times for every approach it applies to. The generated checkpoint ran against five separate generations from the same spec.
- Grading reads only the ledger, the decision logs and the token issuer's logs, never the agent's own account of what it did. A refusal counts only when a log line names the rule that refused.

## What I learned before the first official run

Four things surfaced while wiring the scenarios end to end. None is a result about the six approaches. Each changed the harness before the official run.

1. **My gateway skipped three token checks, and the whole test suite passed.** It never checked expiry, never checked when a token was issued or became valid, and accepted a token at the exact second it expired. The expired-token scenario, written as a control every approach should pass, exposed the first.
2. **Generated controls inherit the gaps in their spec.** My spec for the generated checkpoint named the inputs to a tool fingerprint but not how to serialise them, and gave delegation direction only in prose. Two of the five generations hash differently and refuse every legitimate tool call; a third guessed the delegation field names. All five passed their own contract tests.
3. **A grader can report a pass for the wrong reason.** Three generations refused the revoked booking only because they refused every booking, the legitimate one included. Every verdict now sits beside the legitimate work completed and the rule that refused.
4. **Generated policies can be invalid and still look right.** Most of the Cedar policies generated for one scenario fail schema validation: each reads an optional attribute without first checking it exists. Cedar skips a policy that errors, so a refusal rule written this way could let a request through. In this study those attributes are always supplied, so no result depends on it. A policy store that validates on upload would have rejected them all.

## What the runs showed

Every cell of ten runs gave the same verdict all ten times, so what follows is about scenarios, not luck.

**A check inside the agent cannot see a call the agent never makes.** Both in-agent checkpoints, hand-written and generated, let the leaked-token scenario through every time: a script holding a leaked token called the approve tool directly, with no agent in the loop to object. Every gateway refused it. That is close to true by construction, and it is the whole case for a gateway. The checkpoints did catch the agent calling a tool its owner never approved, but the credit belongs to the token: its scopes already reflected the tools the agent was approved for.

The checkpoints also looked up delegations from the company directory on every call. That is why they caught the revoked booking at once: they were already reading a live, shared source of facts. When the directory went down, the hand-written checkpoint crashed.

**A gateway with its own copy of the rules is only as current as the copy.** It held the generated-policy scenario as well as the central approaches did, and fell behind in three places. A revoked delegation kept working until the token expired, five minutes or an hour depending on the token. After the limit was lowered centrally, the agent channel kept allowing an expense that the other channel refused, and the two channels still disagreed at the end of the hour measured. And a use-case team could loosen the shared limit by editing its own copy of it. The gateway that asks the central service refused in all three cases; in the last one because the central forbid overrides any use-case permit.

**Evaluating a local copy keeps most of that.** The gateway working from a published copy caught the revocation a second after it happened, the delay of its revocation event. For the rule change it lagged by its publishing interval, a quarter of an hour, against a full hour of disagreement for the gateway with its own copy. In the outage it carried on from its last copy, where the per-decision gateway refused and named the outage.

**The person travelled with the token.** When one agent asked the payments agent to pay a vendor, every approach except instructions alone refused, and refused for the person: the on-behalf-of token carried her as the subject into the second agent, and a gateway reading only the token got this right with no central lookup. Not tested: a change to her entitlement while the token is still valid.

**Generating the checkpoint from a spec changed what was refused, not what got through.** One generation of five behaved exactly like the hand-written checkpoint in every scenario, and a second differed only in how it logged the outage. The other three refused legitimate work, two of them all of it, because of the gaps in my spec. None of the five let through anything the hand-written checkpoint refused.

**Rules in the prompt alone are not a control.** With the rules only in its instructions, the model did decline some scenarios on its own, but nothing in any log says which rule applied. It used a changed tool and an unreviewed one, rebooked after a revocation, and paid a vendor for someone not entitled to it. Behind a gateway, every decision on a valid token left a full record: who, which agent, which tool, the decision and, for refusals, the rule. With rules only in the prompt, fewer than one call in five did.

**Asking the central service on every decision cost milliseconds and no tokens.** On one machine it added about nine milliseconds at the median; over a real network it would cost more. In tokens the gateway approaches were the cheapest, at roughly half the cost per completed task of instructions alone, because the rules never travel in the prompt and, most likely, because a gateway never offers the agent tools it may not use. Changing a rule took one edit for the central approaches and for instructions alone, and two files each for the checkpoints and for the gateway with its own copy.

## Limits

- One made-up domain, seven rules, one model at temperature 0. Ten runs mostly measure consistency, not variety.
- Identity is simulated: no workload attestation, test signing keys, one machine. Latency is local and understates real deployments.
- The gateway and central service were built for this study. They follow the documented behaviour of real products and standards, but are not those products.
- Chains of agents stop at two hops, and the agent-to-agent library used is labelled experimental.
- Part of the generated checkpoint's false refusals comes from gaps in my own spec, described above.

## How it was built

I collated the design from the public sources listed in DESIGN.md and chose the scenarios. The code was vibe-coded with Claude Code from a spec, one task at a time: failing tests first, then the build, with every commit run and checked by me. This write-up was drafted with Claude and edited by me. The design decisions and the reasons for them are in DESIGN.md.
