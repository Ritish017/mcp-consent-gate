# mcp-consent-gate

**Ask before acting, for Model Context Protocol tools.** Wrap any side-effecting MCP tool (send a message, place an order, unlock a door) so it runs only after a human approves *the exact action*.

- **Elicitation first.** Uses MCP form elicitation (spec 2025-11-25) to show the user the action and let them toggle individual parts ("remind Dad" ✔, "text Priya" ✘).
- **Bound to content.** The approval covers a SHA-256 digest of the action name, parameters and choices. Rewording the summary does not change it. Changing a recipient does.
- **Fails closed.** Decline, cancel, timeout, malformed answers and transport errors all mean *no*.
- **Works with every client.** If the client cannot elicit (or a 2026-07-28 stateless request has no back channel), the tool returns `needs_confirmation` with a one-time code. The assistant reads the action to the user and calls again with the code. A code cannot approve a different action, expires, and works once.
- **Audit trail.** Every outcome goes to an `AuditLog` (in-memory, JSON Lines, or your own sink).

```python
from mcp.server.mcpserver import Context, MCPServer
from mcp_consent_gate import ConsentGate, PendingConfirmation, Proposal

mcp = MCPServer("example")
gate = ConsentGate()   # or ConsentGate(JsonlAuditLog("audit.jsonl"))

@mcp.tool()
async def send_message(to: str, text: str, ctx: Context, confirmation_code: str | None = None) -> dict:
    decision = await gate.request(
        ctx,
        Proposal(action="send_message", summary=f"Send this to {to}?", details={"to": to, "text": text}),
        confirmation_code=confirmation_code,
    )
    if isinstance(decision, PendingConfirmation):
        return decision.to_dict()          # client can't elicit: ask the user, call again with the code
    if not decision.approved:
        return {"status": "not_sent", "reason": decision.reason}
    ...                                    # do the thing
    return {"status": "sent", "digest": decision.digest}
```

Multi-part approvals: pass `choices={"sms": "Send as SMS", "email": "Send as email"}` and check `decision.chose("sms")`.

## Install and test

```bash
pip install -e .           # requires mcp>=2.0 (official Python SDK)
pip install pytest && pytest -q
```

12 tests cover accept, decline, cancel, unticking everything, invalid content, timeout, the code fallback (two-phase commit, wrong-action rejection, replay, expiry), the no-back-channel fallback, and the JSONL audit log. They run against real MCP client/server sessions (in-process).

## Why

Agentic assistants are getting tools that act in the real world. MCP has elicitation, but every server re-implements "are you sure?" differently, often as one yes/no that is not tied to what actually executes. This package makes the safe pattern a few lines of code. It was built for [CareCircle](../../README.md), an Alexa+ MCP server that only contacts family members after the user approves the exact wording.

MIT licensed.
