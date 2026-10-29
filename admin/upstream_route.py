"""Route unusable fake-IP DNS answers without changing system networking."""
import asyncio
from functools import partial
import ipaddress
import logging
import os
from urllib.request import getproxies

import httpx

from admin import network_adapt

_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")
_log = logging.getLogger(__name__)


def _route(request: httpx.Request, host: str, trust_env: bool = True) -> None:
    if (os.getenv("WORKBUDDY_ANDROID") == "1"
            or request.url.scheme != "https" or request.url.port not in (None, 443)
            or request.url.host != host or "sni_hostname" in request.extensions):
        return
    proxies = getproxies() if trust_env else {}
    if proxies.get("https") or proxies.get("all"):
        return
    addresses = network_adapt.local_addresses(host)
    if not addresses or not all(ipaddress.ip_address(ip) in _FAKE_IP_RANGE for ip in addresses):
        return
    _, detail = network_adapt.resolver_rules([host])
    ip = detail.get(host, {}).get("override")
    if not ip:
        _log.warning("No verified alternate route for fake-IP upstream %s", host)
        return
    # Keep certificate verification and HTTP routing bound to the original host.
    # This runs BEFORE sending the request: POSTs are never replayed here.
    request.headers["Host"] = request.url.netloc.decode("ascii")
    request.extensions["sni_hostname"] = host
    request.url = request.url.copy_with(host=ip)
    _log.info("Using verified per-request DNS route for %s", host)


async def _route_async(request: httpx.Request, host: str, trust_env: bool = True) -> None:
    await asyncio.to_thread(_route, request, host, trust_env)


def client(backend_url: str, **options) -> httpx.Client:
    hooks = dict(options.pop("event_hooks", {}) or {})
    if options.get("proxy") is None:
        hooks["request"] = list(hooks.get("request", [])) + [
            partial(_route, host=httpx.URL(backend_url).host,
                    trust_env=options.get("trust_env", True))]
    return httpx.Client(event_hooks=hooks, **options)


def async_client(backend_url: str, **options) -> httpx.AsyncClient:
    hooks = dict(options.pop("event_hooks", {}) or {})
    if options.get("proxy") is None:
        hooks["request"] = list(hooks.get("request", [])) + [
            partial(_route_async, host=httpx.URL(backend_url).host,
                    trust_env=options.get("trust_env", True))]
    return httpx.AsyncClient(event_hooks=hooks, **options)
