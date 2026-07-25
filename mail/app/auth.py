"""Request identity for the mail backend.

Authorization is enforced by the mesh (Istio mTLS + OPA ext_authz) before a
request reaches this process; fm_runtime's PrincipalMiddleware has already
parsed the forwarded JWT and 401-ed non-anonymous routes without one. This
dependency hands handlers the acting principal in the legacy ``UserOut``
shape (``username`` is recorded as ``connected_by`` on mailbox connects).

The only anonymous routes are the health probes and the Google OAuth
callback — Google cannot send a bearer token, so the callback authenticates
with a single-use ``state`` row minted by ``GET /api/mail/oauth/url``.
"""

from fastapi import HTTPException, status

from fm_runtime import current_principal

from app.schemas import UserOut

SERVICE_NAME = "mail"


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
        origin=principal.origin,
        actor=principal.actor,
    )
