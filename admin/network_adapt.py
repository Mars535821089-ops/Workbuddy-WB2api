"""Adapt requests to whatever this machine's DNS actually returns.

Nothing here touches system state: no /etc/hosts, no networksetup, no system
proxy changes. The idea is that the desktop client resolves cloud endpoints
through this machine's DNS, and when that DNS answers with an address that
cannot be reached - a proxy tool's fake-ip range is the usual cause - every
cloud call from the client fails, which surfaces as an empty session list.

Chromium (and therefore Electron) accepts --host-resolver-rules, which maps a
hostname to an address for that one process only. It disappears when the
process exits. So instead of editing the machine we pass the override along
when relaunching the client.

The rules are only produced when the local answer is genuinely unusable. On a
machine whose DNS is healthy this module yields nothing and changes no
behaviour at all.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time as time_module

# Endpoints the desktop client needs to reach.
UPSTREAM_HOSTS = (os.getenv("WB_ADAPT_HOSTS", "copilot.tencent.com").strip() or
                  "copilot.tencent.com").split(",")

_DOH_ENDPOINTS = (
    "https://dns.alidns.com/resolve?name={}",
    "https://doh.pub/dns-query?name={}",
    "https://cloudflare-dns.com/dns-query?name={}",
)

_DEFAULT_TIMEOUT = 6.0

# Cache window for the whole probe, see resolver_rules().
_CACHE_TTL = 300.0
_RULES_CACHE: dict[tuple[str, ...], tuple[float, dict]] = {}


def forget_cache() -> None:
    """Drop cached probes - used by tests and by the "recheck" button."""
    _RULES_CACHE.clear()


def _urllib():
    from urllib.request import ProxyHandler, build_opener

    # Deliberately ignore any *_proxy environment: the whole point is to find
    # out what a direct connection looks like.
    return build_opener(ProxyHandler({}))


def local_addresses(host: str) -> list[str]:
    """What this machine's resolver says, A records only, deduplicated."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return []
    seen, out = set(), []
    for info in infos:
        ip = info[4][0]
        if "." not in ip or ip in seen:          # IPv4 only
            continue
        seen.add(ip)
        out.append(ip)
    return out


def _doh_query(url: str, timeout: float = 5.0) -> list[str]:
    req = _urllib()
    try:
        raw = req.open(url, timeout=timeout).read().decode("utf-8", "replace")
    except Exception:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    out = []
    for ans in payload.get("Answer") or []:
        # 1 = A, 5 = CNAME (the chain is followed by the DoH server already),
        # 28 = AAAA. Only literal IPv4 addresses are useful to us.
        if ans.get("type") != 1:
            continue
        data = str(ans.get("data", "")).strip().rstrip(".")
        if data.count(".") == 3 and data not in out:
            out.append(data)
    return out


def doh_addresses(host: str, timeout: float = 5.0) -> list[str]:
    """Ask several DoH servers. Agreement across providers is the point."""
    out: list[str] = []
    for tpl in _DOH_ENDPOINTS:
        for ip in _doh_query(tpl.format(host), timeout=timeout):
            if ip not in out:
                out.append(ip)
    return out


