"""Share TLS configuration while keeping each request's cookies isolated."""
from contextlib import asynccontextmanager
import asyncio
import ssl
import threading

import certifi
import httpx
from admin import request_metrics

_ssl_context = None
_ssl_lock = threading.Lock()


def ssl_context():
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context(cafile=certifi.where())
        return _ssl_context


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport, *, recovery=None, budget=None, limits=None):
        self.transport = transport
        self.recovery = recovery if recovery is not None else {'used': False}
        self.budget = budget
        self.limits = limits
        self.private = []

    async def handle_async_request(self, request):
        # Attach timings to the actual request; no URLs, headers or bodies retained.
        if 'trace' not in request.extensions:
            sync_trace = request_metrics.network_trace()
            async def trace(name, info):
                sync_trace(name, info)
            request.extensions['trace'] = trace
        try:
            response = await self.transport.handle_async_request(request)
            request_metrics.mark_upstream_response(response.status_code)
            return response
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                httpx.RemoteProtocolError) as error:
            request_metrics.mark_network_error(error)
            # Only replay an in-memory request before ANY response headers.
            # Read/write timeouts and errors while consuming a response are not
            # replayed here. A lost response can still mean upstream accepted
            # the POST; this is bounded recovery, not exactly-once delivery.
            try:
                request.content
            except httpx.RequestNotRead:
                raise error
            if self.recovery['used'] or (self.budget is not None and not self.budget.available()):
                raise
            self.recovery['used'] = True
            if self.budget is not None:
                self.budget.consume()
            request_metrics.mark_connection_recovery()
            await asyncio.sleep(.2)
            fresh = httpx.AsyncHTTPTransport(limits=self.limits, verify=ssl_context())
            self.private.append(fresh)
            # A new pool guarantees the retry cannot reuse the broken socket.
            try:
                response = await fresh.handle_async_request(request)
                request_metrics.mark_upstream_response(response.status_code)
                return response
            except httpx.TransportError as retry_error:
                request_metrics.mark_network_error(retry_error)
                raise

    async def aclose(self):
        # The application owns the pool, not an individual request.
        for transport in self.private:
            await transport.aclose()
        self.private.clear()


async def start(app, limits):
    # Reuse a few recent connections instead of doing DNS/TCP/TLS on every
    # mobile call. Recovery below is limited to one pre-header disconnect.
    mobile_limits = httpx.Limits(max_connections=limits.max_connections,
                               max_keepalive_connections=4, keepalive_expiry=30)
    app.state.upstream_transport = httpx.AsyncHTTPTransport(limits=mobile_limits, verify=ssl_context())


async def stop(app):
    transport = getattr(app.state, 'upstream_transport', None)
    app.state.upstream_transport = None
    if transport is not None:
        await transport.aclose()


@asynccontextmanager
async def client(request, timeout, limits, *, retry_budget=None):
    # Direct route tests can create a Request without an application in scope.
    app = request.scope.get('app') if hasattr(request, 'scope') else getattr(request, 'app', None)
    if app is None:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as connection:
            yield connection
        return
    transport = getattr(getattr(app, 'state', None), 'upstream_transport', None)
    kwargs = {'timeout': timeout, 'limits': limits}
    scope = getattr(request, 'scope', {})
    recovery = scope.setdefault('_upstream_recovery', {'used': False})
    owned = transport is None
    if owned:
        transport = httpx.AsyncHTTPTransport(limits=limits, verify=ssl_context())
    kwargs['transport'] = _BorrowedTransport(transport, recovery=recovery,
                                             budget=retry_budget, limits=limits)
    try:
        async with httpx.AsyncClient(**kwargs) as connection:
            yield connection
    finally:
        if owned:
            await transport.aclose()
