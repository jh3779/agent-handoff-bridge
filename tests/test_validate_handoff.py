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
from pathlib import Path, PureWindowsPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validate_handoff as vh  # noqa: E402
import handoff_bridge as hb  # noqa: E402


class InstallFilesCoverEveryRequiredFileTests(unittest.TestCase):
    """Regression (real-world reproduction, 2026-09-04): REQUIRED_FILES has
    listed a top-level `.gitignore` since before this file existed, but
    `handoff_bridge.py`'s INSTALL_FILES (what `install`/`init` actually
    copies into a workspace) never included one -- so `handoff_bridge.py
    check` failed immediately and deterministically in *every* freshly
    installed/initialized workspace, including every workspace the webui
    auto-creates. `scripts/package_platforms.py`'s COMMON_FILES already
    had it; only INSTALL_FILES was missing it -- the two file lists this
    project ships from had quietly drifted apart. This test compares
    REQUIRED_FILES/JSON_FILES/PYTHON_FILES against INSTALL_FILES directly
    so any *future* file added to one list but not the other fails CI
    immediately, instead of only surfacing when a real agent stumbles into
    a freshly created workspace and "fixes" it by hand."""

    def test_every_validator_required_file_is_actually_installed(self):
        installed_targets = {target for _source, target in hb.INSTALL_FILES}
        # .handoff/current.md is a deliberate exception: install_standard_files()
        # generates it dynamically (a starter template written only if it
        # doesn't already exist) rather than copying a static file, so it
        # has no INSTALL_FILES entry by design -- not the drift this test
        # otherwise guards against.
        required = (set(vh.REQUIRED_FILES) | set(vh.JSON_FILES) | set(vh.PYTHON_FILES)) - {".handoff/current.md"}
        missing = required - installed_targets
        self.assertEqual(
            missing,
            set(),
            msg=f"validate_handoff.py requires these files but INSTALL_FILES never installs them: {missing}",
        )


class CheckSecretsCommandTests(unittest.TestCase):
    def test_unfrozen_shells_out_to_sys_executable_and_the_script(self):
        with mock.patch.object(vh.sys, "frozen", False, create=True), mock.patch(
            "validate_handoff.subprocess.run"
        ) as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], vh.sys.executable)
        self.assertTrue(command[1].endswith(str(Path("scripts") / "scan_secrets.py")))

    def test_frozen_uses_a_sibling_scan_sidecar_next_to_sys_executable(self):
        with mock.patch.object(vh.sys, "frozen", True, create=True), mock.patch.object(
            vh.sys, "executable", "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-validate"
        ), mock.patch.object(vh.sys, "platform", "darwin"), mock.patch("validate_handoff.subprocess.run") as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-scan")

    def test_frozen_on_windows_uses_the_exe_suffix(self):
        # check_secrets() builds this via PureWindowsPath (not the
        # host-native Path) when sys.platform is "win32", so the result is
        # genuinely backslash-style regardless of which OS runs this test --
        # expected value constructed the same way rather than hand-typed, so
        # it can't drift from what PureWindowsPath actually produces.
        with mock.patch.object(vh.sys, "frozen", True, create=True), mock.patch.object(
            vh.sys, "executable", "/apps/agent-handoff-bridge/agent-handoff-bridge-validate.exe"
        ), mock.patch.object(vh.sys, "platform", "win32"), mock.patch("validate_handoff.subprocess.run") as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        command = run_spy.call_args.args[0]
        expected = PureWindowsPath("/apps/agent-handoff-bridge") / "agent-handoff-bridge-scan.exe"
        self.assertEqual(command[0], str(expected))

    def test_a_real_failure_is_still_reported(self):
        with mock.patch.object(vh.sys, "frozen", False, create=True), mock.patch(
            "validate_handoff.subprocess.run"
        ) as run_spy, mock.patch("validate_handoff.fail") as fail_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="found a secret\n", stderr="")
            vh.check_secrets(Path("/some/root"))
        fail_spy.assert_called_once()
        self.assertIn("found a secret", fail_spy.call_args.args[0])

    def test_pins_utf8_encoding(self):
        # Regression coverage for a real crash class (2026-08-14, see
        # handoff_bridge.py's run_provider() fix): without an explicit
        # encoding, subprocess.run() falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine -- to decode scan_secrets.py's own output.
        with mock.patch.object(vh.sys, "frozen", False, create=True), mock.patch(
            "validate_handoff.subprocess.run"
        ) as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            vh.check_secrets(Path("/some/root"))
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")


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
