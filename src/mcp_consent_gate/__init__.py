"""mcp-consent-gate: ask-before-acting for MCP tools.

Wrap any side-effecting MCP tool so it only runs after a human approves the
exact action. Uses MCP form elicitation (spec 2025-11-25) when the client
supports it, and a one-time confirmation code (two-phase commit) when it
does not. Every decision is bound to a SHA-256 digest of the proposal and
written to an append-only audit log.
"""

from .gate import (
    AuditLog,
    ConsentGate,
    Decision,
    JsonlAuditLog,
    MemoryAuditLog,
    PendingConfirmation,
    Proposal,
    proposal_digest,
)

__all__ = [
    "AuditLog",
    "ConsentGate",
    "Decision",
    "JsonlAuditLog",
    "MemoryAuditLog",
    "PendingConfirmation",
    "Proposal",
    "proposal_digest",
]
__version__ = "0.1.0"
