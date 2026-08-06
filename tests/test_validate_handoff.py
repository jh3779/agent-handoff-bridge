#!/usr/bin/env python3
"""Unit tests for scripts/validate_handoff.py.

Covers check_secrets()'s subprocess command construction only -- the rest
of this script is already exercised indirectly via `handoff_bridge.py check`
in CI (docs/quality-gates.md). Phase 7a (DEC-22,
docs/research-phase7-framework.md) added frozen-mode detection here: when
this script is itself running frozen (PyInstaller, as the Tauri sidecar
agent-handoff-bridge-validate), sys.executable is that binary, not a
Python interpreter, so it can't be told to "run" scan_secrets.py's path --
a sibling PyInstaller sidecar (agent-handoff-bridge-scan) is invoked
directly instead in that case.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validate_handoff as vh  # noqa: E402


class CheckSecretsCommandTests(unittest.TestCase):
    def test_unfrozen_shells_out_to_sys_executable_and_the_script(self):
        with mock.patch.object(vh.sys, "frozen", False, create=True), mock.patch(
            "validate_handoff.subprocess.run"
        ) as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], vh.sys.executable)
        self.assertTrue(command[1].endswith("scripts/scan_secrets.py"))

    def test_frozen_uses_a_sibling_scan_sidecar_next_to_sys_executable(self):
        with mock.patch.object(vh.sys, "frozen", True, create=True), mock.patch.object(
            vh.sys, "executable", "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-validate"
        ), mock.patch.object(vh.sys, "platform", "darwin"), mock.patch("validate_handoff.subprocess.run") as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-scan")

    def test_frozen_on_windows_uses_the_exe_suffix(self):
        with mock.patch.object(vh.sys, "frozen", True, create=True), mock.patch.object(
            vh.sys, "executable", "/apps/agent-handoff-bridge/agent-handoff-bridge-validate.exe"
        ), mock.patch.object(vh.sys, "platform", "win32"), mock.patch("validate_handoff.subprocess.run") as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], "/apps/agent-handoff-bridge/agent-handoff-bridge-scan.exe")

    def test_a_real_failure_is_still_reported(self):
        with mock.patch.object(vh.sys, "frozen", False, create=True), mock.patch(
            "validate_handoff.subprocess.run"
        ) as run_spy, mock.patch("validate_handoff.fail") as fail_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="found a secret\n", stderr="")
            vh.check_secrets(Path("/some/root"))
        fail_spy.assert_called_once()
        self.assertIn("found a secret", fail_spy.call_args.args[0])


class CheckTestsFrozenSkipTests(unittest.TestCase):
    """check_tests() re-running this project's own dev unittest suite
    doesn't transfer to a frozen context (see check_tests()'s own
    docstring for why) -- it must skip cleanly there instead of trying
    and hitting the same sys.executable assumption the frozen sidecars
    themselves had to work around elsewhere."""

    def test_frozen_skips_without_running_unittest_discover(self):
        with mock.patch.object(vh.sys, "frozen", True, create=True), mock.patch.object(
            vh.unittest, "TestLoader"
        ) as loader_spy:
            vh.check_tests(Path("/some/root"))
        loader_spy.assert_not_called()

    def test_unfrozen_still_runs_discovery(self):
        with mock.patch.object(vh.sys, "frozen", False, create=True), mock.patch.object(
            vh.unittest, "TestLoader"
        ) as loader_spy:
            fake_suite = mock.Mock()
            fake_suite.countTestCases.return_value = 1
            loader_spy.return_value.discover.return_value = fake_suite
            with mock.patch.object(vh.unittest, "TextTestRunner") as runner_spy:
                runner_spy.return_value.run.return_value = mock.Mock(wasSuccessful=lambda: True)
                vh.check_tests(Path(__file__).resolve().parent.parent)
        loader_spy.return_value.discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
