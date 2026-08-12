#!/usr/bin/env python3
"""Unit tests for the highest-risk pure logic in handoff_bridge.py.

Run with: python3 -m unittest discover -s tests -v

This is the minimum coverage bar described in docs/quality-gates.md: the
provider fallback/classification logic and the shared-state write path must
have tests, because they have no other safety net (no CI provider calls, no
manual QA step that would catch a silent regression).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge as hb  # noqa: E402


class ClassifyHandoffTests(unittest.TestCase):
    def test_success_needs_no_handoff(self):
        needed, reason = hb.classify_handoff(0, "all good", "", {})
        self.assertFalse(needed)
        self.assertTrue(reason.startswith("none:"))

    def test_rate_limit_signal_in_stdout(self):
        needed, reason = hb.classify_handoff(0, "Error: 429 too many requests", "", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("rate_limit:"))

    def test_quota_signal_in_stderr(self):
        needed, reason = hb.classify_handoff(1, "", "insufficient quota for this request", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("quota:"))

    def test_auth_signal(self):
        needed, reason = hb.classify_handoff(1, "", "authentication_failed: token expired", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("auth:"))

    def test_successful_response_merely_mentioning_auth_method_is_not_misclassified(self):
        # Regression (found in review of the "auth method" pattern added
        # for the real Gemini auth-failure signal): that pattern is also
        # checked against a *successful* run's raw combined stdout+stderr
        # in classify_handoff()'s second, unconditional loop -- a bare
        # "auth method" match wrongly fired on a genuinely successful
        # response that merely discusses auth methods in prose, discarding
        # a good answer (and, with --auto-fallback, re-running the prompt
        # on a different provider for no reason). The pattern must match
        # Gemini's distinctive imperative error wording ("set an auth
        # method"), not just any mention of the phrase.
        stdout = json.dumps({"response": "You can configure the auth method in your settings.json file."})
        parsed = hb.summarize_gemini(stdout, "", exit_code=0)
        needed, reason = hb.classify_handoff(0, stdout, "", parsed)
        self.assertFalse(needed, msg=reason)

    def test_gemini_autherror_is_classified_as_auth_not_unknown(self):
        # Regression (found in review): summarize_gemini()'s error dict
        # for an auth failure looks like {"type": "AuthError", "message":
        # "not authenticated"} -- neither "AuthError" nor "not
        # authenticated" matched the old auth pattern
        # (not logged in|authentication_failed|unauthorized|forbidden),
        # so this fell all the way through to "unknown" even though
        # AuthError is a documented Gemini signal
        # (docs/research-gemini-cli.md).
        parsed = hb.summarize_gemini(
            "",
            json.dumps({"response": "", "error": {"type": "AuthError", "message": "not authenticated"}}),
            exit_code=41,
        )
        needed, reason = hb.classify_handoff(41, "", "", parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("auth:"), msg=reason)

    def test_gemini_real_cli_autherror_shape_is_classified_as_auth(self):
        # Regression (found via real verification against an actually
        # installed gemini binary, v0.54.0, 2026-08-06, not a mock): the
        # unauthenticated-CLI failure writes its JSON error object to
        # *stderr*, not stdout, and error.type comes back as the generic
        # "Error" rather than "AuthError"/"FatalAuthenticationError" --
        # so this real shape must still classify as "auth" through the
        # message-text match, not the type-name match, and only after
        # summarize_gemini() is given stderr to fall back to.
        stderr = json.dumps(
            {
                "session_id": "eab3f432-f14a-431d-b976-7ffa1a3b0e1a",
                "error": {
                    "type": "Error",
                    "message": (
                        "Please set an Auth method in your /Users/x/.gemini/settings.json "
                        "or specify one of the following environment variables before "
                        "running: GEMINI_API_KEY, GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_GENAI_USE_GCA"
                    ),
                    "code": 41,
                },
            }
        )
        parsed = hb.summarize_gemini("", stderr, exit_code=41)
        self.assertEqual(len(parsed["errors"]), 1)
        needed, reason = hb.classify_handoff(41, "", stderr, parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("auth:"), msg=reason)

    def test_successful_response_merely_quoting_tool_failure_text_is_not_misclassified(self):
        # Regression (full-project review, 2026-08-07): the false-positive
        # class fixed above for `auth` specifically (narrowing its pattern)
        # was never fixed for the other ERROR_PATTERNS labels -- a
        # genuinely successful run (exit_code 0, no structured `errors`)
        # whose own answer text quotes a phrase like "command not found"
        # (e.g. summarizing a bug it just fixed) was still wrongly
        # classified as tool_failure by the second loop, which scanned the
        # raw combined stdout+stderr including the model's own answer text.
        # Fixed generally by cutting parsed["final_text"] out of the text
        # that loop scans, instead of re-narrowing each pattern one at a
        # time -- a plain exit_code != 0 gate was tried first but rejected:
        # test_rate_limit_signal_in_stdout (below) intentionally exercises a
        # real case where exit_code is 0 but a genuine plain-text signal
        # outside the answer text must still be caught.
        stdout = json.dumps(
            {"response": "Fixed it: the script previously failed with command not found."}
        )
        parsed = hb.summarize_gemini(stdout, "", exit_code=0)
        needed, reason = hb.classify_handoff(0, stdout, "", parsed)
        self.assertFalse(needed, msg=reason)

    def test_tool_failure_signal_by_pattern(self):
        needed, reason = hb.classify_handoff(1, "", "bash: codex: command not found", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("tool_failure:"))

    def test_tool_failure_from_exit_code_127(self):
        needed, reason = hb.classify_handoff(127, "", "", {})
        self.assertTrue(needed)
        self.assertEqual(reason, "tool_failure: provider command not found")

    def test_unmatched_nonzero_exit_is_tool_failure(self):
        needed, reason = hb.classify_handoff(2, "no idea what happened", "", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("tool_failure:"))

    def test_machine_readable_error_without_known_signal_is_unknown(self):
        needed, reason = hb.classify_handoff(0, "", "", {"errors": [{"type": "error", "message": "boom"}]})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("unknown:"))

    def test_machine_readable_error_with_known_signal(self):
        needed, reason = hb.classify_handoff(
            0, "", "", {"errors": [{"type": "error", "message": "server overloaded"}]}
        )
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("overloaded:"))

    def test_reason_label_always_in_handoff_labels_or_none(self):
        cases = [
            (0, "", "", {}),
            (1, "429", "", {}),
            (1, "", "quota exceeded", {}),
            (127, "", "", {}),
            (3, "", "", {}),
        ]
        for exit_code, stdout, stderr, parsed in cases:
            _, reason = hb.classify_handoff(exit_code, stdout, stderr, parsed)
            label = reason.split(":", 1)[0]
            self.assertIn(label, hb.HANDOFF_LABELS + ("none",))


class ChooseAutoProviderTests(unittest.TestCase):
    # choose_auto_provider()'s handoff_needed branch now goes through
    # next_available_provider() (review fix), which calls shutil.which()
    # for every provider -- these three tests assert a specific *ordering*
    # outcome that must hold regardless of what's actually installed on
    # whatever machine runs the suite, so shutil.which() is pinned to
    # "everything is installed" rather than left to the real environment.
    def test_handoff_needed_switches_to_other_provider(self):
        state = {"status": "handoff_needed", "last_provider": "codex", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.choose_auto_provider(state), "claude")

    def test_handoff_needed_from_claude_switches_to_gemini(self):
        # Phase 5: PROVIDERS is now ("codex", "claude", "gemini") -- N-way
        # fallback walks to the *next* provider in that order, not back to
        # the start. "the other one" stopped being well-defined once a
        # third provider existed (docs/provider-extensibility.md).
        state = {"status": "handoff_needed", "last_provider": "claude", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.choose_auto_provider(state), "gemini")

    def test_handoff_needed_from_gemini_wraps_around_to_codex(self):
        state = {"status": "handoff_needed", "last_provider": "gemini", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.choose_auto_provider(state), "codex")

    def test_handoff_needed_skips_an_uninstalled_provider_in_between(self):
        # The exact scenario a review flagged: codex fails, claude isn't
        # installed, gemini is -- the single-hop fallback must still reach
        # the installed gemini instead of naively landing on claude and
        # stopping there.
        state = {"status": "handoff_needed", "last_provider": "codex", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name in ("codex", "gemini") and f"/usr/bin/{name}"):
            self.assertEqual(hb.choose_auto_provider(state), "gemini")

    def test_prefers_primary_when_available(self):
        state = {"status": "ready", "primary_provider": "claude"}
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "claude" and "/usr/bin/claude"):
            self.assertEqual(hb.choose_auto_provider(state), "claude")

    def test_falls_back_to_any_available_provider(self):
        state = {"status": "ready", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "claude" and "/usr/bin/claude"):
            self.assertEqual(hb.choose_auto_provider(state), "claude")

    def test_falls_back_to_primary_when_nothing_is_installed(self):
        state = {"status": "ready", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value=None):
            self.assertEqual(hb.choose_auto_provider(state), "codex")


class ModelOverrideArgTests(unittest.TestCase):
    def test_recording_labels_are_not_passed_through(self):
        for label in ("app-selected default", "provider default", "default", "unknown", "  UNKNOWN  "):
            self.assertIsNone(hb.model_override_arg(label))

    def test_none_and_empty_are_not_passed_through(self):
        self.assertIsNone(hb.model_override_arg(None))
        self.assertIsNone(hb.model_override_arg(""))

    def test_exact_model_id_is_passed_through(self):
        self.assertEqual(hb.model_override_arg("claude-sonnet-5"), "claude-sonnet-5")


class AtomicWriteTests(unittest.TestCase):
    def test_write_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "file.txt"
            hb.atomic_write_text(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")

    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            hb.atomic_write_text(target, "content")
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "file.txt"]
            self.assertEqual(leftovers, [])

    def test_overwrite_replaces_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            hb.atomic_write_text(target, "first")
            hb.atomic_write_text(target, "second")
            self.assertEqual(target.read_text(encoding="utf-8"), "second")


class VersionTests(unittest.TestCase):
    def test_cli_version_flag_reports_bridge_version(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "handoff_bridge.py"), "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(hb.BRIDGE_VERSION, result.stdout)


class InstructionTypeArgparseTests(unittest.TestCase):
    """Regression coverage: `--instruction-type` previously had no
    `choices=` restriction on either subcommand, so an arbitrary/typo'd
    value was silently accepted and written straight into the shared
    .handoff/current.md/state.json -- `--primary`/`provider` were already
    correctly validated this way; `--instruction-type` was the one gap."""

    def _run_cli(self, *args: str, workspace: Path) -> subprocess.CompletedProcess:
        bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
        return subprocess.run(
            [sys.executable, str(bridge_script), "--workspace", str(workspace), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_rejects_an_unrecognized_instruction_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_cli("init", "a task", "--instruction-type", "totally-bogus", workspace=Path(tmp))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid choice", result.stderr)
            self.assertFalse((Path(tmp) / ".handoff" / "current.md").exists())

    def test_init_accepts_every_documented_instruction_type(self):
        for instruction_type in hb.INSTRUCTION_TYPES:
            with tempfile.TemporaryDirectory() as tmp:
                result = self._run_cli("init", "a task", "--instruction-type", instruction_type, workspace=Path(tmp))
                self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_run_preview_rejects_an_unrecognized_instruction_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            init_result = self._run_cli("init", "a task", workspace=workspace)
            self.assertEqual(init_result.returncode, 0, msg=init_result.stderr)
            result = self._run_cli("run", "codex", "--instruction-type", "totally-bogus", workspace=workspace)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid choice", result.stderr)


class CheckCommandTests(unittest.TestCase):
    """check()'s subprocess command construction -- Phase 7a (DEC-22):
    when frozen (PyInstaller, as the Tauri sidecar
    agent-handoff-bridge-cli), sys.executable is this binary itself, not
    a Python interpreter, so `[sys.executable, validate_handoff.py]`
    wouldn't run that script. A sibling PyInstaller sidecar built from
    validate_handoff.py is invoked directly instead in that case."""

    def test_unfrozen_shells_out_to_sys_executable_and_the_script(self):
        with mock.patch.object(hb.sys, "frozen", False, create=True), mock.patch(
            "handoff_bridge.subprocess.run"
        ) as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            hb.check(mock.Mock())
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], hb.sys.executable)
        self.assertTrue(command[1].endswith(str(Path("scripts") / "validate_handoff.py")))

    def test_frozen_uses_a_sibling_validate_sidecar_next_to_sys_executable(self):
        with mock.patch.object(hb.sys, "frozen", True, create=True), mock.patch.object(
            hb.sys, "executable", "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-cli"
        ), mock.patch.object(hb.sys, "platform", "darwin"), mock.patch("handoff_bridge.subprocess.run") as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            hb.check(mock.Mock())
        command = run_spy.call_args.args[0]
        self.assertEqual(command[0], "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-validate")

    def test_frozen_on_windows_uses_the_exe_suffix(self):
        # check() now builds this via PureWindowsPath (not the host-native
        # Path) when sys.platform is "win32", so the result is genuinely
        # backslash-style regardless of which OS runs this test -- expected
        # value constructed the same way rather than hand-typed, so it can't
        # drift from what PureWindowsPath actually produces.
        with mock.patch.object(hb.sys, "frozen", True, create=True), mock.patch.object(
            hb.sys, "executable", "/apps/agent-handoff-bridge/agent-handoff-bridge-cli.exe"
        ), mock.patch.object(hb.sys, "platform", "win32"), mock.patch("handoff_bridge.subprocess.run") as run_spy:
            run_spy.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            hb.check(mock.Mock())
        command = run_spy.call_args.args[0]
        expected = PureWindowsPath("/apps/agent-handoff-bridge") / "agent-handoff-bridge-validate.exe"
        self.assertEqual(command[0], str(expected))


class WriteLockTests(unittest.TestCase):
    def test_lock_is_released_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".write.lock"
            with hb.WriteLock(lock_path, timeout=1):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

    def test_second_lock_times_out_while_first_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".write.lock"
            with hb.WriteLock(lock_path, timeout=1):
                with self.assertRaises(TimeoutError):
                    with hb.WriteLock(lock_path, timeout=0.2):
                        pass  # pragma: no cover - should never acquire


class RunCommandLockTests(unittest.TestCase):
    """run_command() (the `run` subcommand's handler) serializes concurrent
    invocations against the same workspace via RUN_LOCK_FILE, closing a
    lost-update race on state.json that two overlapping remote-server tasks
    on the same workspace could otherwise hit (load_state()/save_state()
    themselves are not locked across the whole read-modify-write cycle)."""

    def test_run_command_fails_fast_instead_of_racing_when_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".handoff").mkdir()
            lock_path = workspace / ".handoff" / ".run.lock"
            lock_path.touch()  # simulate another `run` already holding it

            original_cwd = Path.cwd()
            os.chdir(workspace)
            try:
                with mock.patch.object(hb, "RUN_LOCK_TIMEOUT_SECONDS", 0.2), mock.patch.object(
                    hb, "run_provider"
                ) as run_provider_spy:
                    exit_code = hb.run_command(mock.Mock(provider="codex", prompt="hi"))
            finally:
                os.chdir(original_cwd)

            self.assertEqual(exit_code, 75)
            run_provider_spy.assert_not_called()


class DecodeTimeoutOutputTests(unittest.TestCase):
    def test_none_becomes_empty_string(self):
        self.assertEqual(hb.decode_timeout_output(None), "")

    def test_str_passes_through_unchanged(self):
        self.assertEqual(hb.decode_timeout_output("already text"), "already text")

    def test_bytes_are_decoded_to_str(self):
        # CPython's subprocess._communicate() builds TimeoutExpired.stdout/
        # .stderr via b''.join(...) on the timeout path even when the
        # Popen/run() call used text=True -- only the successful-return path
        # decodes to str. A provider that emits partial JSONL right before
        # timing out must not crash the bridge here.
        self.assertEqual(hb.decode_timeout_output(b"partial-json-line\n"), "partial-json-line\n")

    def test_bytes_with_invalid_utf8_do_not_raise(self):
        self.assertEqual(hb.decode_timeout_output(b"\xff\xfe"), "��")


class ShortRunTimeoutTests(unittest.TestCase):
    def test_timeout_with_bytes_partial_output_does_not_raise(self):
        exc = subprocess.TimeoutExpired(cmd=["fake"], timeout=1, output=b"partial stdout", stderr=b"partial stderr")
        with mock.patch.object(hb.subprocess, "run", side_effect=exc):
            exit_code, stdout, stderr = hb.short_run(["fake"])
        self.assertEqual(exit_code, 124)
        self.assertEqual(stdout, "partial stdout")
        self.assertEqual(stderr, "partial stderr")

    def test_timeout_with_no_output_falls_back_to_message(self):
        exc = subprocess.TimeoutExpired(cmd=["fake"], timeout=1, output=None, stderr=None)
        with mock.patch.object(hb.subprocess, "run", side_effect=exc):
            exit_code, stdout, stderr = hb.short_run(["fake"])
        self.assertEqual(exit_code, 124)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "timed out")

    def test_binary_not_found_returns_127_not_a_raised_exception(self):
        # check_for_update()'s CheckForUpdateTests only ever mock short_run
        # itself, so this is the one place the actual FileNotFoundError ->
        # exit 127 translation this whole "gh missing" fallback chain
        # depends on gets exercised directly, with a real, genuinely
        # nonexistent command (not a mocked subprocess.run) -- confirmed
        # for real on a Windows dev machine with no `gh` installed at all
        # (2026-08-12): check_for_update() returned "unavailable" instantly
        # rather than raising or hanging, exactly because of this path.
        exit_code, stdout, stderr = hb.short_run(["definitely-not-a-real-binary-xyz"])
        self.assertEqual(exit_code, 127)
        self.assertEqual(stdout, "")
        self.assertIn("not found", stderr)


class RunProviderTimeoutIntegrationTests(unittest.TestCase):
    """CLI-level regression test for the exact scenario flagged in review:
    a provider that emits partial JSONL and then hangs past
    --timeout-seconds. run_provider()'s TimeoutExpired handler used to pass
    exc.stdout/exc.stderr straight to Path.write_text() -- CPython's
    subprocess._communicate() gives bytes there even under text=True (see
    DecodeTimeoutOutputTests), so a real partial-output timeout would raise
    TypeError before the history record for it was ever saved.
    """

    def setUp(self):
        if os.name != "posix" or not hb.shutil.which("sh"):
            self.skipTest("POSIX shell not available for fake provider scripts")

    def test_partial_jsonl_then_hang_still_saves_a_history_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                'echo \'{"type": "thread.started", "thread_id": "partial-session"}\'\n'
                "sleep 5\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            prompt_path = workspace / "prompt.txt"
            prompt_path.write_text("hello", encoding="utf-8")

            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(bridge_script),
                    "--workspace",
                    str(workspace),
                    "run",
                    "codex",
                    "--execute",
                    "--prompt-file",
                    str(prompt_path),
                    "--timeout-seconds",
                    "1",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=30,
            )

            # run_provider() returns exit_code (124 for a timeout) and
            # main() does sys.exit(main()), so the CLI process itself exits
            # 124 here -- the fix under test is that it exits 124 with a
            # saved history record instead of crashing with an uncaught
            # TypeError from write_text(bytes).
            self.assertEqual(result.returncode, 124, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["history"]), 1)
            self.assertEqual(state["history"][0]["exit_code"], 124)


class AutoFallbackPromptPropagationTests(unittest.TestCase):
    """Regression test: the recursive --auto-fallback call used to replace
    the user's actual prompt with the literal string "Continue after
    provider handoff." -- so a rate-limited codex auto-falling-back into
    claude meant claude never saw what the user actually asked, silently
    undermining the whole point of auto-fallback (and, for the Web UI, the
    attachment content handoff_webui.build_run_prompt() folds into that
    same prompt). Verified end-to-end via a real CLI invocation with fake
    provider scripts, not just a unit test of build_prompt()."""

    def setUp(self):
        if os.name != "posix" or not hb.shutil.which("sh"):
            self.skipTest("POSIX shell not available for fake provider scripts")

    def test_fallback_provider_receives_the_original_user_prompt_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()

            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                "echo 'Error: rate limit exceeded (429)'\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            claude_stdin_capture = workspace / "claude-stdin.txt"
            fake_claude = fake_bin / "claude"
            fake_claude.write_text(
                "#!/bin/sh\n"
                f"cat > {claude_stdin_capture}\n"
                'echo \'{"type": "system", "subtype": "init", "session_id": "s"}\'\n'
                'echo \'{"type": "result", "session_id": "s", "result": "ok", "total_cost_usd": 0.0, "is_error": false}\'\n',
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            distinctive_prompt = "please review the attached distinctive-marker-xyz123.py file"
            prompt_path = workspace / "prompt.txt"
            prompt_path.write_text(distinctive_prompt, encoding="utf-8")

            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(bridge_script),
                    "--workspace",
                    str(workspace),
                    "run",
                    "codex",
                    "--execute",
                    "--auto-fallback",
                    "--prompt-file",
                    str(prompt_path),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["history"]), 2)
            self.assertEqual(state["history"][1]["provider"], "claude")

            self.assertTrue(claude_stdin_capture.exists())
            claude_stdin = claude_stdin_capture.read_text(encoding="utf-8")
            self.assertIn(
                distinctive_prompt,
                claude_stdin,
                msg="fallback provider must receive the user's actual prompt, not a placeholder",
            )


class RunProviderAutoFallbackBuildPromptCountTests(unittest.TestCase):
    """Regression test: run_provider()'s --auto-fallback path used to call
    build_prompt() twice for the same fallback hop -- once just before the
    recursive run_provider(fallback, ...) call purely to write
    NEXT_PROMPT_FILE, and again inside that recursive call itself right
    after state["instruction_type"] is set to "handoff". Nothing reads
    NEXT_PROMPT_FILE synchronously in between, so the first build_prompt()
    call (4 doc reads + a git_snapshot() subprocess pair, and built before
    instruction_type became "handoff") was pure waste and left a stale,
    superseded prompt on disk. build_prompt() must now run at most once per
    provider hop."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        # addCleanup runs LIFO: chdir back to _orig_cwd must be registered
        # *after* (so it runs *before*) _tmp.cleanup() -- deleting a
        # directory while it's still the process's cwd raises
        # PermissionError on Windows (allowed on POSIX, which is why this
        # was invisible until the suite first ran on Windows).
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def test_auto_fallback_calls_build_prompt_at_most_once_per_hop(self):
        def fake_subprocess_run(command, **kwargs):
            if command[0] == "git":
                # build_prompt() -> git_snapshot() shells out to real git
                # (status --short / diff --stat) on every call; this test
                # only cares about codex/claude provider invocations, so
                # give git calls an empty, successful result.
                return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
            if command[0] == "codex":
                return subprocess.CompletedProcess(
                    command, returncode=1, stdout="", stderr="Error: 429 too many requests"
                )
            self.assertEqual(command[0], "claude")
            stdout = (
                '{"type": "system", "subtype": "init", "session_id": "s"}\n'
                '{"type": "result", "session_id": "s", "result": "ok", '
                '"total_cost_usd": 0.0, "is_error": false}\n'
            )
            return subprocess.CompletedProcess(command, returncode=0, stdout=stdout, stderr="")

        args = hb.argparse.Namespace(
            prompt="hello",
            prompt_file=None,
            execute=True,
            auto_fallback=True,
            timeout_seconds=30,
            model=None,
            instruction_type="continue",
        )
        state = {"task": "hello", "primary_provider": "codex", "status": "ready"}

        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
            hb.subprocess, "run", side_effect=fake_subprocess_run
        ), mock.patch.object(hb, "build_prompt", side_effect=hb.build_prompt) as build_prompt_spy:
            exit_code = hb.run_provider("codex", args, state)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(state["history"]), 2)
        self.assertEqual(state["history"][0]["provider"], "codex")
        self.assertEqual(state["history"][1]["provider"], "claude")
        # One call for the failing codex leg, one call for the claude
        # fallback leg that actually runs -- never two for the same hop.
        self.assertEqual(
            build_prompt_spy.call_count,
            2,
            msg="build_prompt() must run at most once per fallback hop, not twice for the same fallback",
        )


