"""Minimal MCP server whose only side-effecting tool is guarded by mcp-consent-gate.

Run:  python examples/send_message_server.py   (Streamable HTTP on http://127.0.0.1:8765/mcp)
"""
from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer

from mcp_consent_gate import ConsentGate, PendingConfirmation, Proposal

mcp = MCPServer("consent-gate-example")
gate = ConsentGate()
SENT: list[dict] = []


@mcp.tool(description="Send a message to someone. Always asks the user first.")
async def send_message(to: str, text: str, ctx: Context, confirmation_code: str | None = None) -> dict:
    proposal = Proposal(action="send_message", summary=f"Send this message to {to}?", details={"to": to, "text": text})
    decision = await gate.request(ctx, proposal, confirmation_code=confirmation_code)
    if isinstance(decision, PendingConfirmation):
        return decision.to_dict()
    if not decision.approved:
        return {"status": "not_sent", "reason": decision.reason, "digest": decision.digest}
    SENT.append({"to": to, "text": text})
    return {"status": "sent", "digest": decision.digest, "method": decision.method}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=8765)
