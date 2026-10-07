# Where should an AI agent's permission check sit?

A small comparison of six ways to place the "is this person allowed to do this?" check for AI agents: rules in the prompt, a checkpoint inside the agent (hand-written, and generated from a spec), a gateway with its own copy of the rules, and a gateway backed by a central policy service (asked on every decision, or evaluated from a published copy). Sixteen scenarios, 1,900 runs, every verdict scored from logs.

Start here:

- [RESULTS.md](RESULTS.md): the findings, in plain words.
- `results/official-2026-10-06/`: the run records: every run as a line of `results.jsonl`, the summary table, the token budget.

The scenarios and the pass/fail criteria were written down before any run. They are in [DESIGN.md](DESIGN.md), the spec the harness was built from, and anything changed afterwards is listed at the end of it with a date. The official run is tagged `task8-frozen`.

Built with Strands Agents and Claude on Amazon Bedrock, tools over MCP, agent-to-agent calls over A2A, policies in Cedar, OAuth token exchange (RFC 8693), SPIFFE-style agent identity, and GitHub Spec Kit as agent skills in Claude Code. The gateway and the central policy service were built for this study; they follow the documented behaviour of real products and standards but are not those products.

## Sources

Where the placements and the mechanisms come from. Each is documented on its own; putting them side by side and testing them is mine.

- Instructions are not a control. OWASP Top 10 for LLM Applications 2025, LLM07: https://genai.owasp.org/llmrisk/llm072025-system-prompt-leakage/
- Checks inside the agent. OpenAI Agents SDK guardrails: https://openai.github.io/openai-agents-python/guardrails/ and Strands Agents hooks: https://strandsagents.com/docs/user-guide/sdk/agents/hooks/
- Harness engineering. OpenAI: https://openai.com/index/harness-engineering/
- Spec-driven development. GitHub Spec Kit: https://github.com/github/spec-kit
- Gateways. Google Cloud Agent Gateway: https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview and AWS Bedrock AgentCore Policy: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html
- Policy language. Cedar authorization semantics, forbid overrides permit: https://docs.cedarpolicy.com/auth/authorization.html
- Tool access. Model Context Protocol specification (2025-11-25), authorization: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- Agent identity. NIST NCCoE concept paper, February 2026: https://csrc.nist.gov/pubs/other/2026/02/05/accelerating-the-adoption-of-software-and-ai-agent/ipd
- Acting on someone's behalf. RFC 8693, OAuth 2.0 Token Exchange: https://www.rfc-editor.org/rfc/rfc8693
- Pushed revocation. OpenID Continuous Access Evaluation Profile 1.0: https://openid.net/specs/openid-caep-1_0.html

Ravi Teja Mandapati, October 2026. Views my own. Licence: MIT (see [LICENSE.md](LICENSE.md)).