class NextProviderTests(unittest.TestCase):
    """Phase 5: next_provider() replaces the old other_provider() binary
    toggle -- docs/provider-extensibility.md's "The Current Code Assumes
    Exactly Two Providers" finding, resolved by walking PROVIDERS in order
    instead of a two-way ternary."""

    def test_walks_to_the_next_provider_in_order(self):
        self.assertEqual(hb.next_provider("codex"), "claude")
        self.assertEqual(hb.next_provider("claude"), "gemini")

    def test_wraps_around_at_the_end(self):
        self.assertEqual(hb.next_provider("gemini"), "codex")

    def test_skips_entries_already_in_tried(self):
        # codex -> claude is next in order, but claude is already tried,
        # so this must skip to gemini instead.
        self.assertEqual(hb.next_provider("codex", tried={"claude"}), "gemini")

    def test_falls_back_to_current_when_every_provider_is_exhausted(self):
        self.assertEqual(hb.next_provider("codex", tried={"codex", "claude", "gemini"}), "codex")

    def test_current_itself_is_always_excluded_even_if_not_in_tried(self):
        # A 2-provider cycle: excluding claude and gemini leaves only
        # codex itself, which must never be returned as its own "next".
        self.assertEqual(hb.next_provider("codex", tried={"claude", "gemini"}), "codex")


