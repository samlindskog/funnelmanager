"""Mail tools — DEFERRED to Phase 5.

The mail MCP surface (``/api/mail/mcp/v1/*``) and the ``mcp->mail`` svc scope do
NOT exist yet (per docs/agent-build-plan.md, mail's full client + campaigns land
in Phase 5). This module is an intentional stub so the tool surface is obvious
and the drop-in pattern is documented; ``register`` is a no-op until then.

When Phase 5 lands, wire it up:
  - add ``mail: BackendClient`` to ``app.deps.Deps`` (audience ``mail``),
    construct it in ``main.py`` with ``settings.mail_backend_url``, add
    ``MAIL_BACKEND_URL`` to config, and provision the ``mcp->mail`` scope;
  - implement the tools here against ``deps.mail`` and add ``register(mcp, deps)``
    to ``tools.register_all``:
      list_campaigns / get_campaign (readOnly), start_campaign / continue_campaign
      (write), contacted_contacts (readOnly), list_messages / get_thread
      (readOnly), send_message (write, not idempotent).

Tool names/schemas are a stable v1 contract — add them additively; never
repurpose an existing tool.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.deps import Deps


def register(mcp: FastMCP, deps: Deps) -> None:  # noqa: ARG001 - stub until Phase 5
    """No-op: mail MCP endpoints (and the mcp->mail scope) do not exist yet."""
    return None
