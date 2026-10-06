# What the gateway passes to the policy

Each tool call becomes one Cedar request.

## Entities

- Principal: `User::"<sub>"`, where `<sub>` is the signed-in user's name (for
  example `alice`). The User entity has two attributes: `username` (String, the
  same as `<sub>`) and `role` (String: `employee`, `manager`, `executive`,
  `assistant` or `finance`).
- Resource: `Resource::"r"`. It has no attributes.

## Actions

The action is `Action::"<tool>"`, where `<tool>` is one of: `read_receipt`,
`submit_expense`, `approve_expense`, `book_travel`, `pay_vendor`.

## Context attributes

Every request has `action_name` (String, the tool name). The other attributes
are present only for the tool shown, and only when the gateway could fill them.
A policy must test that an attribute is present before it reads it.

| Tool | Attribute | Type | Meaning |
|---|---|---|---|
| `submit_expense` | `claimant` | String | the person the expense is submitted for |
| `submit_expense` | `amount_pence` | Long | the amount in pence (£1 is 100) |
| `submit_expense` | `has_approval_ref` | Boolean | whether an approval reference was given |
| `approve_expense` | `expense_claimant` | String | the claimant of the expense being approved |
| `approve_expense` | `approver_is_manager` | Boolean | true when the expense's claimant reports to the approver |
| `book_travel` | `traveller` | String | the person travelling |
| `book_travel` | `has_travel_delegation` | Boolean | whether a live travel delegation exists |
| `pay_vendor` | `vendor` | String | the vendor name |

## How a refusal is reported

The gateway evaluates the policy with Cedar's default deny: a request is
allowed only if some `permit` matches and no `forbid` matches. When a `forbid`
refuses a request, the gateway reports that policy's `@id` annotation as the
refusing rule. Every `forbid` rule must therefore carry an `@id` annotation
equal to the rule's label in requirements.md (for example `@id("P1")`).