class NextAvailableProviderTests(unittest.TestCase):
    """Regression coverage (found in review, real gap only reachable once
    PROVIDERS grew past two entries in Phase 5): a single-hop auto-fallback
    used to pick next_provider() blindly, with no regard for whether that
    candidate's CLI was actually installed -- a codex failure could land on
    an uninstalled claude and never reach an installed gemini sitting right
    after it in PROVIDERS order."""

    def test_skips_an_uninstalled_provider_to_reach_an_installed_one(self):
        # codex fails -> naive next_provider() would say "claude" -- but
        # only codex and gemini are "installed" here, so this must skip
        # past claude to gemini instead.
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name in ("codex", "gemini") and f"/usr/bin/{name}"):
            self.assertEqual(hb.next_available_provider("codex"), "gemini")

    def test_still_respects_tried_on_top_of_availability(self):
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):  # everything "installed"
            self.assertEqual(hb.next_available_provider("codex", tried={"claude"}), "gemini")

    def test_falls_back_to_current_when_nothing_else_is_installed(self):
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "codex" and "/usr/bin/codex"):
            self.assertEqual(hb.next_available_provider("codex"), "codex")

    def test_matches_plain_next_provider_when_everything_is_installed(self):
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.next_available_provider("codex"), hb.next_provider("codex"))


