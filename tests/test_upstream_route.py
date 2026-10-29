"""Synthetic DNS/transport regressions; no credentials, files or live network."""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import httpx

from admin import upstream_route
import converter

HOST = "copilot.tencent.com"
BACKEND = "https://" + HOST
URL = BACKEND + "/v2/chat/completions"


class RoutingFixture:
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(upstream_route.os.environ,
                                             {"WORKBUDDY_ANDROID": "0"}))
        self.explicit_proxy = self.stack.enter_context(
            patch.object(upstream_route, "_explicit_proxy", return_value=False))
        self.proxies = self.stack.enter_context(
            patch.object(upstream_route, "getproxies", return_value={}))
        self.addresses = self.stack.enter_context(
            patch.object(upstream_route.network_adapt, "local_addresses",
                         return_value=["198.18.0.99"]))
        self.resolve = self.stack.enter_context(
            patch.object(upstream_route.network_adapt, "resolver_rules",
                         return_value=("unused", {HOST: {"override": "203.0.113.7"}})))


class RouteTests(RoutingFixture, unittest.TestCase):
    def test_only_destination_changes_with_host_sni_and_body_preserved(self):
        request = httpx.Request("POST", URL + "?keep=1",
                                headers={"Authorization": "Bearer synthetic"},
                                json={"messages": [{"role": "user", "content": "synthetic"}]})
        original_body = request.content
        upstream_route._route(request, HOST)
        self.assertEqual(str(request.url), "https://203.0.113.7/v2/chat/completions?keep=1")
        self.assertEqual(request.headers["host"], HOST)
        self.assertEqual(request.extensions["sni_hostname"], HOST)
        self.assertEqual(request.headers["authorization"], "Bearer synthetic")
        self.assertEqual(request.content, original_body)
        self.resolve.assert_called_once_with([HOST])

    def test_healthy_dns_does_not_probe_or_change_request(self):
        for addresses in (["203.0.113.4"], ["198.18.0.99", "203.0.113.4"], []):
            with self.subTest(addresses=addresses):
                self.addresses.return_value = addresses
                request = httpx.Request("GET", URL)
                upstream_route._route(request, HOST)
                self.assertEqual(str(request.url), URL)
                self.assertNotIn("sni_hostname", request.extensions)
                self.resolve.assert_not_called()

    def test_explicit_environment_proxy_keeps_original_routing(self):
        self.explicit_proxy.return_value = True
        for proxies in ({"https": "http://synthetic:1080"}, {"all": "socks5://synthetic:1080"}):
            with self.subTest(proxies=proxies):
                self.proxies.return_value = proxies
                request = httpx.Request("GET", URL)
                upstream_route._route(request, HOST)
                self.assertEqual(str(request.url), URL)
                self.addresses.assert_not_called()

    def test_unrelated_hosts_http_custom_ports_and_existing_sni_are_untouched(self):
        for url, extensions in (
            ("https://other.invalid/v1", {}),
            ("http://" + HOST + "/v2", {}),
            (BACKEND + ":8443/v2", {}),
            (URL, {"sni_hostname": "existing"}),
        ):
            with self.subTest(url=url, extensions=extensions):
                request = httpx.Request("GET", url, extensions=extensions)
                upstream_route._route(request, HOST)
                self.assertEqual(str(request.url), url)
                self.addresses.assert_not_called()

    def test_android_keeps_existing_transport_behavior(self):
        with patch.dict(upstream_route.os.environ, {"WORKBUDDY_ANDROID": "1"}):
            request = httpx.Request("GET", URL)
            upstream_route._route(request, HOST)
        self.assertEqual(str(request.url), URL)
        self.addresses.assert_not_called()

    def test_no_verified_address_is_not_a_success_shaped_fallback(self):
        self.resolve.return_value = (None, {HOST: {"override": None}})
        request = httpx.Request("GET", URL)
        upstream_route._route(request, HOST)
        self.assertEqual(str(request.url), URL)
        self.assertNotIn("sni_hostname", request.extensions)

    def test_sync_client_preserves_hooks_options_and_sends_post_once(self):
        seen = []
        hook = Mock()
        hooks = {"request": [hook]}
        def handle(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True})
        with upstream_route.client(BACKEND, timeout=7, event_hooks=hooks,
                                   transport=httpx.MockTransport(handle)) as client:
            response = client.post(URL, json={"keep": True})
            self.assertEqual(client.timeout.read, 7)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "203.0.113.7")
        hook.assert_called_once()
        self.assertEqual(hooks, {"request": [hook]})

    def test_disconnect_is_propagated_without_post_replay(self):
        seen = []
        def handle(request):
            seen.append(request)
            raise httpx.RemoteProtocolError("synthetic disconnect", request=request)
        with upstream_route.client(BACKEND, transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(httpx.RemoteProtocolError):
                client.post(URL, json={"keep": True})
        self.assertEqual(len(seen), 1)

    def test_explicit_proxy_and_tls_options_are_preserved(self):
        with patch.object(upstream_route.httpx, "Client") as factory:
            upstream_route.client(BACKEND, proxy="http://synthetic:1080", verify=True)
        self.assertEqual(factory.call_args.kwargs,
                         {"event_hooks": {}, "proxy": "http://synthetic:1080", "verify": True})

    def test_trust_env_false_does_not_consult_environment_proxy(self):
        request = httpx.Request("GET", URL)
        upstream_route._route(request, HOST, trust_env=False)
        self.proxies.assert_not_called()
        self.assertEqual(request.url.host, "203.0.113.7")

    def test_system_proxy_bypass_is_scoped_to_verified_ip(self):
        self.proxies.return_value = {"https": "http://127.0.0.1:1082"}
        with patch.object(upstream_route.httpx, "HTTPTransport") as transport:
            options = upstream_route._options(BACKEND, {"verify": True})
        self.assertEqual(set(options["mounts"]), {"https://203.0.113.7"})
        transport.assert_called_once_with(verify=True)
        self.assertNotIn("trust_env", options)

    def test_system_proxy_kept_when_dns_is_healthy_or_explicit_proxy_is_set(self):
        self.proxies.return_value = {"https": "http://127.0.0.1:1082"}
        self.addresses.return_value = ["203.0.113.9"]
        self.assertNotIn("mounts", upstream_route._options(BACKEND, {}))
        self.explicit_proxy.return_value = True
        self.addresses.return_value = ["198.18.0.99"]
        self.assertNotIn("mounts", upstream_route._options(BACKEND, {}))


class AsyncRouteTests(RoutingFixture, unittest.IsolatedAsyncioTestCase):
    async def test_async_transport_is_routed_once(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(200, text="synthetic")
        async with upstream_route.async_client(
                BACKEND, transport=httpx.MockTransport(handle)) as client:
            response = await client.post(URL, json={"keep": True})
        self.assertEqual(response.text, "synthetic")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].headers["host"], HOST)
        self.assertEqual(seen[0].extensions["sni_hostname"], HOST)

    async def test_auto_alias_and_stream_are_preserved_through_converter(self):
        seen = []
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "model": "auto",
                 "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}
        sse = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
        def handle(request):
            seen.append(request)
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        real_async_client = upstream_route.async_client
        def outbound(backend_url, **options):
            return real_async_client(backend_url, transport=httpx.MockTransport(handle), **options)
        cred = Mock()
        cred.get_headers.return_value = {"Authorization": "Bearer synthetic"}
        with patch.dict(converter.CONFIG, {"api_key": "synthetic", "cred": cred,
                                           "desensitize": False, "log_path": None}), \
                patch.object(converter, "_supported_efforts_for", return_value=None), \
                patch.object(converter.upstream_route, "async_client", side_effect=outbound):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=converter.app),
                                         base_url="http://test") as client:
                response = await client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer synthetic"},
                    json={"model": "Workbuddy-Auto", "stream": True,
                          "messages": [{"role": "user", "content": "Reply OK"}]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("[DONE]", response.text)
        self.assertIn('"content": "OK"', response.text)
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(seen[0].content)["model"], "auto")
        self.assertEqual(seen[0].headers["host"], HOST)
        self.assertEqual(seen[0].url.host, "203.0.113.7")


if __name__ == "__main__":
    unittest.main()
