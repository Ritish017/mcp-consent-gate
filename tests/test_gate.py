import json
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import Context, MCPServer
import mcp.types as types

from mcp_consent_gate import (ConsentGate, JsonlAuditLog, MemoryAuditLog, PendingConfirmation, Proposal,
                              proposal_digest)

pytestmark = pytest.mark.anyio


def out(r):
    """Tool result as a dict (dict-returning tools come back as JSON text)."""
    sc = r.structured_content
    return sc if sc is not None else json.loads(r.content[0].text)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_server(gate: ConsentGate, executed: list):
    mcp = MCPServer("t")

    @mcp.tool()
    async def notify(to: str, text: str, ctx: Context, confirmation_code: str | None = None) -> dict:
        p = Proposal(action="notify", summary=f"Message {to}?", details={"to": to, "text": text},
                     choices={"sms": "Send as SMS", "email": "Send as email"}, defaults={"email": False},
                     allow_note=True)
        d = await gate.request(ctx, p, confirmation_code=confirmation_code)
        if isinstance(d, PendingConfirmation):
            return d.to_dict()
        if d.approved:
            executed.append({"to": to, "choices": d.choices, "note": d.note})
        return d.to_dict()

    return mcp


def answer(action="accept", content=None, seen=None):
    async def cb(ctx, params):
        if seen is not None:
            seen.append(params)
        return types.ElicitResult(action=action, content=content)
    return cb


async def test_digest_is_stable_and_content_bound():
    a = Proposal(action="x", summary="s", details={"to": "a", "n": 1})
    b = Proposal(action="x", summary="different wording", details={"n": 1, "to": "a"})
    c = Proposal(action="x", summary="s", details={"to": "b", "n": 1})
    assert proposal_digest(a) == proposal_digest(b)  # wording does not change what is approved
    assert proposal_digest(a) != proposal_digest(c)


async def test_elicitation_accept_runs_only_selected_choices():
    executed, seen = [], []
    gate = ConsentGate()
    srv = make_server(gate, executed)
    async with Client(srv, mode="legacy", elicitation_callback=answer("accept", {"sms": True, "email": False, "note": "call later"}, seen)) as c:
        r = await c.call_tool("notify", {"to": "Priya", "text": "Dad missed a pill"})
    res = out(r)
    assert res["approved"] is True and res["method"] == "elicitation"
    assert executed == [{"to": "Priya", "choices": {"sms": True, "email": False}, "note": "call later"}]
    schema = seen[0].requested_schema
    assert set(schema["properties"]) == {"sms", "email", "note"}
    assert schema["properties"]["email"]["default"] is False
    assert "Dad missed a pill" in seen[0].message
    assert gate.audit.entries[-1]["outcome"] == "approved"


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_decline_and_cancel_fail_closed(action):
    executed = []
    gate = ConsentGate()
    async with Client(make_server(gate, executed), mode="legacy", elicitation_callback=answer(action)) as c:
        r = await c.call_tool("notify", {"to": "Priya", "text": "hi"})
    assert out(r)["approved"] is False
    assert executed == []
    assert gate.audit.entries[-1]["outcome"] == "denied"


async def test_unticking_everything_is_a_refusal():
    executed = []
    gate = ConsentGate()
    async with Client(make_server(gate, executed),
                      mode="legacy", elicitation_callback=answer("accept", {"sms": False, "email": False})) as c:
        r = await c.call_tool("notify", {"to": "Priya", "text": "hi"})
    assert out(r)["approved"] is False and executed == []


async def test_invalid_elicitation_content_fails_closed():
    executed = []
    gate = ConsentGate()
    async with Client(make_server(gate, executed),
                      mode="legacy", elicitation_callback=answer("accept", {"sms": "definitely", "email": []})) as c:
        r = await c.call_tool("notify", {"to": "Priya", "text": "hi"})
    assert out(r)["approved"] is False
    assert "elicitation failed" in out(r)["reason"]
    assert executed == []


async def test_code_fallback_two_phase_commit_and_replay_protection():
    executed = []
    gate = ConsentGate()
    srv = make_server(gate, executed)
    async with Client(srv) as c:  # no elicitation_callback -> client does not advertise elicitation
        first = out(await c.call_tool("notify", {"to": "Priya", "text": "hi"}))
        assert first["status"] == "needs_confirmation" and executed == []
        code = first["confirmation_code"]
        # a code cannot approve a different action
        other = out(await c.call_tool("notify", {"to": "Mallory", "text": "hi", "confirmation_code": code}))
        assert other["approved"] is False and "different action" in other["reason"]
        ok = out(await c.call_tool("notify", {"to": "Priya", "text": "hi", "confirmation_code": code}))
        assert ok["approved"] is True and ok["method"] == "confirmation_code"
        assert ok["choices"] == {"sms": True, "email": False}  # defaults apply
        replay = out(await c.call_tool("notify", {"to": "Priya", "text": "hi", "confirmation_code": code}))
        assert replay["approved"] is False and "already used" in replay["reason"]
    assert len(executed) == 1


async def test_code_expiry():
    now = [1000.0]
    executed = []
    gate = ConsentGate(code_ttl_seconds=60, clock=lambda: now[0])
    async with Client(make_server(gate, executed)) as c:
        code = out(await c.call_tool("notify", {"to": "P", "text": "t"}))["confirmation_code"]
        now[0] += 61
        r = out(await c.call_tool("notify", {"to": "P", "text": "t", "confirmation_code": code}))
    assert r["approved"] is False and r["reason"] == "code expired" and executed == []


async def test_fallback_can_be_disabled():
    gate = ConsentGate(allow_code_fallback=False)
    async with Client(make_server(gate, [])) as c:
        r = out(await c.call_tool("notify", {"to": "P", "text": "t"}))
    assert r["approved"] is False and "fallback disabled" in r["reason"]


async def test_jsonl_audit_log(tmp_path: Path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    gate = ConsentGate(log)
    async with Client(make_server(gate, []), mode="legacy", elicitation_callback=answer("decline")) as c:
        await c.call_tool("notify", {"to": "P", "text": "t"})
    rows = log.read()
    assert len(rows) == 1 and rows[0]["outcome"] == "denied" and len(rows[0]["digest"]) == 64


async def test_slow_user_times_out_closed():
    import anyio

    async def slow(ctx, params):
        await anyio.sleep(2)
        return types.ElicitResult(action="accept", content={"sms": True, "email": True})

    executed = []
    gate = ConsentGate(timeout_seconds=0.2)
    async with Client(make_server(gate, executed), mode="legacy", elicitation_callback=slow) as c:
        r = out(await c.call_tool("notify", {"to": "P", "text": "t"}))
    assert r["approved"] is False and "timed out" in r["reason"] and executed == []


async def test_modern_connection_without_back_channel_falls_back_to_code():
    """In-process 'auto' mode negotiates 2026-07-28 with no server->client channel."""
    executed = []
    gate = ConsentGate()
    async with Client(make_server(gate, executed), elicitation_callback=answer("accept", {"sms": True})) as c:
        r = out(await c.call_tool("notify", {"to": "P", "text": "t"}))
    assert r["status"] == "needs_confirmation" and executed == []