class ProviderCommandGeminiTests(unittest.TestCase):
    def test_first_call_in_a_workspace_has_no_resume_flag(self):
        state = {"sessions": {"gemini": None}}
        command = hb.provider_command("gemini", state)
        self.assertEqual(command[0], "gemini")
        self.assertIn("--output-format", command)
        self.assertIn("json", command)
        self.assertNotIn("--resume", command)

    def test_a_prior_clean_run_adds_resume_latest(self):
        # session_id is always the literal sentinel "latest" for gemini
        # (see summarize_gemini()) -- provider_command() doesn't know or
        # care that it isn't a real ID, only that one was previously set.
        state = {"sessions": {"gemini": "latest"}}
        command = hb.provider_command("gemini", state)
        idx = command.index("--resume")
        self.assertEqual(command[idx + 1], "latest")

    def test_model_is_passed_through(self):
        state = {"sessions": {"gemini": None}}
        command = hb.provider_command("gemini", state, model="gemini-2.5-pro")
        idx = command.index("--model")
        self.assertEqual(command[idx + 1], "gemini-2.5-pro")

    def test_no_inline_prompt_flag_prompt_travels_via_stdin_like_the_others(self):
        # docs/research-gemini-cli.md: piped stdin alone auto-triggers
        # non-interactive mode, matching how codex/claude already receive
        # their prompt via subprocess.run(..., input=prompt), not argv.
        state = {"sessions": {"gemini": None}}
        command = hb.provider_command("gemini", state)
        self.assertNotIn("-p", command)


