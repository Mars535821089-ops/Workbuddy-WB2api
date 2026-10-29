"""Offline launch regressions: no desktop process, account or network access."""
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from admin import client_follow, client_session


class LaunchClientTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(client_session, "IS_MAC", True))
        self.stack.enter_context(patch.object(client_session, "IS_WIN", False))
        self.stack.enter_context(patch.object(client_session.os, "getuid", return_value=501,
                                             create=True))
        self.stack.enter_context(
            patch.object(client_follow, "history_db_path", return_value="unused.db"))
        self.recover = self.stack.enter_context(patch.object(client_follow, "recover_pending"))
        self.run = self.stack.enter_context(
            patch.object(client_session.subprocess, "run", return_value=Mock(returncode=0)))
        self.popen = self.stack.enter_context(patch.object(client_session.subprocess, "Popen"))
        self.wait = self.stack.enter_context(
            patch.object(client_session, "wait_running", return_value=True))
        self.path = "/Applications/WorkBuddy CN.app"
        self.argv = ["/bin/launchctl", "asuser", "501", "/usr/bin/open", "-a", self.path]

    def test_mac_launch_uses_gui_domain_and_waits(self):
        result = client_session.launch_client(self.path, timeout=7)
        self.run.assert_called_once_with(self.argv, capture_output=True, text=True, timeout=15)
        self.popen.assert_not_called()
        self.recover.assert_called_once_with("unused.db")
        self.wait.assert_called_once_with(self.path, timeout=7)
        self.assertEqual(result, {"launched": True, "app_path": self.path, "running": True,
                                  "dns_overrides": None, "chromium_args": None})

    def test_mac_preserves_extra_arguments_without_shell_interpretation(self):
        extra = ["--host-resolver-rules=MAP example.test 127.0.0.1",
                 "--literal=$(touch should-not-exist)", "--value=a;b"]
        detail = {"adapted": True}
        with patch("admin.network_adapt.electron_args", return_value=(extra, detail)):
            result = client_session.launch_client(self.path, adapt_dns=True)
        self.run.assert_called_once_with(self.argv + ["--args"] + extra,
                                         capture_output=True, text=True, timeout=15)
        self.popen.assert_not_called()
        self.assertEqual(result["chromium_args"], extra)
        self.assertEqual(result["dns_overrides"], detail)

    def test_without_dns_adaptation_does_not_probe_network(self):
        with patch("admin.network_adapt.electron_args") as args:
            client_session.launch_client(self.path, adapt_dns=False)
        args.assert_not_called()

    def test_dns_probe_failure_still_launches_without_extra_arguments(self):
        with patch("admin.network_adapt.electron_args", side_effect=OSError("probe failed")):
            result = client_session.launch_client(self.path, adapt_dns=True)
        self.assertTrue(result["launched"])
        self.assertEqual(result["dns_overrides"], {"error": "OSError: probe failed"})
        self.assertEqual(self.run.call_args.args[0], self.argv)

    def test_mac_failure_never_falls_back_to_direct_binary(self):
        for failure in (Mock(returncode=1), OSError("no GUI session"),
                        subprocess.TimeoutExpired(self.argv, 15)):
            with self.subTest(failure=failure):
                self.run.reset_mock()
                self.wait.reset_mock()
                self.run.side_effect = failure if isinstance(failure, Exception) else None
                self.run.return_value = failure
                result = client_session.launch_client(self.path)
                self.assertFalse(result["launched"])
                self.assertFalse(result["running"])
                self.wait.assert_not_called()
                self.popen.assert_not_called()
                self.run.assert_called_once()

    def test_mac_wait_timeout_does_not_claim_running(self):
        self.wait.return_value = False
        result = client_session.launch_client(self.path, timeout=3)
        self.assertTrue(result["launched"])
        self.assertFalse(result["running"])
        self.wait.assert_called_once_with(self.path, timeout=3)

    def test_mac_no_wait_retains_existing_return_behavior(self):
        result = client_session.launch_client(self.path, wait=False)
        self.assertTrue(result["launched"])
        self.assertFalse(result["running"])
        self.wait.assert_not_called()

    def test_missing_installation_does_not_launch(self):
        with patch.object(client_session, "app_install_path", return_value=None):
            result = client_session.launch_client()
        self.assertFalse(result["launched"])
        self.recover.assert_not_called()
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_pending_recovery_failure_prevents_launch(self):
        self.recover.side_effect = client_follow.FollowError("pending")
        result = client_session.launch_client(self.path)
        self.assertFalse(result["launched"])
        self.assertFalse(result["running"])
        self.run.assert_not_called()
        self.popen.assert_not_called()
        self.wait.assert_not_called()

    def test_windows_and_linux_keep_direct_launch_and_arguments(self):
        extra = ["--host-resolver-rules=MAP example.test 127.0.0.1"]
        for is_win in (True, False):
            with self.subTest(is_win=is_win), \
                    patch.object(client_session, "IS_MAC", False), \
                    patch.object(client_session, "IS_WIN", is_win), \
                    patch.object(client_session.shutil, "which", return_value="/client"), \
                    patch("admin.network_adapt.electron_args", return_value=(extra, {})):
                self.popen.reset_mock()
                self.wait.reset_mock()
                result = client_session.launch_client("/client", adapt_dns=True)
                self.assertTrue(result["launched"])
                self.assertTrue(result["running"])
                self.popen.assert_called_once_with(["/client"] + extra, close_fds=True)
                self.wait.assert_called_once_with("/client", timeout=20.0)
                self.run.assert_not_called()

    def test_linux_missing_executable_does_not_launch(self):
        with patch.object(client_session, "IS_MAC", False), \
                patch.object(client_session.shutil, "which", return_value=None):
            result = client_session.launch_client("/missing")
        self.assertFalse(result["launched"])
        self.popen.assert_not_called()
        self.wait.assert_not_called()


class WaitRunningTests(unittest.TestCase):
    def test_polls_until_client_appears(self):
        with patch.object(client_session.time, "time", side_effect=[0, 0, 1]), \
                patch.object(client_session.time, "sleep") as sleep, \
                patch.object(client_session, "client_pids", side_effect=[[], [123]]):
            self.assertTrue(client_session.wait_running("/client", timeout=2))
        sleep.assert_called_once_with(0.5)

    def test_timeout_checks_client_one_last_time(self):
        for pids in ([], [123]):
            with self.subTest(pids=pids), \
                    patch.object(client_session.time, "time", side_effect=[0, 2]), \
                    patch.object(client_session.time, "sleep") as sleep, \
                    patch.object(client_session, "client_pids", return_value=pids) as detect:
                self.assertEqual(client_session.wait_running("/client", timeout=1), bool(pids))
            sleep.assert_not_called()
            detect.assert_called_once_with("/client")


if __name__ == "__main__":
    unittest.main()
