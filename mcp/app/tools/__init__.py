"""MCP tool registry.

Each ``tools/<svc>.py`` exposes ``register(mcp, deps)``; ``register_all`` wires
them onto the server. Adding a service's tools = a drop-in module here + a client
on ``Deps`` + an ``mcp->{svc}`` scope. The mail module is a Phase-5 stub (no-op).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.deps import Deps
from app.tools import jobs, leads, mail, search


def register_all(mcp: FastMCP, deps: Deps) -> None:
    leads.register(mcp, deps)
    search.register(mcp, deps)
    jobs.register(mcp, deps)
    mail.register(mcp, deps)  # no-op until Phase 5


__all__ = ["register_all"]