class SummarizeGeminiTests(unittest.TestCase):
    def test_successful_response_is_parsed(self):
        stdout = json.dumps({"response": "hello back", "stats": {"tokens": {"total": 42}}})
        summary = hb.summarize_gemini(stdout, exit_code=0)
        self.assertEqual(summary["final_text"], "hello back")
        self.assertEqual(summary["usage"], {"tokens": {"total": 42}})
        self.assertEqual(summary["errors"], [])
        # A clean run marks the resume sentinel -- the *only* way
        # provider_command() ever learns "gemini has run here before".
        self.assertEqual(summary["session_id"], "latest")

    def test_nonzero_exit_never_marks_the_resume_sentinel_even_with_a_clean_looking_body(self):
        # Regression (found in review): Gemini's own docs have two
        # overlapping, disagreeing exit-code tables and don't fully
        # document exit-code/JSON-body correlation on failure -- a
        # nonzero exit (e.g. exit 41, FatalAuthenticationError) could in
        # principle still print a `response`/no-`error` body. Checking
        # only the JSON body (ignoring exit_code) would have wrongly
        # marked a failed run as safe to --resume latest on the next
        # call.
        stdout = json.dumps({"response": "partial output before the crash"})
        summary = hb.summarize_gemini(stdout, exit_code=41)
        self.assertIsNone(summary["session_id"])
        # The (misleadingly clean-looking) response text/usage still get
        # surfaced -- only the resume sentinel is suppressed.
        self.assertEqual(summary["final_text"], "partial output before the crash")

    def test_error_field_is_captured_and_session_id_stays_none(self):
        stdout = json.dumps({"response": "", "error": {"type": "AuthError", "message": "not authenticated"}})
        summary = hb.summarize_gemini(stdout)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["type"], "AuthError")
        # An error response must never mark the resume sentinel -- there's
        # nothing confirmed resumable from a failed call.
        self.assertIsNone(summary["session_id"])

    def test_malformed_json_does_not_raise(self):
        summary = hb.summarize_gemini("not json at all")
        self.assertEqual(summary["provider"], "gemini")
        self.assertIsNone(summary["session_id"])
        self.assertEqual(summary["final_text"], "")

    def test_empty_stdout_does_not_raise(self):
        summary = hb.summarize_gemini("")
        self.assertIsNone(summary["session_id"])

    def test_a_json_array_top_level_does_not_raise(self):
        # Valid JSON, but not the expected object shape -- must not crash
        # trying to call .get() on a list.
        summary = hb.summarize_gemini("[1, 2, 3]")
        self.assertIsNone(summary["session_id"])
        self.assertEqual(summary["final_text"], "")

    def test_falls_back_to_stderr_when_stdout_has_nothing_parseable(self):
        # The real CLI (confirmed against v0.54.0) writes fatal-error
        # bodies to stderr, empty stdout -- summarize_gemini() must find
        # the JSON there instead of giving up after an empty/unparseable
        # stdout.
        stderr = json.dumps({"error": {"type": "Error", "message": "boom"}})
        summary = hb.summarize_gemini("", stderr, exit_code=1)
        self.assertEqual(summary["errors"], [{"type": "Error", "message": "boom"}])

    def test_prefers_stdout_over_stderr_when_both_are_present(self):
        # A successful run's real response must never be shadowed by
        # leftover/unrelated stderr text.
        stdout = json.dumps({"response": "real reply"})
        summary = hb.summarize_gemini(stdout, "some unrelated stderr noise", exit_code=0)
        self.assertEqual(summary["final_text"], "real reply")

    def test_falls_back_to_stderr_when_stdout_parses_but_is_not_a_dict(self):
        # Regression (found in review): the original stdout/stderr
        # fallback only tried stderr on a JSONDecodeError from stdout, not
        # when stdout parsed fine but wasn't the expected object shape
        # (e.g. "null", a bare array) -- it returned the empty summary
        # immediately instead of still checking stderr, contradicting this
        # function's own documented stdout-then-stderr fallback contract.
        stderr = json.dumps({"error": {"type": "Error", "message": "boom"}})
        summary = hb.summarize_gemini("null", stderr, exit_code=1)
        self.assertEqual(summary["errors"], [{"type": "Error", "message": "boom"}])