def address_reachable(host: str, ip: str, timeout: float = _DEFAULT_TIMEOUT) -> bool:
    """True if we can complete a TLS handshake with `ip` presenting `host`."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except Exception:
        return False


def _fallback_doh_via_curl(host: str) -> list[str]:
    """Some shells are locked down further than urllib; try curl directly."""
    out: list[str] = []
    for tpl in _DOH_ENDPOINTS:
        try:
            done = subprocess.run(
                ["curl", "-s", "--max-time", "6", "--noproxy", "*",
                 "-H", "accept: application/dns-json", tpl.format(host)],
                capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        try:
            payload = json.loads(done.stdout)
        except Exception:
            continue
        for ans in payload.get("Answer") or []:
            if ans.get("type") != 1:
                continue
            ip = str(ans.get("data", "")).strip().rstrip(".")
            if ip.count(".") == 3 and ip not in out:
                out.append(ip)
        if out:
            break
    return out


def usable_ip(host: str, timeout: float = _DEFAULT_TIMEOUT) -> str | None:
    """An address for `host` that actually answers TLS, or None.

    Local answers are tried first: when DNS is healthy this costs one
    handshake and nothing is overridden.
    """
    for ip in local_addresses(host):
        if address_reachable(host, ip, timeout=timeout):
            return None          # local DNS is fine, leave everything alone

    candidates = doh_addresses(host)
    if not candidates:
        candidates = _fallback_doh_via_curl(host)
    for ip in candidates:
        if address_reachable(host, ip, timeout=timeout):
            return ip
    return None


def resolver_rules(hosts=None, timeout: float = _DEFAULT_TIMEOUT) -> tuple[str | None, dict]:
    """Build a Chromium host-resolver-rules string.

    Returns (rules_or_None, detail). `rules_or_None` is None when every host
    already resolves to something reachable.

    Results are cached briefly: this runs on the click path of a UI button and
    a round of DoH lookups plus TLS handshakes is far too slow to repeat. The
    cache is deliberately short so a network that recovers is picked up again.
    """
    targets = [h.strip() for h in (hosts if hosts is not None else UPSTREAM_HOSTS)]
    targets = [h for h in targets if h]
    now = time_module.time()
    hit = _RULES_CACHE.get(tuple(targets))
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1].get("rules"), hit[1].get("detail", {})

    rules, detail = [], {}
    for host in targets:
        ip = usable_ip(host, timeout=timeout)
        if ip:
            rules.append(f"MAP {host} {ip}")
            detail[host] = {"local": local_addresses(host), "override": ip,
                            "reason": "local DNS answer not reachable"}
        else:
            detail[host] = {"local": local_addresses(host), "override": None,
                            "reason": ("reachable" if local_addresses(host)
                                       else "unresolved and unreachable")}
    out = (";".join(rules) if rules else None)
    _RULES_CACHE[tuple(targets)] = (now, {"rules": out, "detail": detail})
    return out, detail


def system_proxy() -> str | None:
    """The machine's HTTP proxy as a URL, without changing anything.

    Read-only, so launching the client with the same value the system already
    uses keeps every other request behaving exactly as it does today.
    """
    env = os.getenv("https_proxy") or os.getenv("HTTPS_PROXY") or \
        os.getenv("http_proxy") or os.getenv("HTTP_PROXY")
    if env:
        return env
    if sys.platform != "darwin":
        return None
    try:
        done = subprocess.run(["/usr/sbin/scutil", "--proxy"],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    cfg = {}
    for line in done.stdout.splitlines():
        key, _, val = line.partition(":")
        cfg[key.strip()] = val.strip()
    if cfg.get("HTTPEnable") != "1" and cfg.get("HTTPSEnable") != "1":
        return None
    host = cfg.get("HTTPProxy") or cfg.get("HTTPSProxy") or "127.0.0.1"
    port = cfg.get("HTTPPort") or cfg.get("HTTPSPort") or ""
    if not port:
        return None
    return f"http://{host}:{port}"


def electron_args(hosts=None, timeout: float = _DEFAULT_TIMEOUT) -> tuple[list[str], dict]:
    """Chromium argv fragment, ready to append when launching Electron.

    Two things have to happen together, and either alone is useless:

    * map the hostname straight to a working address, so the poisoned local
      resolver never gets a say;
    * tell Chromium to reach that host directly, because otherwise it hands the
      request to the system proxy, which resolves the name itself - through the
      very resolver we are working around.

    The bypass is scoped to exactly the hosts we had to remap. Everything else
    keeps using whatever proxy this machine is configured for.
    """
    hosts_list = [h.strip() for h in (hosts if hosts is not None else UPSTREAM_HOSTS)]
    hosts_list = [h for h in hosts_list if h]
    rules, detail = resolver_rules(hosts_list, timeout=timeout)
    if not rules:
        return [], detail

    args = ["--host-resolver-rules", rules]
    proxy = system_proxy()
    if proxy:
        # direct:// would also work, but bypassing only the affected hosts is
        # far less disruptive: every other request keeps the user's proxy.
        args += ["--proxy-server", proxy,
                 "--proxy-bypass-list", ";".join(hosts_list)]
        detail["_proxy"] = {"url": proxy, "bypassed_hosts": hosts_list,
                            "reason": "proxy would re-resolve these and hit the same dead address"}
    else:
        detail["_proxy"] = None
    return args, detail


if __name__ == "__main__":
    for h in UPSTREAM_HOSTS:
        ips = local_addresses(h)
        print(f"{h}: 本机解析={ips or '无'}", end="  ")
        ok = [ip for ip in ips if address_reachable(h, ip)]
        print(f"本机可达={ok or '否'}")
    rules, detail = resolver_rules()
    print("\n生成的映射:", rules)
    print("明细:", json.dumps(detail, ensure_ascii=False, indent=2))
