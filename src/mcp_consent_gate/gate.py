"""Core of mcp-consent-gate.

Design rules
------------
* Fail closed: decline, cancel, a timeout, a malformed answer or a transport
  error all produce ``Decision(approved=False)``.
* Bind approval to content: the user approves a digest of the exact action
  (action name, details, choices). A confirmation code issued for one proposal
  cannot approve a different one, and can be used once.
* Degrade gracefully: clients without the ``elicitation`` capability get a
  ``PendingConfirmation`` with a short code that the user must read back.
* Leave a trail: every outcome is appended to an ``AuditLog``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import anyio
from pydantic import BaseModel, Field, create_model

Method = Literal["elicitation", "confirmation_code", "none"]


class Proposal(BaseModel):
    """An action the server wants to take, described for a human."""

    action: str = Field(description="Machine name of the action, e.g. 'notify_caregiver'.")
    summary: str = Field(description="One sentence a person can approve or refuse.")
    details: dict[str, Any] = Field(default_factory=dict, description="Exact parameters that will be used.")
    choices: dict[str, str] = Field(
        default_factory=dict,
        description="Optional sub-actions the user can toggle individually: key -> label.",
    )
    defaults: dict[str, bool] = Field(default_factory=dict, description="Default value per choice (True if omitted).")
    allow_note: bool = Field(default=False, description="Offer a free-text note field.")

    def default_choices(self) -> dict[str, bool]:
        return {k: self.defaults.get(k, True) for k in self.choices}


def proposal_digest(proposal: Proposal) -> str:
    """SHA-256 over the canonical JSON of what will actually be executed."""
    canonical = json.dumps(
        {"action": proposal.action, "details": proposal.details, "choices": sorted(proposal.choices)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Decision:
    approved: bool
    digest: str
    method: Method
    choices: dict[str, bool] = field(default_factory=dict)
    note: str | None = None
    reason: str = ""

    def chose(self, key: str) -> bool:
        return self.approved and self.choices.get(key, False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "digest": self.digest,
            "method": self.method,
            "choices": self.choices,
            "note": self.note,
            "reason": self.reason,
        }


@dataclass
class PendingConfirmation:
    """Returned when the client cannot elicit: ask the user, then call again with the code."""

    code: str
    digest: str
    expires_at: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "needs_confirmation",
            "confirmation_code": self.code,
            "digest": self.digest,
            "expires_at": self.expires_at,
            "message": self.message,
        }


class AuditLog(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


class MemoryAuditLog:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)


class JsonlAuditLog:
    """Append-only JSON Lines audit file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _client_can_elicit(ctx: Any) -> bool:
    try:
        caps = ctx.client_capabilities
    except Exception:  # no request context
        return False
    return bool(caps is not None and getattr(caps, "elicitation", None) is not None)


def _render_message(p: Proposal) -> str:
    lines = [p.summary]
    for k, v in p.details.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _schema_for(p: Proposal) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    if p.choices:
        for key, label in p.choices.items():
            fields[key] = (bool, Field(default=p.defaults.get(key, True), title=label, description=label))
    else:
        fields["approve"] = (bool, Field(default=True, title="Approve", description=p.summary))
    if p.allow_note:
        fields["note"] = (str, Field(default="", title="Note", description="Optional note"))
    return create_model("ConsentForm", **fields)  # type: ignore[call-overload]


