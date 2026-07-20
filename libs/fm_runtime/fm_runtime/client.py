"""InternalClient — the only sanctioned way to call another in-cluster service.

Wraps httpx and, per request:
- resolves the outbound token via the TokenBroker (RFC 8693 exchange of the
  current principal's token for the target audience; client-credentials when
  acting as the service itself),
- attaches it as Authorization: Bearer,
- propagates trace headers (traceparent/tracestate/b3/x-request-id).

`InternalClient.detached(...)` captures the current principal's subject token
for work that outlives the request (this repo's detached NDJSON ingest jobs):
the job keeps exchanging against the captured subject token until it expires,
exactly matching today's session-lifetime semantics.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from fm_runtime.context import current_principal, current_trace_headers
from fm_runtime.tokens import TokenBroker, get_broker


class InternalClient:
    def __init__(
        self,
        base_url: str,
        audience: str,
        *,
        subject_token: str | None = None,
        follow_context: bool = True,
        timeout: httpx.Timeout | float | None = 30.0,
        broker: TokenBroker | None = None,
    ) -> None:
        """follow_context=True (default): the subject token and trace headers
        are read from the current request context on every call.
        follow_context=False: `subject_token` (possibly None → service
        identity) and captured trace headers are frozen in — for detached
        jobs."""
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._broker = broker or get_broker()
        self._follow_context = follow_context
        self._fixed_subject = subject_token
        self._fixed_trace = {} if follow_context else current_trace_headers()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    @classmethod
    def detached(cls, base_url: str, audience: str, **kwargs: Any) -> "InternalClient":
        """Freeze the current principal's token + trace context for a
        background job started inside a request."""
        principal = current_principal()
        return cls(
            base_url,
            audience,
            subject_token=principal.raw_token if principal else None,
            follow_context=False,
            **kwargs,
        )

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if self._follow_context:
            principal = current_principal()
            subject = principal.raw_token if principal else None
            trace = current_trace_headers()
        else:
            subject = self._fixed_subject
            trace = dict(self._fixed_trace)
        headers = dict(trace)
        token = await self._broker.token_for(self.audience, subject)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        kwargs["headers"] = await self._headers(kwargs.get("headers"))
        return await self._client.request(method, path, **kwargs)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def stream_lines(
        self, method: str, path: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        """Iterate response lines of an NDJSON upstream, header handling
        included. Raises httpx.HTTPStatusError on a non-2xx status before
        yielding anything."""
        kwargs["headers"] = await self._headers(kwargs.get("headers"))
        async with self._client.stream(method, path, **kwargs) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "InternalClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