class GeminiIntegrationTests(unittest.TestCase):
    """Real subprocess, fake `gemini` binary -- same pattern as
    RunProviderTimeoutIntegrationTests/AutoFallbackPromptPropagationTests
    above, extended to the third provider."""

    def setUp(self):
        if os.name != "posix" or not hb.shutil.which("sh"):
            self.skipTest("POSIX shell not available for fake provider scripts")

    def test_successful_gemini_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            fake_gemini = fake_bin / "gemini"
            fake_gemini.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                'echo \'{"response": "fake gemini reply", "stats": {"tokens": {"total": 7}}}\'\n',
                encoding="utf-8",
            )
            fake_gemini.chmod(0o755)

            prompt_path = workspace / "prompt.txt"
            prompt_path.write_text("hello", encoding="utf-8")

            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(bridge_script),
                    "--workspace",
                    str(workspace),
                    "run",
                    "gemini",
                    "--execute",
                    "--prompt-file",
                    str(prompt_path),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["history"]), 1)
            record = state["history"][0]
            self.assertEqual(record["provider"], "gemini")
            self.assertEqual(record["final_text"], "fake gemini reply")
            self.assertFalse(record["handoff_needed"])
            # The sentinel, captured from summarize_gemini()'s clean-run
            # detection, now saved into state so the *next* gemini call in
            # this workspace resumes instead of starting fresh.
            self.assertEqual(state["sessions"]["gemini"], "latest")

    def test_unauthenticated_gemini_run_end_to_end(self):
        # Fake binary shaped exactly like the real unauthenticated CLI
        # (v0.54.0, confirmed 2026-08-06): empty stdout, the JSON error
        # object on stderr, exit code 41. Exercises the real stdout/stderr
        # split through the full run_provider() path, not just
        # summarize_gemini() directly.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            fake_gemini = fake_bin / "gemini"
            fake_gemini.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                "cat >&2 <<'EOF'\n"
                '{"session_id": "abc", "error": {"type": "Error", "message": "Please set an Auth method in your settings.json", "code": 41}}\n'
                "EOF\n"
                "exit 41\n",
                encoding="utf-8",
            )
            fake_gemini.chmod(0o755)

            prompt_path = workspace / "prompt.txt"
            prompt_path.write_text("hello", encoding="utf-8")

            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(bridge_script),
                    "--workspace",
                    str(workspace),
                    "run",
                    "gemini",
                    "--execute",
                    "--prompt-file",
                    str(prompt_path),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=30,
            )
            # run_provider() propagates the provider's own exit code as
            # the bridge's exit code, so 41 here (not 0) is expected.
            self.assertEqual(result.returncode, 41, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            record = state["history"][0]
            self.assertTrue(record["handoff_needed"])
            self.assertTrue(record["reason"].startswith("auth:"), msg=record["reason"])
            self.assertIsNone(state["sessions"].get("gemini"))

    def test_auto_fallback_skips_an_uninstalled_middle_provider_to_reach_gemini(self):
        # The exact scenario a review flagged as reachable only once
        # PROVIDERS grew past two entries: codex fails, claude isn't
        # installed at all, gemini is -- the single-hop auto-fallback
        # must still land on gemini, not silently stop after failing to
        # even start "claude".
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()

            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\ncat >/dev/null\necho 'Error: rate limit exceeded (429)'\nexit 1\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            # Deliberately no fake "claude" script in fake_bin.

            fake_gemini = fake_bin / "gemini"
            fake_gemini.write_text(
                "#!/bin/sh\ncat >/dev/null\necho '{\"response\": \"gemini picked up the handoff\"}'\n",
                encoding="utf-8",
            )
            fake_gemini.chmod(0o755)

            prompt_path = workspace / "prompt.txt"
            prompt_path.write_text("hello", encoding="utf-8")

            # PATH is replaced with a minimal system baseline + fake_bin,
            # not the real inherited PATH (unlike the other integration
            # tests in this file) -- a prepend-only change would still let
            # a real `claude` CLI on this machine's actual PATH answer for
            # "claude", which would silently defeat the point of this
            # specific test (proving the skip-when-uninstalled behavior,
            # not "claude happens to also work here"). A fully-empty PATH
            # doesn't work either -- the fake scripts' own `cat`/`echo`
            # need /bin or /usr/bin, and dropping it produced a confusing
            # "cat: command not found" failure inside the fake scripts
            # instead of the fallback behavior under test.
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin"
            bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(bridge_script),
                    "--workspace",
                    str(workspace),
                    "run",
                    "codex",
                    "--execute",
                    "--auto-fallback",
                    "--prompt-file",
                    str(prompt_path),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["history"]), 2)
            self.assertEqual(state["history"][0]["provider"], "codex")
            self.assertEqual(state["history"][1]["provider"], "gemini")
            self.assertEqual(state["history"][1]["final_text"], "gemini picked up the handoff")
            self.assertFalse(state["history"][1]["handoff_needed"])


