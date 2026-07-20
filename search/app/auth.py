"""Request identity for the search backend.

Authorization is enforced by the mesh (Istio mTLS + OPA ext_authz) before a
request reaches this process; fm_runtime's PrincipalMiddleware has already
parsed the forwarded JWT and 401-ed non-anonymous routes without one. These
dependencies only hand the handlers what they need:

- ``get_current_user`` — the acting principal as the legacy ``UserOut`` shape
  (``username`` keys search-history ownership rows).
- ``oauth2_scheme`` — the principal's raw subject token, which LeadsClient
  exchanges (RFC 8693) for a leads-audience token on every outbound call.
"""

from fastapi import HTTPException, status

from fm_runtime import current_principal

from app.schemas import UserOut

SERVICE_NAME = "search"


async def oauth2_scheme() -> str | None:
    """Subject token for outbound token exchange (name kept so existing
    ``Depends(oauth2_scheme)`` call sites read unchanged)."""
    principal = current_principal()
    return principal.raw_token if principal else None


async def get_current_user() -> UserOut:
    principal = current_principal()
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return UserOut(
        username=principal.username,
        role="admin" if "admin" in principal.roles else "",
    )