class ConsentGate:
    """Ask the human before a tool acts.

    Usage inside an MCP tool::

        gate = ConsentGate()

        @mcp.tool()
        async def send_message(to: str, text: str, ctx: Context, confirmation_code: str | None = None):
            decision = await gate.request(ctx, Proposal(action="send_message", summary=f"Send '{text}' to {to}?",
                                                         details={"to": to, "text": text}),
                                          confirmation_code=confirmation_code)
            if isinstance(decision, PendingConfirmation):
                return decision.to_dict()
            if not decision.approved:
                return {"status": "not_sent", "reason": decision.reason}
            ...
    """

    def __init__(
        self,
        audit: AuditLog | None = None,
        *,
        timeout_seconds: float = 120.0,
        code_ttl_seconds: float = 300.0,
        secret: bytes | None = None,
        clock: Callable[[], float] = time.time,
        allow_code_fallback: bool = True,
    ) -> None:
        self.audit: AuditLog = audit if audit is not None else MemoryAuditLog()
        self.timeout_seconds = timeout_seconds
        self.code_ttl_seconds = code_ttl_seconds
        self._secret = secret or secrets.token_bytes(32)
        self._clock = clock
        self.allow_code_fallback = allow_code_fallback
        self._pending: dict[str, tuple[str, float]] = {}  # code -> (digest, expires_at)
        self._used: set[str] = set()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- public
    async def request(
        self, ctx: Any, proposal: Proposal, *, confirmation_code: str | None = None
    ) -> Decision | PendingConfirmation:
        digest = proposal_digest(proposal)
        if confirmation_code:
            return self._finish(proposal, self._redeem(proposal, digest, confirmation_code))
        if _client_can_elicit(ctx):
            decision = await self._elicit(ctx, proposal, digest)
            if decision is not None:
                return self._finish(proposal, decision)
            # The client advertised elicitation but this connection has no
            # server->client back channel (e.g. a stateless 2026-07-28 request):
            # fall through to the confirmation-code path instead of failing.
        if self.allow_code_fallback:
            pending = self._issue(digest)
            self._log(proposal, digest, "pending", "confirmation_code", {}, None, "awaiting read-back of code")
            return pending
        return self._finish(proposal, Decision(False, digest, "none", reason="client cannot elicit and fallback disabled"))

    # --------------------------------------------------------------- elicit
    async def _elicit(self, ctx: Any, p: Proposal, digest: str) -> Decision | None:
        schema = _schema_for(p)
        try:
            with anyio.fail_after(self.timeout_seconds):
                result = await ctx.elicit(_render_message(p), schema)
        except TimeoutError:
            return Decision(False, digest, "elicitation", reason="timed out waiting for the user")
        except Exception as exc:  # transport error, invalid content...
            if type(exc).__name__ == "NoBackChannelError":
                return None
            return Decision(False, digest, "elicitation", reason=f"elicitation failed: {type(exc).__name__}")
        if result.action != "accept":
            return Decision(False, digest, "elicitation", reason=f"user chose {result.action}")
        data = result.data.model_dump()
        note = data.pop("note", None) or None
        if p.choices:
            choices = {k: bool(data.get(k, False)) for k in p.choices}
            approved = any(choices.values())
        else:
            choices = {}
            approved = bool(data.get("approve", False))
        return Decision(approved, digest, "elicitation", choices, note,
                        "approved" if approved else "user unticked every option")

    # ----------------------------------------------------------- code path
    def _make_code(self, digest: str, nonce: str) -> str:
        mac = hmac.new(self._secret, f"{digest}:{nonce}".encode(), hashlib.sha256).digest()
        return base64.b32encode(mac)[:6].decode()

    def _issue(self, digest: str) -> PendingConfirmation:
        code = self._make_code(digest, secrets.token_hex(8))
        expires = self._clock() + self.code_ttl_seconds
        with self._lock:
            self._pending[code] = (digest, expires)
        return PendingConfirmation(
            code=code,
            digest=digest,
            expires_at=expires,
            message=(
                "This action needs the user's explicit OK. Read the summary to the user; if they agree, "
                f"call the tool again with the same arguments and confirmation_code='{code}'."
            ),
        )

    def _redeem(self, p: Proposal, digest: str, code: str) -> Decision:
        code = code.strip().upper()
        with self._lock:
            if code in self._used:
                return Decision(False, digest, "confirmation_code", reason="code already used")
            entry = self._pending.get(code)
            if entry is None:
                return Decision(False, digest, "confirmation_code", reason="unknown code")
            bound_digest, expires = entry
            if self._clock() > expires:
                self._pending.pop(code, None)
                return Decision(False, digest, "confirmation_code", reason="code expired")
            if not hmac.compare_digest(bound_digest, digest):
                return Decision(False, digest, "confirmation_code",
                                reason="code was issued for a different action")
            self._pending.pop(code, None)
            self._used.add(code)
        return Decision(True, digest, "confirmation_code", p.default_choices(), None, "approved by read-back code")

    # --------------------------------------------------------------- audit
    def _finish(self, p: Proposal, d: Decision) -> Decision:
        self._log(p, d.digest, "approved" if d.approved else "denied", d.method, d.choices, d.note, d.reason)
        return d

    def _log(self, p: Proposal, digest: str, outcome: str, method: str, choices: dict[str, bool],
             note: str | None, reason: str) -> None:
        self.audit.record({
            "ts": self._clock(),
            "action": p.action,
            "summary": p.summary,
            "details": p.details,
            "digest": digest,
            "outcome": outcome,
            "method": method,
            "choices": choices,
            "note": note,
            "reason": reason,
        })
