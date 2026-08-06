"""Dependencies wired once in ``main.py`` and handed to every tool module.

``Deps`` bundles one ``BackendClient`` per upstream. ``main.py`` builds it and
passes it to ``tools.register_all(mcp, deps)``; each ``tools/<svc>.py`` reaches
its backend through the matching attribute. Adding a service = a new client
attribute + a drop-in tool module.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.clients import BackendClient


@dataclass(frozen=True)
class Deps:
    leads: BackendClient
    search: BackendClient
    jobs: BackendClient
    mail: BackendClient
    # Optional: wired only when KNOWLEDGE_BACKEND_URL is set (the knowledge
    # service integrates at the operator's convenience; None => its tools are
    # not registered).
    knowledge: BackendClient | None = None