class ParseVersionTupleTests(unittest.TestCase):
    def test_v_prefix_is_stripped(self):
        self.assertEqual(hb.parse_version_tuple("v0.2.0"), (0, 2, 0))

    def test_no_prefix_still_works(self):
        self.assertEqual(hb.parse_version_tuple("0.1.0"), (0, 1, 0))

    def test_malformed_returns_none_not_raise(self):
        self.assertIsNone(hb.parse_version_tuple("not-a-version"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(hb.parse_version_tuple(""))

    def test_differing_lengths_compare_sensibly(self):
        # Natural tuple comparison, not string comparison -- "0.2" must
        # compare greater than "0.1.0" despite being "shorter" as text.
        self.assertGreater(hb.parse_version_tuple("0.2"), hb.parse_version_tuple("0.1.0"))
        self.assertLess(hb.parse_version_tuple("0.1"), hb.parse_version_tuple("0.1.1"))


class CheckForUpdateTests(unittest.TestCase):
    """CFL-18, resolved as DEC-20 (docs/design-system/flutter-mapping.html#s1c): check_for_update()
    always returns a dict with a `status` field -- "available"/"current"/
    "unavailable" -- never `None`, specifically so "genuinely current"
    and "couldn't check at all" (gh missing/unauthenticated/offline, all
    real DEC-19-documented failure paths) stay distinguishable instead of
    both collapsing into the same falsy value."""

    def test_a_newer_release_is_reported(self):
        # A hardcoded "v0.2.0" here used to silently collide with
        # BRIDGE_VERSION whenever a real release actually bumped it to
        # that value -- caught for real when cutting the v0.2.0 release.
        # Derived relative to BRIDGE_VERSION (major+1) instead, so this
        # test can never again coincide with whatever the real current
        # version happens to be.
        current = hb.parse_version_tuple(hb.BRIDGE_VERSION)
        newer_tag = f"v{current[0] + 1}.0.0"
        newer_version = f"{current[0] + 1}.0.0"
        with mock.patch.object(
            hb, "short_run", return_value=(0, json.dumps({"tagName": newer_tag, "url": "https://example.invalid/latest"}), "")
        ):
            result = hb.check_for_update()
        self.assertEqual(
            result,
            {
                "status": "available",
                "latest_version": newer_version,
                "current_version": hb.BRIDGE_VERSION,
                "url": "https://example.invalid/latest",
            },
        )

    def test_same_version_is_reported_as_current_not_available(self):
        with mock.patch.object(
            hb, "short_run", return_value=(0, json.dumps({"tagName": f"v{hb.BRIDGE_VERSION}", "url": "https://example.invalid"}), "")
        ):
            result = hb.check_for_update()
        self.assertEqual(result, {"status": "current", "current_version": hb.BRIDGE_VERSION})

    def test_an_older_tag_is_reported_as_current_not_available(self):
        # Shouldn't normally happen (releases only move forward), but a
        # stale/mistagged release must never be offered as an "update".
        with mock.patch.object(
            hb, "short_run", return_value=(0, json.dumps({"tagName": "v0.0.1", "url": "https://example.invalid"}), "")
        ):
            result = hb.check_for_update()
        self.assertEqual(result["status"], "current")

    def test_gh_not_installed_is_unavailable_not_current(self):
        # short_run() itself already turns FileNotFoundError into exit
        # code 127 -- this just confirms check_for_update() treats any
        # nonzero exit as "can't check" (status "unavailable"), not just
        # a specific one, and critically not "current" either -- we
        # genuinely don't know.
        with mock.patch.object(hb, "short_run", return_value=(127, "", "gh not found")):
            result = hb.check_for_update()
        self.assertEqual(result, {"status": "unavailable", "current_version": hb.BRIDGE_VERSION})

    def test_gh_error_exit_is_unavailable(self):
        with mock.patch.object(hb, "short_run", return_value=(1, "", "gh: authentication required")):
            result = hb.check_for_update()
        self.assertEqual(result["status"], "unavailable")

    def test_malformed_json_is_unavailable(self):
        with mock.patch.object(hb, "short_run", return_value=(0, "not json", "")):
            result = hb.check_for_update()
        self.assertEqual(result["status"], "unavailable")

    def test_missing_expected_fields_is_unavailable(self):
        with mock.patch.object(hb, "short_run", return_value=(0, json.dumps({"somethingElse": True}), "")):
            result = hb.check_for_update()
        self.assertEqual(result["status"], "unavailable")

    def test_calls_gh_with_the_repo_pinned_not_relying_on_cwd(self):
        # handoff_webui.py can run with --workspace pointing at any
        # directory, not necessarily a checkout of this repo -- the repo
        # must be explicit, not inferred from cwd's git remote.
        with mock.patch.object(hb, "short_run", return_value=(0, "{}", "")) as spy:
            hb.check_for_update()
        command = spy.call_args.args[0]
        self.assertIn("--repo", command)
        self.assertIn(hb.GITHUB_REPO, command)


if __name__ == "__main__":
    unittest.main()
