"""Route unusable fake-IP DNS answers without changing system networking."""
import asyncio
from contextlib import asynccontextmanager
from functools import partial
import ipaddress
import logging
import os
from urllib.request import getproxies

import httpx

from admin import network_adapt

_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")
_log = logging.getLogger(__name__)


def _explicit_proxy() -> bool:
    return any(os.getenv(key) for key in
               ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                "HTTP_PROXY", "http_proxy"))


def _verified_address(host: str) -> str | None:
    addresses = network_adapt.local_addresses(host)
    if not addresses or not all(ipaddress.ip_address(ip) in _FAKE_IP_RANGE for ip in addresses):
        return None
    _, detail = network_adapt.resolver_rules([host])
    return detail.get(host, {}).get("override")


def _route(request: httpx.Request, host: str, trust_env: bool = True) -> None:
    if (os.getenv("WORKBUDDY_ANDROID") == "1"
            or request.url.scheme != "https" or request.url.port not in (None, 443)
            or request.url.host != host or "sni_hostname" in request.extensions):
        return
    if trust_env and _explicit_proxy():
        return
    ip = _verified_address(host)
    if not ip:
        return
    # Keep certificate verification and HTTP routing bound to the original host.
    # This runs BEFORE sending the request: POSTs are never replayed here.
    request.headers["Host"] = request.url.netloc.decode("ascii")
    request.extensions["sni_hostname"] = host
    request.url = request.url.copy_with(host=ip)
    _log.info("Using verified per-request DNS route for %s", host)


async def _route_async(request: httpx.Request, host: str, trust_env: bool = True) -> None:
    await asyncio.to_thread(_route, request, host, trust_env)


def _options(backend_url: str, options: dict, asynchronous: bool = False) -> dict:
    options = dict(options)
    host = httpx.URL(backend_url).host
    # macOS urllib also discovers system proxies. Only the verified IP gets a
    # direct mount; all other destinations retain HTTPX's existing proxy rules.
    if (options.get("proxy") is None and options.get("transport") is None
            and options.get("trust_env", True) and not _explicit_proxy()
            and os.getenv("WORKBUDDY_ANDROID") != "1"):
        proxies = getproxies()
        if proxies.get("https") or proxies.get("all"):
            ip = _verified_address(host)
            if ip:
                mounts = dict(options.get("mounts", {}) or {})
                transport = httpx.AsyncHTTPTransport if asynchronous else httpx.HTTPTransport
                transport_options = {key: options[key] for key in
                                     ("verify", "cert", "trust_env", "http1", "http2", "limits")
                                     if key in options}
                mounts.setdefault("https://" + ip, transport(**transport_options))
                options["mounts"] = mounts
    hooks = dict(options.pop("event_hooks", {}) or {})
    if options.get("proxy") is None:
        hooks["request"] = list(hooks.get("request", [])) + [
            partial(_route_async if asynchronous else _route, host=host,
                    trust_env=options.get("trust_env", True))]
    return {"event_hooks": hooks, **options}


def client(backend_url: str, **options) -> httpx.Client:
    return httpx.Client(**_options(backend_url, options))


@asynccontextmanager
async def async_client(backend_url: str, **options):
    kwargs = await asyncio.to_thread(_options, backend_url, options, True)
    async with httpx.AsyncClient(**kwargs) as connection:
        yield connection
