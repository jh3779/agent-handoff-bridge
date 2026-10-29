#!/usr/bin/env python3
"""Unit tests for the highest-risk pure logic in handoff_bridge.py.

Run with: python3 -m unittest discover -s tests -v

This is the minimum coverage bar described in docs/quality-gates.md: the
provider fallback/classification logic and the shared-state write path must
have tests, because they have no other safety net (no CI provider calls, no
manual QA step that would catch a silent regression).
"""

from __future__ import annotations

import contextlib
import io
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

    def test_successful_response_merely_mentioning_authentication_is_not_misclassified(self):
        # The auth pattern is also checked against a *successful* run's raw
        # combined stdout+stderr (classify_handoff()'s second loop). A
        # response that merely discusses authentication in prose -- here
        # with the exact phrase agy's real error uses -- must not trigger a
        # handoff/auto-fallback that discards a good answer.
        stdout = json.dumps(
            {
                "conversation_id": "c1",
                "status": "SUCCESS",
                "response": "Note: authentication required for the admin API.",
            }
        )
        parsed = hb.summarize_agy(stdout, "", exit_code=0)
        needed, reason = hb.classify_handoff(0, stdout, "", parsed)
        self.assertFalse(needed, msg=reason)

    def test_agy_unauthenticated_headless_run_is_classified_as_auth(self):
        # agy exits non-zero with "authentication required" when a
        # non-interactive run has no cached login
        # (https://antigravity.google/docs/cli/headless).
        stderr = "Error: authentication required. Run `agy` interactively to sign in."
        parsed = hb.summarize_agy("", stderr, exit_code=1)
        needed, reason = hb.classify_handoff(1, "", stderr, parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("auth:"), msg=reason)

    def test_agy_denied_tool_actions_with_an_empty_response_is_a_tool_failure_handoff(self):
        # Verified against agy 1.4.8 (2026-10-28): print mode auto-denies a
        # write/command that needs a permission prompt, yet exits 0 with
        # status SUCCESS and an empty response. That must not look like a
        # finished run.
        stdout = json.dumps(
            {
                "conversation_id": "c1",
                "status": "SUCCESS",
                "response": "",
                "usage": {"input_tokens": 6870, "output_tokens": 145},
                "denied_actions": [{"action": "write_file", "display_name": "WriteFile"}],
            }
        )
        parsed = hb.summarize_agy(stdout, "jetski: no output produced", exit_code=0)
        needed, reason = hb.classify_handoff(0, stdout, "", parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("tool_failure:"), msg=reason)
        self.assertIn("permissions.allow", parsed["final_text"])
        self.assertIn("write_file", parsed["final_text"])

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
        parsed = hb.summarize_agy(stdout, "", exit_code=0)
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

    def test_successful_codex_run_quoting_a_trigger_word_via_tool_output_is_not_misclassified(self):
        # Regression (real-world report, 2026-09-03): a user asked codex to
        # read/summarize a README that happened to say "Be mindful of the
        # rate limit and quota ... see billing docs" -- ordinary prose in
        # the *file*, not a signal about codex's own run. Because that text
        # arrives via a command_execution item's aggregated_output (not
        # agent_message/final_text), the pre-fix scan still saw it in raw
        # stdout and wrongly classified this successful run as
        # rate_limit-needs-handoff, discarding a good answer (and, with
        # --auto-fallback, silently re-running on a different provider).
        raw_stdout = "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": "t1"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "cat README.md",
                        "aggregated_output": (
                            "# App\n\nBe mindful of the rate limit and quota\n"
                            "when making requests -- see billing docs for details.\n"
                        ),
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Summary: calls an API.\n\nDONE"},
                },
                {"type": "turn.completed", "usage": {}},
            ]
        )
        events = hb.parse_jsonl(raw_stdout)
        parsed = hb.summarize_codex(events)
        needed, reason = hb.classify_handoff(0, raw_stdout, "", parsed)
        self.assertFalse(needed, msg=reason)

    def test_successful_claude_run_quoting_a_trigger_word_via_tool_result_is_not_misclassified(self):
        # Same regression as above, for claude's "user"-typed tool_result
        # echo events (Read tool output).
        raw_stdout = "\n".join(
            json.dumps(event)
            for event in [
                {"type": "system", "subtype": "init", "session_id": "s1"},
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "content": (
                                    "1\t# App\n2\t\n3\tBe mindful of the rate limit and quota\n"
                                    "4\twhen making requests -- see billing docs for details.\n"
                                ),
                            }
                        ]
                    },
                },
                {"type": "result", "result": "Summary: calls an API.\n\nDONE"},
            ]
        )
        events = hb.parse_jsonl(raw_stdout)
        parsed = hb.summarize_claude(events)
        needed, reason = hb.classify_handoff(0, raw_stdout, "", parsed)
        self.assertFalse(needed, msg=reason)

    def _codex_stream(self, events):
        raw = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
        return raw, hb.summarize_codex(hb.parse_jsonl(raw))

    def test_tool_output_with_non_ascii_text_and_trigger_words_is_not_a_signal(self):
        # Real case (2026-10-29): codex searched this repo's own source; the rg output
        # held lines like a Korean i18n string mentioning "quota/rate" and a test
        # string "quota: matched quota signal". The old cut removed each output line
        # by its ASCII-escaped (\\uXXXX) form, but codex writes UTF-8 as-is, so the
        # Korean line survived and an exit-0 run was reported as a quota handoff.
        output = (
            'webui/i18n.js:63:      "settings.autoFallback.note": "선택한 provider가 quota/rate/컨텍스트 한도로 실패하면 전환",\n'
            'tests/test_handoff_bridge.py:1317:  "reason": reason or ("quota: matched quota signal" if handoff else "none"),\n'
            "rg: handoff_chat.py: No such file or directory (os error 2)\n"
            "error\n"
        )
        raw, parsed = self._codex_stream(
            [
                {"type": "thread.started", "thread_id": "t1"},
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "command": "rg quota", "aggregated_output": output, "exit_code": 0},
                },
                {"type": "item.completed", "item": {"type": "agent_message", "text": "찾아봤습니다."}},
                {"type": "turn.completed", "usage": {}},
            ]
        )
        needed, reason = hb.classify_handoff(0, raw, "", parsed)
        self.assertFalse(needed, msg=reason)

    def test_an_intermediate_agent_message_quoting_a_trigger_word_is_not_a_signal(self):
        # Only the *last* agent_message becomes final_text; earlier ones are narration
        # (often quoting protocol words such as "Blocked: tool_failure").
        raw, parsed = self._codex_stream(
            [
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Blocked: tool_failure is a label; also rate limit and quota."}},
                {"type": "item.completed", "item": {"type": "command_execution", "command": "echo \"quota\"", "aggregated_output": "quota\n", "exit_code": 0}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "끝났습니다."}},
                {"type": "turn.completed", "usage": {}},
            ]
        )
        needed, reason = hb.classify_handoff(0, raw, "", parsed)
        self.assertFalse(needed, msg=reason)

    def test_a_real_codex_error_event_is_still_a_handoff_and_gets_the_right_label(self):
        raw, parsed = self._codex_stream(
            [
                {"type": "thread.started", "thread_id": "t1"},
                {"type": "item.completed", "item": {"type": "command_execution", "command": "rg", "aggregated_output": "tool_failure auth quota", "exit_code": 0}},
                {"type": "error", "message": "You exceeded your current quota, please check your plan"},
            ]
        )
        needed, reason = hb.classify_handoff(0, raw, "", parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("quota:"), msg=reason)  # not auth/tool_failure from the tool output

    def test_plain_text_and_stderr_signals_are_still_caught_next_to_json_events(self):
        raw, parsed = self._codex_stream(
            [{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}]
        )
        needed, reason = hb.classify_handoff(0, raw + "\nError: 429 too many requests", "", parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("rate_limit:"), msg=reason)
        needed, reason = hb.classify_handoff(1, raw, "usage limit reached", parsed)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("rate_limit:"), msg=reason)

    def test_multiline_final_text_is_still_excluded_from_the_raw_json_escaped_stdout(self):
        # Regression (found in review while fixing the above): a decoded
        # final_text containing a real newline character doesn't literally
        # appear in raw JSONL stdout, where json.dumps encodes it as the two
        # characters `\` `n` instead -- a plain combined.replace(final_text,
        # "") silently fails to strip it, so this only worked by coincidence
        # for previously-tested single-line text.
        events_raw = [
            {"type": "thread.started", "thread_id": "t1"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "line one\nrate limit and quota\nline three"},
            },
        ]
        raw_stdout = "\n".join(json.dumps(e) for e in events_raw)
        events = hb.parse_jsonl(raw_stdout)
        parsed = hb.summarize_codex(events)
        needed, reason = hb.classify_handoff(0, raw_stdout, "", parsed)
        self.assertFalse(needed, msg=reason)

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

    def test_handoff_needed_from_claude_switches_to_agy(self):
        # Phase 5: PROVIDERS is now ("codex", "claude", "agy") -- N-way
        # fallback walks to the *next* provider in that order, not back to
        # the start. "the other one" stopped being well-defined once a
        # third provider existed (docs/provider-extensibility.md).
        state = {"status": "handoff_needed", "last_provider": "claude", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.choose_auto_provider(state), "agy")

    def test_a_state_file_from_before_gemini_was_removed_still_resolves(self):
        # state.json written by an older version can name "gemini" as the
        # primary or last provider; neither may crash or be run.
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "claude" and "/usr/bin/claude"):
            legacy_primary = {"status": "ready", "primary_provider": "gemini"}
            self.assertEqual(hb.choose_auto_provider(legacy_primary), "claude")
            legacy_last = {"status": "handoff_needed", "last_provider": "gemini", "primary_provider": "codex"}
            self.assertEqual(hb.choose_auto_provider(legacy_last), "claude")

    def test_handoff_needed_from_agy_wraps_around_to_codex(self):
        state = {"status": "handoff_needed", "last_provider": "agy", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.choose_auto_provider(state), "codex")

    def test_handoff_needed_skips_an_uninstalled_provider_in_between(self):
        # The exact scenario a review flagged: codex fails, claude isn't
        # installed, agy is -- the single-hop fallback must still reach
        # the installed agy instead of naively landing on claude and
        # stopping there.
        state = {"status": "handoff_needed", "last_provider": "codex", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name in ("codex", "agy") and f"/usr/bin/{name}"):
            self.assertEqual(hb.choose_auto_provider(state), "agy")

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


class NormalizePathTests(unittest.TestCase):
    def test_expands_home_and_resolves_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                (Path(tmp) / "sub").mkdir()
                result = hb.normalize_path("sub")
                self.assertEqual(result, (Path(tmp) / "sub").resolve())
            finally:
                os.chdir(original_cwd)

    def test_resolve_workspace_uses_the_same_normalization(self):
        # resolve_workspace() used to inline Path(path).expanduser().resolve()
        # itself -- a structure audit found the same expression independently
        # reimplemented in handoff_control.py/handoff_webui.py/
        # remote_handoff_server.py. Confirm resolve_workspace() now goes
        # through the shared helper rather than a parallel copy that could
        # silently drift from it.
        with mock.patch.object(hb, "normalize_path", wraps=hb.normalize_path) as spy:
            with tempfile.TemporaryDirectory() as tmp:
                hb.resolve_workspace(tmp)
        spy.assert_called_once_with(tmp)


class LoadStateTests(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        # addCleanup runs LIFO: chdir back to _orig_cwd must be registered
        # *after* (so it runs *before*) _tmp.cleanup() -- deleting a
        # directory while it's still the process's cwd raises
        # PermissionError on Windows (allowed on POSIX, which is why this
        # was invisible until the suite first ran on Windows -- see
        # RunProviderAutoFallbackBuildPromptCountTests for the same fix).
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def test_missing_state_file_returns_default_state(self):
        state = hb.load_state()
        self.assertEqual(state["status"], "new")
        self.assertEqual(state["history"], [])

    def test_corrupted_state_file_degrades_to_default_instead_of_raising(self):
        # Regression (structure audit): load_state() used to have no
        # try/except around its json.loads() call at all, unlike its two
        # peripheral counterparts (handoff_webui.py's read_state_dict(),
        # remote_handoff_server.py's read_json()), which both already
        # degraded gracefully on a corrupted/partially-written file.
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        hb.STATE_FILE.write_text('{"task": "truncated', encoding="utf-8")
        state = hb.load_state()
        self.assertEqual(state["status"], "new")
        self.assertEqual(state["history"], [])

    def test_valid_state_file_is_read_as_is(self):
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        hb.STATE_FILE.write_text('{"task": "real task", "status": "ready"}', encoding="utf-8")
        state = hb.load_state()
        self.assertEqual(state["task"], "real task")
        self.assertEqual(state["status"], "ready")


class BuildPromptSharedContextTests(unittest.TestCase):
    """`.handoff/shared-context.md` -- free-form, user-authored, per-
    workspace project context folded into every prompt regardless of
    provider."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        # addCleanup runs LIFO -- see LoadStateTests.setUp() above for why
        # this order matters on Windows.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def _prompt(self):
        return hb.build_prompt("codex", {"task": "do the thing"}, "hello")

    def test_missing_file_adds_no_project_context_section(self):
        self.assertNotIn("## Project Context", self._prompt())

    def test_whitespace_only_file_adds_no_section_either(self):
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        hb.SHARED_CONTEXT_FILE.write_text("   \n\n", encoding="utf-8")
        self.assertNotIn("## Project Context", self._prompt())

    def test_real_content_is_folded_into_the_prompt(self):
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        hb.SHARED_CONTEXT_FILE.write_text("Never touch the legacy/ folder.", encoding="utf-8")
        prompt = self._prompt()
        self.assertIn("## Project Context", prompt)
        self.assertIn("Never touch the legacy/ folder.", prompt)


class TailForPromptTests(unittest.TestCase):
    """tail_for_prompt() -- bounds how much of an ever-growing file (in
    practice, .handoff/current.md) gets folded into a single prompt. See
    MAX_CURRENT_FILE_PROMPT_CHARS."""

    def test_short_text_is_returned_unchanged(self):
        text = "short and sweet"
        self.assertEqual(hb.tail_for_prompt(text, 100), text)

    def test_text_at_exactly_the_limit_is_unchanged(self):
        text = "x" * 100
        self.assertEqual(hb.tail_for_prompt(text, 100), text)

    def test_long_text_is_cut_down_to_roughly_the_limit(self):
        text = "x" * 1000
        result = hb.tail_for_prompt(text, 100)
        self.assertLess(len(result), 1000)

    def test_long_text_keeps_the_most_recent_content(self):
        text = "old " * 1000 + "MOST_RECENT_MARKER"
        result = hb.tail_for_prompt(text, 100)
        self.assertIn("MOST_RECENT_MARKER", result)

    def test_long_text_drops_the_oldest_content(self):
        text = "OLDEST_MARKER" + ("filler " * 1000)
        result = hb.tail_for_prompt(text, 100)
        self.assertNotIn("OLDEST_MARKER", result)

    def test_truncation_is_noted_so_it_is_not_mistaken_for_the_whole_file(self):
        result = hb.tail_for_prompt("x" * 1000, 100)
        self.assertIn("earlier history omitted", result)

    def test_cuts_at_a_newline_boundary_not_mid_line(self):
        text = "## Run one\ncontent one\n" * 50 + "## Run two\nfinal content\n"
        result = hb.tail_for_prompt(text, 40)
        # The kept portion must start at the beginning of a line, not
        # mid-word/mid-heading -- i.e. everything after the omission
        # marker's blank line is a clean suffix of the original text.
        kept = result.split("\n\n", 1)[1]
        self.assertTrue(text.endswith(kept))


class BuildPromptCurrentFileCapTests(unittest.TestCase):
    """End-to-end: build_prompt() actually applies tail_for_prompt() to
    .handoff/current.md, not just that the helper itself works in
    isolation."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def test_a_small_current_file_is_included_in_full(self):
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        hb.CURRENT_FILE.write_text("## Run one\nsmall log\n", encoding="utf-8")
        prompt = hb.build_prompt("codex", {"task": "do the thing"}, "hello")
        self.assertIn("small log", prompt)

    def test_a_huge_current_file_is_truncated_in_the_prompt(self):
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        huge = "OLDEST_ENTRY_MARKER\n" + ("## Run\nfiller\n" * 5000) + "NEWEST_ENTRY_MARKER"
        hb.CURRENT_FILE.write_text(huge, encoding="utf-8")
        prompt = hb.build_prompt("codex", {"task": "do the thing"}, "hello")
        self.assertIn("NEWEST_ENTRY_MARKER", prompt)
        self.assertNotIn("OLDEST_ENTRY_MARKER", prompt)
        # The full, untruncated file on disk must be completely untouched
        # by building a prompt from it -- this caps what's *sent*, not
        # what's *kept* as the durable handoff log.
        self.assertIn("OLDEST_ENTRY_MARKER", hb.CURRENT_FILE.read_text(encoding="utf-8"))


class BuildPromptContinuationTests(unittest.TestCase):
    """A provider with a live `--resume`d session (`state["sessions"]`)
    already has the static protocol docs and prior handoff history from
    its own session's first turn -- re-sending all of that in full on
    every subsequent turn is pure repeated overhead. A continuation turn
    (no `reason`, i.e. not a fresh handoff) sends only what's new."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

    def test_first_turn_with_no_session_gets_the_full_context(self):
        state = {"task": "do the thing", "sessions": {}}
        prompt = hb.build_prompt("codex", state, "hello", full=True)
        self.assertIn("## Agent Targeting Protocol", prompt)
        self.assertIn("## Shared Agent Contract", prompt)
        self.assertIn("## Verification Playbook", prompt)

    def test_first_turn_default_is_compact_working_rules_not_the_three_docs(self):
        state = {"task": "do the thing", "sessions": {}}
        prompt = hb.build_prompt("codex", state, "hello")
        self.assertIn("## Working Rules", prompt)
        self.assertNotIn("## Agent Targeting Protocol", prompt)
        self.assertNotIn("## Shared Agent Contract", prompt)
        self.assertNotIn("## Verification Playbook", prompt)

    def test_continuation_turn_omits_the_static_protocol_docs(self):
        state = {"task": "do the thing", "sessions": {"codex": "existing-session-id"}}
        prompt = hb.build_prompt("codex", state, "hello")
        self.assertNotIn("## Agent Targeting Protocol", prompt)
        self.assertNotIn("## Shared Agent Contract", prompt)
        self.assertNotIn("## Verification Playbook", prompt)
        self.assertIn("hello", prompt)

    def test_a_handoff_reason_forces_full_context_even_with_an_existing_session(self):
        # A handoff (rate_limit/quota/etc., or an explicit provider switch)
        # is a cold start for this provider's *task understanding* even if
        # it happens to already have a lingering session id from earlier,
        # unrelated use -- it must not be treated as a plain continuation.
        state = {"task": "do the thing", "sessions": {"claude": "old-session-id"}}
        prompt = hb.build_prompt("claude", state, "hello", reason="rate_limit: codex hit a limit")
        self.assertIn("## Working Rules", prompt)
        self.assertIn("## Handoff Reason", prompt)
        full_prompt = hb.build_prompt("claude", state, "hello", reason="rate_limit: codex hit a limit", full=True)
        self.assertIn("## Shared Agent Contract", full_prompt)

    def test_continuation_only_includes_log_entries_added_since_this_providers_last_turn(self):
        hb.CURRENT_FILE.write_text("OLD_ENTRY_ALREADY_SEEN\n", encoding="utf-8")
        state = {"task": "do the thing", "sessions": {"codex": "s1"}}
        # First continuation call records the offset as of this point.
        hb.build_prompt("codex", state, "first message")
        hb.CURRENT_FILE.write_text(
            hb.CURRENT_FILE.read_text(encoding="utf-8") + "NEW_ENTRY_SINCE_LAST_TURN\n", encoding="utf-8"
        )
        prompt = hb.build_prompt("codex", state, "second message")
        self.assertIn("NEW_ENTRY_SINCE_LAST_TURN", prompt)
        self.assertNotIn("OLD_ENTRY_ALREADY_SEEN", prompt)

    def test_no_new_log_entries_since_last_turn_adds_no_empty_section(self):
        hb.CURRENT_FILE.write_text("SOME_ENTRY\n", encoding="utf-8")
        state = {"task": "do the thing", "sessions": {"codex": "s1"}}
        hb.build_prompt("codex", state, "first message")
        prompt = hb.build_prompt("codex", state, "second message, nothing new logged")
        self.assertNotIn("## New Handoff Log Entries Since Your Last Turn", prompt)

    def test_offsets_are_tracked_independently_per_provider(self):
        hb.CURRENT_FILE.write_text("ENTRY_BEFORE_EITHER_PROVIDER_RAN\n", encoding="utf-8")
        state = {"task": "do the thing", "sessions": {"codex": "s1", "claude": "s2"}}
        # codex's first continuation call advances *only* codex's offset.
        hb.build_prompt("codex", state, "codex turn")
        hb.CURRENT_FILE.write_text(
            hb.CURRENT_FILE.read_text(encoding="utf-8") + "ENTRY_AFTER_CODEX_TURN\n", encoding="utf-8"
        )
        # claude has never had a turn yet -- its own first continuation
        # call must still see everything logged so far, not just what
        # was appended after codex's turn.
        claude_prompt = hb.build_prompt("claude", state, "claude turn")
        self.assertIn("ENTRY_BEFORE_EITHER_PROVIDER_RAN", claude_prompt)
        self.assertIn("ENTRY_AFTER_CODEX_TURN", claude_prompt)


class BuildPromptSelfContainedNoticeTests(unittest.TestCase):
    """Regression (real-world reproduction, 2026-09-04): asked to run a
    real first-turn prompt end-to-end, codex re-opened every doc already
    inlined in its own prompt via `sed`, and separately matched this
    project's "handoff" terminology against an unrelated, user-machine-
    global codex skill of the same name (a different `.agent/`
    PROJECT_CONTEXT/HANDOFF/DECISIONS scheme) plus a generic self-
    evaluation skill -- ballooning a one-word message into a multi-minute
    session that reran this project's own full test suite repeatedly and
    edited unrelated files. SELF_CONTAINED_NOTICE is a best-effort, in-
    prompt mitigation for both failure modes; these tests only guard that
    it's actually present, not that a model necessarily obeys it."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

    def test_first_turn_includes_the_self_contained_notice(self):
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn(hb.SELF_CONTAINED_NOTICE, prompt)

    def test_continuation_turn_includes_the_self_contained_notice(self):
        state = {"task": "do the thing", "sessions": {"codex": "existing-session-id"}}
        prompt = hb.build_prompt("codex", state, "hello")
        self.assertIn(hb.SELF_CONTAINED_NOTICE, prompt)

    def test_notice_disambiguates_from_an_unrelated_global_skill_of_the_same_name(self):
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn("unrelated", prompt)
        self.assertIn("project-continuation, checkpoint, or self-evaluation skill", prompt)

    def test_required_behavior_tells_the_model_not_to_reopen_already_included_docs(self):
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello", full=True)
        self.assertIn("no need to", prompt)
        self.assertIn("open these files again", prompt)
        self.assertIn("respect it without re-reading", prompt)

    def test_compact_required_behavior_also_tells_the_model_not_to_open_the_docs(self):
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn("that summary is complete", prompt)
        self.assertIn("do not open `docs/agent-targeting-protocol.md`", prompt)
        self.assertIn("respect it without re-reading", prompt)

    def test_first_turn_includes_the_scope_discipline_notice(self):
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn(hb.SCOPE_DISCIPLINE_NOTICE, prompt)

    def test_continuation_turn_includes_the_scope_discipline_notice(self):
        state = {"task": "do the thing", "sessions": {"codex": "existing-session-id"}}
        prompt = hb.build_prompt("codex", state, "hello")
        self.assertIn(hb.SCOPE_DISCIPLINE_NOTICE, prompt)

    def test_scope_discipline_notice_tells_the_model_not_to_go_looking_for_extra_work(self):
        # Regression (real-world reproduction, 2026-09-04, second finding):
        # even after SELF_CONTAINED_NOTICE stopped the redundant re-reads
        # and the "handoff"-named skill collision, a follow-up real run
        # still ballooned a trivial message into a multi-minute session --
        # this time by finding unrelated real bugs (a missing .gitignore,
        # a stale run lock) and pulling in yet another global skill (a
        # generic TDD workflow) to fix them, none of which the turn's
        # actual prompt asked for.
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn("do not proactively audit", prompt)
        self.assertIn("unrelated bugs", prompt)
        self.assertIn("full project test suite", prompt)
        self.assertIn("do not invoke a skill, subagent, or larger workflow", prompt)

    def test_scope_discipline_notice_tells_the_model_to_report_blockers_not_fix_them(self):
        # Regression (real-world reproduction, 2026-09-04, third finding):
        # even with the notice above, a real run still fixed a real bug (a
        # missing .gitignore) it happened to hit while merely trying to
        # verify its own no-op turn -- its own reasoning treated "the
        # blocker in front of me" as in-scope even though nothing about it
        # was actually asked for.
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn("report it briefly", prompt)
        self.assertIn("do not fix it unprompted", prompt)


class SplitLogSectionsTests(unittest.TestCase):
    def test_empty_text_has_no_sections(self):
        self.assertEqual(hb.split_log_sections(""), [])

    def test_sections_rejoin_to_the_original_text(self):
        text = "# Title\nintro\n## Run A\nbody a\n### Sub\nbody sub\n## Run B\nbody b\n"
        sections = hb.split_log_sections(text)
        self.assertEqual("".join(sections), text)
        self.assertEqual(len(sections), 4)

    def test_text_starting_with_a_heading_has_no_empty_leading_section(self):
        sections = hb.split_log_sections("## Run A\na\n## Run B\nb\n")
        self.assertEqual(sections, ["## Run A\na\n", "## Run B\nb\n"])

    def test_text_without_any_heading_is_one_section(self):
        self.assertEqual(hb.split_log_sections("just notes\nmore\n"), ["just notes\nmore\n"])

    def test_level_one_heading_and_hash_inside_a_line_do_not_split(self):
        text = "# Title\nsee issue ## 5 for details\n"
        self.assertEqual(hb.split_log_sections(text), [text])


class RecentLogForPromptTests(unittest.TestCase):
    def _log(self, count, body="x" * 100):
        return "".join(f"## Run {i}\n{body}\n" for i in range(count))

    def test_text_under_the_budget_is_returned_unchanged(self):
        text = self._log(2)
        self.assertEqual(hb.recent_log_for_prompt(text, 10_000), text)

    def test_keeps_whole_newest_sections_and_marks_the_omission(self):
        text = self._log(50)
        result = hb.recent_log_for_prompt(text, 500)
        self.assertTrue(result.startswith(hb.EARLIER_HISTORY_OMITTED_NOTE))
        self.assertIn("## Run 49\n", result)
        self.assertNotIn("## Run 0\n", result)
        # Whole sections only: every kept section is complete.
        self.assertEqual(result.count("## Run "), result.count("x" * 100))

    def test_stays_within_the_budget_when_sections_are_small(self):
        text = self._log(50)
        result = hb.recent_log_for_prompt(text, 500)
        body = result[len(hb.EARLIER_HISTORY_OMITTED_NOTE) :].lstrip()
        self.assertLessEqual(len(body), 500)

    def test_an_oversized_newest_section_falls_back_to_a_character_tail(self):
        text = "## Old\nold\n## New\n" + ("y" * 5000) + "\nNEWEST_LINE\n"
        result = hb.recent_log_for_prompt(text, 1000)
        self.assertIn("NEWEST_LINE", result)
        self.assertNotIn("old\n", result)
        self.assertLessEqual(len(result), 1000 + len(hb.EARLIER_HISTORY_OMITTED_NOTE) + 2)


class CompactPromptSizeTests(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

    def test_compact_prompt_is_much_smaller_than_full_for_a_long_log(self):
        log = "".join(f"## Run {i}\n" + ("filler line\n" * 40) for i in range(200))
        hb.CURRENT_FILE.write_text(log, encoding="utf-8")
        state = {"task": "do the thing", "sessions": {}}
        compact = hb.build_prompt("claude", state, "hello", reason="quota: x")
        full = hb.build_prompt("claude", state, "hello", reason="quota: x", full=True)
        self.assertLess(len(compact), len(full) // 2)
        self.assertLess(len(compact), 12_000)

    def test_compact_prompt_keeps_the_newest_remaining_and_next_entry(self):
        log = "".join(f"## Run {i}\n" + ("filler line\n" * 40) for i in range(200))
        log += "### Handoff\n- Remaining: finish the parser\n- Next: run the tests\n"
        hb.CURRENT_FILE.write_text(log, encoding="utf-8")
        prompt = hb.build_prompt("claude", {"task": "t", "sessions": {}}, "hello", reason="quota: x")
        self.assertIn("Remaining: finish the parser", prompt)
        self.assertIn("Next: run the tests", prompt)

    def test_full_mode_still_inlines_the_real_docs_when_present(self):
        Path("docs").mkdir()
        Path("docs/shared-agent-contract.md").write_text("CUSTOM_CONTRACT_MARKER", encoding="utf-8")
        state = {"task": "t", "sessions": {}}
        self.assertIn("CUSTOM_CONTRACT_MARKER", hb.build_prompt("codex", state, "hi", full=True))
        # A customised doc is sent as written even without --full-prompt.
        self.assertIn("CUSTOM_CONTRACT_MARKER", hb.build_prompt("codex", state, "hi"))

    def test_workspace_copies_identical_to_the_bundled_docs_stay_compact(self):
        Path("docs").mkdir()
        for rel in hb.PROTOCOL_DOC_PATHS:
            Path(rel).write_text((hb.BRIDGE_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
        self.assertFalse(hb.protocol_docs_customized())
        prompt = hb.build_prompt("codex", {"task": "t", "sessions": {}}, "hi")
        self.assertIn("## Working Rules", prompt)

    def test_crlf_line_endings_alone_do_not_count_as_customised(self):
        Path("docs").mkdir()
        for rel in hb.PROTOCOL_DOC_PATHS:
            text = (hb.BRIDGE_ROOT / rel).read_text(encoding="utf-8").replace("\n", "\r\n")
            Path(rel).write_bytes(text.encode("utf-8"))
        self.assertFalse(hb.protocol_docs_customized())

    def test_a_workspace_without_the_docs_stays_compact(self):
        self.assertFalse(hb.protocol_docs_customized())

    def test_an_extra_rule_in_the_verification_playbook_switches_to_full(self):
        Path("docs").mkdir()
        bundled = (hb.BRIDGE_ROOT / "docs/verification-playbook.md").read_text(encoding="utf-8")
        Path("docs/verification-playbook.md").write_text(bundled + "\nRun ./company-check.sh before release.\n", encoding="utf-8")
        self.assertTrue(hb.protocol_docs_customized())
        prompt = hb.build_prompt("codex", {"task": "t", "sessions": {}}, "hi")
        self.assertIn("Run ./company-check.sh before release.", prompt)
        self.assertNotIn("## Working Rules", prompt)

    def test_protocol_summary_names_every_failure_label(self):
        for label in hb.HANDOFF_LABELS:
            self.assertIn(label, hb.PROTOCOL_SUMMARY)

    def test_continuation_turn_caps_unseen_entries_to_the_compact_budget(self):
        hb.CURRENT_FILE.write_text("", encoding="utf-8")
        state = {"task": "t", "sessions": {"codex": "s1"}}
        hb.build_prompt("codex", state, "first")
        burst = "".join(f"## Run {i}\n" + ("z" * 200) + "\n" for i in range(100))
        hb.CURRENT_FILE.write_text(burst + "## Last\nLAST_ENTRY_MARKER\n", encoding="utf-8")
        prompt = hb.build_prompt("codex", state, "second")
        self.assertIn("LAST_ENTRY_MARKER", prompt)
        self.assertNotIn("## Run 0\n", prompt)


class ArchiveOldCurrentEntriesTests(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)
        hb.HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

    def _big_log(self, count=400):
        return "".join(f"## Run {i}\n" + ("w" * 300) + "\n" for i in range(count))

    def test_small_file_is_left_alone(self):
        hb.CURRENT_FILE.write_text("## Run 0\nsmall\n", encoding="utf-8")
        self.assertEqual(hb.archive_old_current_entries({}), 0)
        self.assertFalse(hb.ARCHIVE_DIR.exists())

    def test_missing_file_is_a_no_op(self):
        self.assertEqual(hb.archive_old_current_entries({}), 0)

    def test_large_file_moves_old_entries_and_loses_nothing(self):
        original = self._big_log()
        hb.CURRENT_FILE.write_text(original, encoding="utf-8")
        moved = hb.archive_old_current_entries({})
        self.assertGreater(moved, 0)
        live = hb.CURRENT_FILE.read_text(encoding="utf-8")
        archives = sorted(hb.ARCHIVE_DIR.glob("current-*.md"))
        self.assertEqual(len(archives), 1)
        archived = archives[0].read_text(encoding="utf-8")
        self.assertTrue(live.startswith(hb.CURRENT_ARCHIVE_POINTER))
        self.assertEqual(archived + live[len(hb.CURRENT_ARCHIVE_POINTER) :], original)
        self.assertLessEqual(len(live), hb.CURRENT_ARCHIVE_KEEP_CHARS + len(hb.CURRENT_ARCHIVE_POINTER) + 400)
        self.assertIn("## Run 399\n", live)

    def test_a_second_archive_does_not_stack_pointers_or_overwrite_the_first(self):
        hb.CURRENT_FILE.write_text(self._big_log(), encoding="utf-8")
        hb.archive_old_current_entries({})
        hb.CURRENT_FILE.write_text(
            hb.CURRENT_FILE.read_text(encoding="utf-8") + self._big_log(400), encoding="utf-8"
        )
        hb.archive_old_current_entries({})
        live = hb.CURRENT_FILE.read_text(encoding="utf-8")
        self.assertEqual(live.count(hb.CURRENT_ARCHIVE_POINTER), 1)
        self.assertEqual(len(list(hb.ARCHIVE_DIR.glob("current-*.md"))), 2)

    def test_offsets_shift_so_a_continuation_turn_still_sees_only_new_entries(self):
        hb.CURRENT_FILE.write_text(self._big_log(), encoding="utf-8")
        state = {"task": "t", "sessions": {"codex": "s1"}}
        hb.build_prompt("codex", state, "first")  # records the offset at end-of-file
        hb.archive_old_current_entries(state)
        live_len = len(hb.CURRENT_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["prompt_context_offsets"]["codex"], live_len)
        hb.CURRENT_FILE.write_text(
            hb.CURRENT_FILE.read_text(encoding="utf-8") + "## Fresh\nFRESH_ENTRY_MARKER\n", encoding="utf-8"
        )
        prompt = hb.build_prompt("codex", state, "second")
        self.assertIn("FRESH_ENTRY_MARKER", prompt)
        self.assertNotIn("## Run 399\n", prompt)

    def test_an_offset_past_the_end_of_the_log_resends_instead_of_skipping_new_entries(self):
        # Reproduces: archive, then the run is interrupted before state.json
        # is saved, so the on-disk offset still points into the old, longer log.
        hb.CURRENT_FILE.write_text("## Fresh\nFRESH_AFTER_INTERRUPT\n", encoding="utf-8")
        state = {"task": "t", "sessions": {"codex": "s1"}, "prompt_context_offsets": {"codex": 100_000}}
        prompt = hb.build_prompt("codex", state, "next")
        self.assertIn("FRESH_AFTER_INTERRUPT", prompt)

    def test_a_negative_or_non_integer_offset_is_treated_as_zero(self):
        hb.CURRENT_FILE.write_text("## Entry\nENTRY_MARKER\n", encoding="utf-8")
        for bad in (-5, "12", None):
            state = {"task": "t", "sessions": {"codex": "s1"}, "prompt_context_offsets": {"codex": bad}}
            self.assertIn("ENTRY_MARKER", hb.build_prompt("codex", state, "next"))

    def test_run_provider_saves_the_shifted_offsets_before_calling_the_provider(self):
        hb.CURRENT_FILE.write_text(self._big_log(), encoding="utf-8")
        old_len = len(hb.CURRENT_FILE.read_text(encoding="utf-8"))
        state = {"task": "t", "primary_provider": "claude", "status": "ready",
                 "sessions": {"claude": "s1"}, "prompt_context_offsets": {"claude": old_len}}
        hb.save_state(state)
        seen = {}

        def fake_run(command, **kwargs):
            if command[0] == "git":
                return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
            # What an interruption right now would leave on disk.
            seen["offset"] = json.loads(hb.STATE_FILE.read_text(encoding="utf-8"))["prompt_context_offsets"]["claude"]
            seen["live_len"] = len(hb.CURRENT_FILE.read_text(encoding="utf-8"))
            raise KeyboardInterrupt

        args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=False,
                                     timeout_seconds=0, model=None, instruction_type="continue")
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
            hb.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaises(KeyboardInterrupt):
                hb.run_provider("claude", args, state)
        self.assertLess(seen["offset"], old_len)
        self.assertLessEqual(seen["offset"], seen["live_len"])

    def test_the_archive_directory_is_not_git_ignored(self):
        ignore_lines = (hb.BRIDGE_ROOT / ".handoff/.gitignore").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("archive/", [line.strip() for line in ignore_lines])
        self.assertNotIn("archive", [line.strip().rstrip("/") for line in ignore_lines])

    def test_an_offset_pointing_into_archived_text_clamps_to_zero(self):
        hb.CURRENT_FILE.write_text(self._big_log(), encoding="utf-8")
        state = {"prompt_context_offsets": {"claude": 10}}
        hb.archive_old_current_entries(state)
        self.assertEqual(state["prompt_context_offsets"]["claude"], 0)


class FullPromptFlagTests(unittest.TestCase):
    def test_run_accepts_full_prompt_flag(self):
        args = hb.build_parser().parse_args(["run", "codex", "hi", "--full-prompt"])
        self.assertTrue(args.full_prompt)
        self.assertFalse(hb.build_parser().parse_args(["run", "codex", "hi"]).full_prompt)


class UniqueRunDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def test_two_turns_in_the_same_second_get_separate_log_directories(self):
        fixed = hb.datetime(2026, 1, 1, 12, 0, 0, tzinfo=hb.timezone.utc)

        def fake_run(command, **kwargs):
            if command[0] == "git":
                return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
            marker = command[0]
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout='{"type": "result", "session_id": "s", "result": "reply from ' + marker + '", "is_error": false}\n',
                stderr="",
            )

        args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=False,
                                     timeout_seconds=0, model=None, instruction_type="continue")
        state = {"task": "t", "primary_provider": "claude", "status": "ready"}
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
            hb.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(hb, "datetime") as fake_datetime:
            fake_datetime.now.return_value = fixed
            hb.run_provider("claude", args, state)
            first_stdout = (Path(state["history"][0]["run_dir"]) / "stdout.jsonl").read_text(encoding="utf-8")
            args.prompt = "second"
            hb.run_provider("claude", args, state)
        dirs = [record["run_dir"] for record in state["history"]]
        self.assertEqual(len(set(dirs)), 2)
        self.assertEqual((Path(dirs[0]) / "stdout.jsonl").read_text(encoding="utf-8"), first_stdout)
        self.assertTrue(dirs[1].endswith("-claude-2"))


class UsageTokenCountsTests(unittest.TestCase):
    def test_codex_shape(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50}
        self.assertEqual(hb.usage_token_counts(usage), {"input": 1000, "cached": 800, "output": 50})

    def test_claude_shape_with_nested_objects(self):
        usage = {
            "input_tokens": 12,
            "output_tokens": 34,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 70,
            "server_tool_use": {"web_search_requests": 0},
        }
        self.assertEqual(hb.usage_token_counts(usage), {"input": 12, "output": 34, "cached": 5000})

    def test_unrecognised_or_missing_usage_yields_nothing_rather_than_a_guess(self):
        self.assertEqual(hb.usage_token_counts(None), {})
        self.assertEqual(hb.usage_token_counts({"models": {"x": {"tokens": {"prompt": 5, "total": 9}}}}), {})

    def test_booleans_and_strings_are_ignored(self):
        self.assertEqual(hb.usage_token_counts({"input_tokens": True, "output_tokens": "7"}), {})

    def test_estimate_tokens_rounds_up(self):
        self.assertEqual(hb.estimate_tokens(0), 0)
        self.assertEqual(hb.estimate_tokens(1), 1)
        self.assertEqual(hb.estimate_tokens(4), 1)
        self.assertEqual(hb.estimate_tokens(5), 2)


class WriteLockBusyTests(unittest.TestCase):
    """Windows reports a lock file another holder is deleting as
    PermissionError, which failed concurrent Web UI requests with a 500 (found by
    the Windows CI run). It is retried briefly there, but a lasting
    PermissionError must surface instead of waiting out a long lock timeout."""

    def _lock(self, timeout=3600.0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return hb.WriteLock(Path(tmp.name) / ".write.lock", timeout=timeout)

    @staticmethod
    def _clock(step):
        ticks = iter(i * step for i in range(10_000))
        return lambda: next(ticks)

    def _patched(self, errors, open_side_effect, step=0.1):
        return (
            mock.patch.object(hb, "TRANSIENT_LOCK_ERRORS", errors),
            mock.patch.object(hb.os, "open", side_effect=open_side_effect),
            mock.patch.object(hb.time, "sleep"),
            mock.patch.object(hb.time, "monotonic", side_effect=self._clock(step)),
        )

    def test_a_brief_permission_error_is_retried_until_the_lock_is_free(self):
        real_open = os.open
        calls = {"n": 0}

        def flaky(path, flags, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        patches = self._patched((PermissionError,), flaky)
        with patches[0], patches[1], patches[2], patches[3]:
            with self._lock():
                pass
        self.assertEqual(calls["n"], 3)

    def test_a_lasting_permission_error_is_raised_not_waited_out(self):
        always = PermissionError(13, "Permission denied")
        patches = self._patched((PermissionError,), always, step=0.5)
        with patches[0], patches[1] as opened, patches[2], patches[3]:
            with self.assertRaises(PermissionError):
                with self._lock(timeout=3600.0):
                    pass
        # 2.0s of budget at 0.5s per tick: a handful of attempts, not thousands.
        self.assertLess(opened.call_count, 10)

    def test_where_permission_errors_are_not_transient_they_fail_immediately(self):
        patches = self._patched((), PermissionError(13, "Permission denied"))
        with patches[0], patches[1] as opened, patches[2], patches[3]:
            with self.assertRaises(PermissionError):
                with self._lock():
                    pass
        self.assertEqual(opened.call_count, 1)

    def test_a_held_lock_still_times_out_with_the_cause_attached(self):
        patches = self._patched((), FileExistsError(17, "exists"), step=1.0)
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(TimeoutError) as raised:
                with self._lock(timeout=3.0):
                    pass
        self.assertIsInstance(raised.exception.__cause__, FileExistsError)


class ParseCliArgsTests(unittest.TestCase):
    """The docs write `run codex --execute "text"`; Python 3.11's argparse
    rejected it (optional positional after an option), so main() goes through
    parse_cli_args(). Run on 3.11 and a newer Python to see both paths."""

    def _parse(self, *argv):
        return hb.parse_cli_args(hb.build_parser(), list(argv))

    def test_prompt_after_an_option(self):
        args = self._parse("run", "codex", "--execute", "hello world")
        self.assertEqual((args.provider, args.execute, args.prompt), ("codex", True, "hello world"))

    def test_prompt_before_an_option_still_works(self):
        args = self._parse("run", "codex", "hello", "--execute")
        self.assertEqual((args.prompt, args.execute), ("hello", True))

    def test_run_without_a_prompt_keeps_the_empty_default(self):
        self.assertEqual(self._parse("run", "codex", "--execute").prompt, "")

    def test_chat_provider_after_an_option(self):
        self.assertEqual(self._parse("chat", "--full-prompt", "claude").provider, "claude")

    def test_chat_without_a_provider_defaults_to_auto(self):
        self.assertEqual(self._parse("chat").provider, "auto")
        self.assertEqual(self._parse("chat", "--full-prompt").provider, "auto")

    def test_a_second_chat_provider_never_replaces_the_first(self):
        # chat codex --full-prompt claude must be rejected, not become claude.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self._parse("chat", "codex", "--full-prompt", "claude")
        self.assertEqual(raised.exception.code, 2)

    def test_an_unknown_extra_argument_is_still_an_error(self):
        for argv in (
            ("run", "codex", "--execute", "a", "b"),
            ("run", "codex", "--bogus"),
            ("chat", "--full-prompt", "not-a-provider"),
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self._parse(*argv)
                self.assertEqual(raised.exception.code, 2)


class ChatReplTests(unittest.TestCase):
    """`chat`: a line-oriented front end over run_command(), so everything
    below drives it with scripted input and a faked provider subprocess."""

    CLAUDE_STDOUT = (
        '{"type": "system", "subtype": "init", "session_id": "s1"}\n'
        '{"type": "result", "session_id": "s1", "result": "all done", '
        '"usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 500}, '
        '"total_cost_usd": 0.01, "is_error": false}\n'
    )

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)
        self.provider_inputs = []

    def _fake_run(self, command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
        self.provider_inputs.append((command[0], kwargs.get("input", "")))
        return subprocess.CompletedProcess(command, returncode=0, stdout=self.CLAUDE_STDOUT, stderr="")

    def _chat(self, lines, **overrides):
        args = hb.build_parser().parse_args(["chat", "claude"])
        for key, value in overrides.items():
            setattr(args, key, value)
        feed = iter(lines)

        def input_fn(prompt):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError

        output = []
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
            hb.subprocess, "run", side_effect=self._fake_run
        ):
            code = hb.chat_command(args, input_fn=input_fn, out=output.append)
        return code, "\n".join(output)

    def test_parser_defaults(self):
        parser = hb.build_parser()
        args = hb.parse_cli_args(parser, ["chat"])
        self.assertEqual(args.provider, "auto")
        self.assertTrue(args.auto_fallback)
        self.assertFalse(args.full_prompt)
        self.assertFalse(hb.parse_cli_args(parser, ["chat", "--no-auto-fallback"]).auto_fallback)

    def test_a_message_runs_the_provider_and_shows_reply_and_token_line(self):
        code, output = self._chat(["fix the bug", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.provider_inputs), 1)
        self.assertEqual(self.provider_inputs[0][0], "claude")
        self.assertIn("fix the bug", self.provider_inputs[0][1])
        self.assertIn("[claude] exit 0 - ok", output)
        self.assertIn("all done", output)
        self.assertIn("tokens: sent ~", output)
        self.assertIn("reported in 120, out 30, cached 500", output)
        self.assertIn("cost $0.01", output)

    def test_the_result_is_recorded_in_state_like_any_other_run(self):
        self._chat(["hello", "/exit"])
        history = hb.load_state()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["provider"], "claude")

    def test_second_turn_resumes_the_session_and_sends_a_smaller_prompt(self):
        self._chat(["first", "second", "/exit"])
        self.assertEqual(len(self.provider_inputs), 2)
        self.assertLess(len(self.provider_inputs[1][1]), len(self.provider_inputs[0][1]))

    def test_totals_are_printed_on_exit_and_by_slash_tokens(self):
        _, output = self._chat(["one", "/tokens", "/exit"])
        self.assertIn("this session: 1 run(s), sent ~", output)
        self.assertGreaterEqual(output.count("this session:"), 2)

    def test_eof_ends_the_chat_cleanly(self):
        code, output = self._chat(["hello"])
        self.assertEqual(code, 0)
        self.assertIn("this session: 1 run(s)", output)

    def test_blank_lines_are_ignored(self):
        _, output = self._chat(["", "   ", "/exit"])
        self.assertEqual(self.provider_inputs, [])
        self.assertIn("this session: 0 run(s)", output)

    def test_trailing_backslash_continues_the_message(self):
        self._chat(["line one \\", "line two", "/exit"])
        self.assertEqual(len(self.provider_inputs), 1)
        sent = self.provider_inputs[0][1]
        self.assertIn("line one \nline two", sent)

    def test_eof_in_the_middle_of_a_multiline_message_sends_nothing_and_ends_the_chat(self):
        code, output = self._chat(["delete temporary \\"])
        self.assertEqual(code, 0)
        self.assertEqual(self.provider_inputs, [])
        self.assertIn("this session: 0 run(s)", output)

    def test_ctrl_c_while_typing_discards_the_message_and_returns_to_the_prompt(self):
        args = hb.build_parser().parse_args(["chat", "claude"])
        script = iter(["delete temporary \\", KeyboardInterrupt, "hello", "/exit"])

        def input_fn(prompt):
            item = next(script)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        out = []
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
            hb.subprocess, "run", side_effect=self._fake_run
        ):
            hb.chat_command(args, input_fn=input_fn, out=out.append)
        self.assertEqual(len(self.provider_inputs), 1)
        sent = self.provider_inputs[0][1]
        self.assertIn("hello", sent)
        self.assertNotIn("delete temporary", sent)

    def test_ctrl_c_while_a_provider_runs_reports_the_turn_as_not_recorded_and_keeps_chatting(self):
        args = hb.build_parser().parse_args(["chat", "claude"])
        feed = iter(["first", "/exit"])
        calls = []

        def interrupting_run_command(ns):
            calls.append(ns.prompt)
            raise KeyboardInterrupt

        out = []
        with mock.patch.object(hb, "run_command", side_effect=interrupting_run_command):
            code = hb.chat_command(args, input_fn=lambda prompt: next(feed), out=out.append)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["first"])
        joined = "\n".join(out)
        self.assertIn("this turn was not recorded", joined)
        self.assertNotIn("background", joined)

    def test_unknown_slash_command_does_not_call_a_provider(self):
        _, output = self._chat(["/nope", "/exit"])
        self.assertIn("unknown command /nope", output)
        self.assertEqual(self.provider_inputs, [])

    def test_provider_command_validates_and_switches(self):
        _, output = self._chat(["/provider vim", "/provider codex", "/exit"])
        self.assertIn("unknown provider 'vim'", output)
        self.assertIn("provider: codex", output)

    def test_fallback_and_full_toggles(self):
        session = hb.ChatSession("auto", True, False, None, 0)
        lines = []
        session.handle_command("/fallback off", lines.append)
        session.handle_command("/full on", lines.append)
        self.assertFalse(session.auto_fallback)
        self.assertTrue(session.full_prompt)
        self.assertEqual(lines, ["fallback: off", "full: on"])
        session.handle_command("/full maybe", lines.append)
        self.assertIn("usage: /full [on|off]", lines)
        self.assertTrue(session.full_prompt)

    def test_exit_and_quit_both_end_the_chat(self):
        session = hb.ChatSession("auto", True, False, None, 0)
        self.assertFalse(session.handle_command("/exit", lambda _: None))
        self.assertFalse(session.handle_command("/quit", lambda _: None))
        self.assertTrue(session.handle_command("/help", lambda _: None))

    def test_a_run_that_records_nothing_surfaces_its_output(self):
        session = hb.ChatSession("claude", True, False, None, 0)

        def failing_run_command(args):
            print("error: another `run` is already in progress")
            return 75

        lines = []
        with mock.patch.object(hb, "run_command", side_effect=failing_run_command):
            code = session.run_turn("hi", lines.append)
        self.assertEqual(code, 75)
        self.assertIn("already in progress", "\n".join(lines))
        self.assertEqual(session.turns, 0)

    def test_an_allow_all_run_warns_on_every_turn(self):
        # run_provider()'s own warning is swallowed into the turn's stdout
        # buffer once a record exists, so the chat has to repeat it per run.
        session = hb.ChatSession("agy", False, False, None, 0)
        base = {"provider": "agy", "exit_code": 0, "handoff_needed": False, "reason": "none", "final_text": "hi"}
        cases = [({**base, "permissions": "allow-all"}, True), (dict(base), False)]
        for record, expect_warning in cases:
            lines = []
            with mock.patch.object(hb, "run_command", return_value=0), mock.patch.object(
                hb, "load_state", side_effect=[{"history": []}, {"history": [record]}]
            ):
                session.run_turn("go", lines.append)
            self.assertEqual(any("auto-approved" in line for line in lines), expect_warning)

    def test_full_flag_reaches_the_prompt(self):
        Path("docs").mkdir()
        Path("docs/shared-agent-contract.md").write_text("CUSTOM_CONTRACT_MARKER", encoding="utf-8")
        self._chat(["hello", "/exit"], full_prompt=True)
        self.assertIn("CUSTOM_CONTRACT_MARKER", self.provider_inputs[0][1])


class StatsTests(unittest.TestCase):
    @staticmethod
    def _record(provider, handoff, reason=None, usage=None, duration=None):
        record = {
            "provider": provider,
            "handoff_needed": handoff,
            "reason": reason or ("quota: matched quota signal" if handoff else "none: no handoff signal detected"),
            "usage": usage,
        }
        if duration is not None:
            record["duration_seconds"] = duration
        return record

    def test_empty_history(self):
        stats = hb.compute_stats([])
        self.assertEqual(stats["runs"], 0)
        self.assertEqual(stats["providers"], {})
        self.assertIn("No runs recorded", hb.format_stats(stats))

    def test_counts_reasons_tokens_and_time_per_provider(self):
        history = [
            self._record("codex", True, usage={"input_tokens": 1000, "output_tokens": 50}, duration=10.0),
            self._record("claude", False, usage={"input_tokens": 200, "output_tokens": 30, "cache_read_input_tokens": 900}, duration=20.0),
            self._record("claude", False, usage={"input_tokens": 100, "output_tokens": 10}, duration=5.0),
        ]
        stats = hb.compute_stats(history)
        codex, claude = stats["providers"]["codex"], stats["providers"]["claude"]
        self.assertEqual((codex["runs"], codex["ok"], codex["handoffs"]), (1, 0, 1))
        self.assertEqual(codex["reasons"], {"quota": 1})
        self.assertEqual((claude["runs"], claude["ok"], claude["handoffs"]), (2, 2, 0))
        self.assertEqual(claude["tokens"], {"input": 300, "output": 40, "cached": 900})
        self.assertEqual(claude["seconds"], 25.0)

    def test_per_ok_run_figures_include_the_spend_of_failed_runs(self):
        history = [
            self._record("codex", True, usage={"input_tokens": 900, "output_tokens": 100}, duration=30.0),
            self._record("codex", False, usage={"input_tokens": 80, "output_tokens": 20}, duration=10.0),
        ]
        codex = hb.compute_stats(history)["providers"]["codex"]
        # 1,100 tokens and 40s were spent to get 1 ok run.
        self.assertEqual(codex["tokens_per_ok_run"], 1100)
        self.assertEqual(codex["seconds_per_ok_run"], 40.0)

    def test_per_ok_run_is_none_without_an_ok_run_or_without_data(self):
        only_failed = hb.compute_stats([self._record("codex", True, usage={"input_tokens": 5, "output_tokens": 5}, duration=1.0)])
        self.assertIsNone(only_failed["providers"]["codex"]["tokens_per_ok_run"])
        self.assertIsNone(only_failed["providers"]["codex"]["seconds_per_ok_run"])
        no_data = hb.compute_stats([self._record("codex", False)])
        self.assertIsNone(no_data["providers"]["codex"]["tokens_per_ok_run"])
        self.assertIsNone(no_data["providers"]["codex"]["seconds_per_ok_run"])

    def test_records_from_before_duration_existed_do_not_break_the_time_figures(self):
        history = [self._record("claude", False), self._record("claude", False, duration=4.0)]
        claude = hb.compute_stats(history)["providers"]["claude"]
        self.assertEqual(claude["runs_timed"], 1)
        self.assertEqual(claude["seconds"], 4.0)

    def test_unmeasured_ok_runs_do_not_dilute_the_per_ok_run_figures(self):
        # Found in review: an old ok record with no duration plus a new ok run
        # of 4s used to print "2.0s per ok run" -- a number with no
        # measurement behind it. The figure now uses only measured runs, and
        # is flagged partial.
        history = [
            self._record("claude", False, usage=None),
            self._record("claude", False, usage={"input_tokens": 100, "output_tokens": 20}, duration=4.0),
        ]
        claude = hb.compute_stats(history)["providers"]["claude"]
        self.assertEqual(claude["seconds_per_ok_run"], 4.0)
        self.assertEqual(claude["tokens_per_ok_run"], 120)
        self.assertFalse(claude["seconds_complete"])
        self.assertFalse(claude["tokens_complete"])
        text = hb.format_stats(hb.compute_stats(history))
        self.assertIn("(partial: 1/2 runs)", text)

    def test_complete_data_is_not_marked_partial(self):
        history = [self._record("codex", False, usage={"input_tokens": 10, "output_tokens": 5}, duration=2.0)]
        codex = hb.compute_stats(history)["providers"]["codex"]
        self.assertTrue(codex["tokens_complete"])
        self.assertTrue(codex["seconds_complete"])
        self.assertNotIn("(partial:", hb.format_stats(hb.compute_stats(history)))

    def test_a_failed_run_with_no_usage_makes_the_token_figure_partial_not_exact(self):
        history = [
            self._record("codex", True, usage=None, duration=30.0),
            self._record("codex", False, usage={"input_tokens": 80, "output_tokens": 20}, duration=10.0),
        ]
        codex = hb.compute_stats(history)["providers"]["codex"]
        self.assertEqual(codex["tokens_per_ok_run"], 100)
        self.assertFalse(codex["tokens_complete"])
        self.assertTrue(codex["seconds_complete"])
        self.assertEqual(codex["seconds_per_ok_run"], 40.0)

    def test_only_records_linked_by_handoff_from_count_as_hops(self):
        linked_ok = dict(self._record("claude", False), handoff_from="run-1")
        linked_failed = dict(self._record("agy", True), handoff_from="run-2")
        history = [
            self._record("codex", True),
            linked_ok,
            self._record("claude", True),
            linked_failed,
            # Adjacent and on a different provider, but never linked: an
            # unrelated later request, not a takeover.
            self._record("codex", False),
        ]
        stats = hb.compute_stats(history)
        self.assertEqual(stats["handoff_hops"], 2)
        self.assertEqual(stats["handoff_hops_ok"], 1)

    def test_records_from_before_handoff_from_existed_are_not_counted_as_hops(self):
        stats = hb.compute_stats([self._record("codex", True), self._record("claude", False)])
        self.assertEqual(stats["handoff_hops"], 0)
        self.assertIn("no linked hops", hb.format_stats(stats))

    def _run_claude(self, state):
        def fake_run(command, **kwargs):
            if command[0] == "git":
                return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                command, returncode=0, stderr="",
                stdout='{"type": "result", "session_id": "s", "result": "ok", "is_error": false}\n',
            )
        args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=False,
                                     timeout_seconds=0, model=None, instruction_type="continue")
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
            hb.subprocess, "run", side_effect=fake_run
        ), contextlib.redirect_stdout(io.StringIO()):
            hb.run_provider("claude", args, state)

    def test_run_provider_links_a_takeover_to_the_run_whose_handoff_it_resolves(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                state = {"task": "t", "status": "handoff_needed", "last_provider": "codex",
                         "history": [{"provider": "codex", "handoff_needed": True, "run_dir": ".handoff/runs/codex-1"}]}
                self._run_claude(state)
                self.assertEqual(state["history"][-1]["handoff_from"], ".handoff/runs/codex-1")
            finally:
                os.chdir(orig)

    def test_run_provider_does_not_link_a_retry_by_the_same_provider_or_a_fresh_start(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                retry = {"task": "t", "status": "handoff_needed", "last_provider": "claude",
                         "history": [{"provider": "claude", "handoff_needed": True, "run_dir": "r"}]}
                self._run_claude(retry)
                self.assertNotIn("handoff_from", retry["history"][-1])
                fresh = {"task": "t", "status": "ready", "last_provider": "codex",
                         "history": [{"provider": "codex", "handoff_needed": False, "run_dir": "r"}]}
                self._run_claude(fresh)
                self.assertNotIn("handoff_from", fresh["history"][-1])
                empty = {"task": "t", "status": "ready"}
                self._run_claude(empty)
                self.assertNotIn("handoff_from", empty["history"][-1])
            finally:
                os.chdir(orig)

    def test_auto_fallback_records_the_link_on_the_second_hop(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                def fake_run(command, **kwargs):
                    if command[0] == "git":
                        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
                    if command[0] == "codex":
                        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="Error: 429 too many requests")
                    return subprocess.CompletedProcess(
                        command, returncode=0, stderr="",
                        stdout='{"type": "result", "session_id": "s", "result": "ok", "is_error": false}\n',
                    )
                args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=True,
                                             timeout_seconds=0, model=None, instruction_type="continue")
                state = {"task": "t", "primary_provider": "codex", "status": "ready"}
                with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
                    hb.subprocess, "run", side_effect=fake_run
                ), contextlib.redirect_stdout(io.StringIO()):
                    hb.run_provider("codex", args, state)
                first, second = state["history"]
                self.assertNotIn("handoff_from", first)
                self.assertEqual(second["handoff_from"], first["run_dir"])
                stats = hb.compute_stats(state["history"])
                self.assertEqual((stats["handoff_hops"], stats["handoff_hops_ok"]), (1, 1))
            finally:
                os.chdir(orig)

    def test_text_output_states_what_ok_means_and_applies_no_price(self):
        text = hb.format_stats(hb.compute_stats([self._record("codex", False, usage={"input_tokens": 10, "output_tokens": 2}, duration=1.0)]))
        self.assertIn("not proof the work is correct", text)
        self.assertIn("no prices applied", text)
        self.assertIn("in 10", text)
        self.assertNotIn("$", text)

    def test_stats_json_flag_prints_machine_readable_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".handoff").mkdir()
            state = {"history": [self._record("codex", False, usage={"input_tokens": 3, "output_tokens": 1}, duration=2.5)]}
            (workspace / ".handoff" / "state.json").write_text(json.dumps(state), encoding="utf-8")
            bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
            result = subprocess.run(
                [sys.executable, str(bridge_script), "--workspace", str(workspace), "stats", "--json"],
                text=True, capture_output=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["providers"]["codex"]["seconds"], 2.5)

    def test_run_provider_records_the_duration_of_each_run(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                def fake_run(command, **kwargs):
                    if command[0] == "git":
                        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
                    return subprocess.CompletedProcess(
                        command, returncode=0, stderr="",
                        stdout='{"type": "result", "session_id": "s", "result": "ok", "is_error": false}\n',
                    )
                args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=False,
                                             timeout_seconds=0, model=None, instruction_type="continue")
                state = {"task": "t", "primary_provider": "claude", "status": "ready"}
                with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"), mock.patch.object(
                    hb.subprocess, "run", side_effect=fake_run
                ):
                    hb.run_provider("claude", args, state)
                duration = state["history"][0]["duration_seconds"]
                self.assertIsInstance(duration, float)
                self.assertGreaterEqual(duration, 0.0)
                self.assertIn("- Duration:", hb.CURRENT_FILE.read_text(encoding="utf-8"))
            finally:
                os.chdir(orig)

    def test_chat_stats_command_prints_the_summary(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                hb.HANDOFF_DIR.mkdir()
                hb.save_state({"history": [self._record("claude", False, duration=1.0)]})
                session = hb.ChatSession("auto", True, False, None, 0)
                lines = []
                self.assertTrue(session.handle_command("/stats", lines.append))
                self.assertIn("claude: 1 run(s), 1 ok, 0 handoff", "\n".join(lines))
            finally:
                os.chdir(orig)


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


class NewCliQuerySubcommandsTests(unittest.TestCase):
    """check-update / next-provider / resolve-auto-provider: thin CLI
    wrappers added so handoff_webui.py can reach check_for_update()/
    next_available_provider()/choose_auto_provider() through the same
    subprocess boundary every other bridge-invoking consumer already uses,
    instead of importing and calling them in-process (a structure-audit
    finding)."""

    def _stdout_of(self, func, args) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = func(args)
        self.assertEqual(exit_code, 0)
        return buffer.getvalue().strip()

    def test_check_update_prints_check_for_update_result_as_json(self):
        fake_result = {"status": "current", "current_version": "1.2.3"}
        with mock.patch.object(hb, "check_for_update", return_value=fake_result):
            output = self._stdout_of(hb.check_update_command, mock.Mock())
        self.assertEqual(json.loads(output), fake_result)

    def test_next_provider_prints_the_next_available_provider(self):
        with mock.patch.object(hb, "next_available_provider", return_value="claude") as spy:
            output = self._stdout_of(hb.next_provider_command, mock.Mock(current="codex"))
        spy.assert_called_once_with("codex")
        self.assertEqual(output, "claude")

    def test_resolve_auto_provider_prints_choose_auto_providers_result(self):
        fake_state = {"primary_provider": "codex"}
        with mock.patch.object(hb, "load_state", return_value=fake_state), mock.patch.object(
            hb, "choose_auto_provider", return_value="codex"
        ) as spy:
            output = self._stdout_of(hb.resolve_auto_provider_command, mock.Mock())
        spy.assert_called_once_with(fake_state)
        self.assertEqual(output, "codex")

    def test_all_three_are_reachable_through_a_real_subprocess_invocation(self):
        # Not mocked: confirms the argparse wiring itself (subcommand name,
        # positional arg, --workspace chdir) actually works end to end, the
        # same shape handoff_webui.py's short_run() calls will use.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent.parent / "handoff_bridge.py"),
                 "--workspace", tmp, "resolve-auto-provider"],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "codex")


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

    def test_pins_utf8_encoding_not_the_locale_default(self):
        # Regression coverage for a real crash (2026-08-14): without an
        # explicit encoding, subprocess.run() falls back to
        # locale.getpreferredencoding() -- cp949, not UTF-8, on a
        # Korean-locale Windows machine -- to decode a git/gh call's
        # stdout/stderr, which can easily contain non-ASCII characters.
        with mock.patch.object(
            hb.subprocess, "run", return_value=subprocess.CompletedProcess(["fake"], returncode=0, stdout="", stderr="")
        ) as run_spy:
            hb.short_run(["fake"])
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")

    def test_cwd_is_passed_through(self):
        # Added so short_run() can absorb handoff_desktop.py's own
        # subprocess wrapper (a structure-audit finding: several files
        # reimplemented this same wrapper without the FileNotFoundError->127
        # normalization) -- must not silently drop a caller-supplied cwd.
        with mock.patch.object(
            hb.subprocess, "run", return_value=subprocess.CompletedProcess(["fake"], returncode=0, stdout="", stderr="")
        ) as run_spy:
            hb.short_run(["fake"], cwd="/some/dir")
        self.assertEqual(run_spy.call_args.kwargs["cwd"], "/some/dir")

    def test_none_timeout_means_no_timeout(self):
        with mock.patch.object(
            hb.subprocess, "run", return_value=subprocess.CompletedProcess(["fake"], returncode=0, stdout="", stderr="")
        ) as run_spy:
            hb.short_run(["fake"], timeout=None)
        self.assertIsNone(run_spy.call_args.kwargs["timeout"])


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


class RunProviderSubprocessEncodingTests(unittest.TestCase):
    """Regression coverage for a real crash (2026-08-14): run_provider()'s
    subprocess.run() call had no explicit `encoding`, so Python fell back
    to locale.getpreferredencoding() to encode `input=prompt` for the
    provider's stdin -- cp949 on a Korean-locale Windows machine, not
    UTF-8. `prompt` folds in this project's own docs
    (docs/shared-agent-contract.md, docs/verification-playbook.md), which
    contain literal em dashes -- cp949 can't encode U+2014, so a plain
    "테스트" prompt still crashed with UnicodeEncodeError before this
    fix, reproduced directly on a real cp949-locale Windows machine (not
    just inferred): even the simplest possible run crashed, because the
    offending character came from the *folded-in doc content*, not the
    user's own text."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def test_provider_subprocess_call_pins_utf8_encoding(self):
        args = hb.argparse.Namespace(
            prompt="hello",
            prompt_file=None,
            execute=True,
            auto_fallback=False,
            timeout_seconds=30,
            model=None,
            instruction_type="continue",
        )
        state = {"task": "hello", "primary_provider": "codex", "status": "ready"}
        stdout = (
            '{"type": "system", "subtype": "init", "session_id": "s"}\n'
            '{"type": "result", "session_id": "s", "result": "ok", '
            '"total_cost_usd": 0.0, "is_error": false}\n'
        )
        with mock.patch.object(
            hb.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["codex"], returncode=0, stdout=stdout, stderr=""),
        ) as run_spy:
            hb.run_provider("codex", args, state)
        provider_call = next(call for call in run_spy.call_args_list if call.args[0][0] == "codex")
        self.assertEqual(provider_call.kwargs["encoding"], "utf-8")
        self.assertEqual(provider_call.kwargs["errors"], "replace")

    def test_a_prompt_containing_an_em_dash_does_not_raise_on_a_real_subprocess_call(self):
        # Not mocked: a real subprocess.run() with a real child process
        # (`cmd /c more` on Windows, `cat` on POSIX) reading real stdin --
        # the exact boundary that crashed. This em dash is standing in for
        # the ones already present in docs/shared-agent-contract.md and
        # docs/verification-playbook.md, which build_prompt() always
        # folds into every real prompt regardless of user input.
        command = ["cmd", "/c", "more"] if os.name == "nt" else ["cat"]
        args = hb.argparse.Namespace(
            prompt="hello — world",
            prompt_file=None,
            execute=True,
            auto_fallback=False,
            timeout_seconds=15,
            model=None,
            instruction_type="continue",
        )
        state = {"task": "hello", "primary_provider": "codex", "status": "ready"}
        with mock.patch.object(hb, "provider_command", return_value=command):
            exit_code = hb.run_provider("codex", args, state)
        # A crash here would surface as an uncaught UnicodeEncodeError
        # propagating out of run_provider() -- reaching this assertion at
        # all is the actual regression check.
        self.assertIsInstance(exit_code, int)


class NextProviderTests(unittest.TestCase):
    """Phase 5: next_provider() replaces the old other_provider() binary
    toggle -- docs/provider-extensibility.md's "The Current Code Assumes
    Exactly Two Providers" finding, resolved by walking PROVIDERS in order
    instead of a two-way ternary."""

    def test_walks_to_the_next_provider_in_order(self):
        self.assertEqual(hb.next_provider("codex"), "claude")
        self.assertEqual(hb.next_provider("claude"), "agy")

    def test_wraps_around_at_the_end(self):
        self.assertEqual(hb.next_provider("agy"), "codex")

    def test_skips_entries_already_in_tried(self):
        # codex -> claude is next in order, but claude is already tried,
        # so this must skip to agy instead.
        self.assertEqual(hb.next_provider("codex", tried={"claude"}), "agy")

    def test_falls_back_to_current_when_every_provider_is_exhausted(self):
        self.assertEqual(hb.next_provider("codex", tried={"codex", "claude", "agy"}), "codex")

    def test_current_itself_is_always_excluded_even_if_not_in_tried(self):
        # A 2-provider cycle: excluding claude and agy leaves only
        # codex itself, which must never be returned as its own "next".
        self.assertEqual(hb.next_provider("codex", tried={"claude", "agy"}), "codex")


class NextAvailableProviderTests(unittest.TestCase):
    """Regression coverage (found in review, real gap only reachable once
    PROVIDERS grew past two entries in Phase 5): a single-hop auto-fallback
    used to pick next_provider() blindly, with no regard for whether that
    candidate's CLI was actually installed -- a codex failure could land on
    an uninstalled claude and never reach an installed agy sitting right
    after it in PROVIDERS order."""

    def test_skips_an_uninstalled_provider_to_reach_an_installed_one(self):
        # codex fails -> naive next_provider() would say "claude" -- but
        # only codex and agy are "installed" here, so this must skip
        # past claude to agy instead.
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name in ("codex", "agy") and f"/usr/bin/{name}"):
            self.assertEqual(hb.next_available_provider("codex"), "agy")

    def test_still_respects_tried_on_top_of_availability(self):
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):  # everything "installed"
            self.assertEqual(hb.next_available_provider("codex", tried={"claude"}), "agy")

    def test_falls_back_to_current_when_nothing_else_is_installed(self):
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "codex" and "/usr/bin/codex"):
            self.assertEqual(hb.next_available_provider("codex"), "codex")

    def test_matches_plain_next_provider_when_everything_is_installed(self):
        with mock.patch.object(hb.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(hb.next_available_provider("codex"), hb.next_provider("codex"))


class ProviderCommandCodexTests(unittest.TestCase):
    """Regression coverage for a real bug a user hit in production
    (2026-09-03): codex exec refuses to run at all ("Not inside a
    trusted directory and --skip-git-repo-check was not specified") in
    any workspace that isn't itself a git repo -- which every workspace
    this bridge auto-creates is not, by default. provider_command() had
    no test coverage at all for codex/claude before this (only agy,
    see ProviderCommandAgyTests below) -- exactly the kind of gap
    that let a real CLI flag requirement change through unnoticed."""

    def test_first_call_has_no_resume_flag_and_skips_the_git_repo_check(self):
        state = {"sessions": {"codex": None}}
        command = hb.provider_command("codex", state)
        self.assertEqual(command[0], "codex")
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertNotIn("resume", command)
        self.assertEqual(command[-1], "-")  # prompt travels via stdin

    def test_a_prior_session_adds_resume_and_still_skips_the_git_repo_check(self):
        state = {"sessions": {"codex": "fake-codex-session"}}
        command = hb.provider_command("codex", state)
        self.assertIn("resume", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertEqual(command[-2:], ["fake-codex-session", "-"])

    def test_model_is_passed_through_on_a_first_call(self):
        state = {"sessions": {"codex": None}}
        command = hb.provider_command("codex", state, model="gpt-5-codex")
        idx = command.index("--model")
        self.assertEqual(command[idx + 1], "gpt-5-codex")

    def test_model_is_passed_through_on_a_resumed_call(self):
        state = {"sessions": {"codex": "fake-codex-session"}}
        command = hb.provider_command("codex", state, model="gpt-5-codex")
        idx = command.index("--model")
        self.assertEqual(command[idx + 1], "gpt-5-codex")


class ProviderCommandClaudeTests(unittest.TestCase):
    """Regression coverage for a real bug a user hit in production
    (2026-09-03): "Error: When using --print, --output-format=stream-json
    requires --verbose" -- a newer claude CLI build hard-requires
    --verbose alongside -p/--output-format=stream-json. See
    ProviderCommandCodexTests' own docstring for why this class exists
    at all (no prior codex/claude coverage here)."""

    def test_first_call_has_no_resume_flag_and_includes_verbose(self):
        state = {"sessions": {"claude": None}}
        command = hb.provider_command("claude", state)
        self.assertEqual(command[0], "claude")
        self.assertIn("-p", command)
        self.assertIn("--verbose", command)
        idx = command.index("--output-format")
        self.assertEqual(command[idx + 1], "stream-json")
        self.assertNotIn("--resume", command)

    def test_a_prior_session_adds_resume_and_still_includes_verbose(self):
        state = {"sessions": {"claude": "fake-claude-session"}}
        command = hb.provider_command("claude", state)
        idx = command.index("--resume")
        self.assertEqual(command[idx + 1], "fake-claude-session")
        self.assertIn("--verbose", command)

    def test_model_is_passed_through_on_a_first_call(self):
        state = {"sessions": {"claude": None}}
        command = hb.provider_command("claude", state, model="claude-opus")
        idx = command.index("--model")
        self.assertEqual(command[idx + 1], "claude-opus")

    def test_model_is_passed_through_on_a_resumed_call(self):
        state = {"sessions": {"claude": "fake-claude-session"}}
        command = hb.provider_command("claude", state, model="claude-opus")
        idx = command.index("--model")
        self.assertEqual(command[idx + 1], "claude-opus")


class GitSnapshotTests(unittest.TestCase):
    """git_snapshot() is folded into every single build_prompt() call --
    keep it cheap, and never leak git's own inconsistent failure-mode text
    into every prompt. Regression (real-world report, 2026-09-04): outside
    a git repository (the default for every workspace this bridge
    auto-creates -- see provider_command()'s `--skip-git-repo-check`),
    `git diff --stat` doesn't fail the way `git status --short` does --
    it falls back to `--no-index` two-path-compare mode, finds no paths
    were given, and dumps its own ~7KB usage/help text to stderr (exit
    129) -- confirmed against a real installed git binary. This was
    previously included here verbatim on *every single prompt* in every
    non-git workspace."""

    def test_clean_repo_reports_no_diff(self):
        with mock.patch("handoff_bridge.short_run") as mock_run:
            mock_run.side_effect = [(0, "", ""), (0, "", "")]
            snapshot = hb.git_snapshot()
        self.assertIn("(clean)", snapshot)
        self.assertIn("(no diff)", snapshot)

    def test_dirty_repo_reports_status_and_diff(self):
        with mock.patch("handoff_bridge.short_run") as mock_run:
            mock_run.side_effect = [(0, " M foo.py\n", ""), (0, " foo.py | 2 +-\n", "")]
            snapshot = hb.git_snapshot()
        self.assertIn("M foo.py", snapshot)
        self.assertIn("foo.py | 2 +-", snapshot)

    def test_non_git_workspace_skips_diff_entirely_instead_of_leaking_git_help_text(self):
        with mock.patch("handoff_bridge.short_run") as mock_run:
            mock_run.return_value = (
                128,
                "",
                "fatal: not a git repository (or any of the parent directories): .git\n",
            )
            snapshot = hb.git_snapshot()
        # The whole point of the fix: once `git status` already proves
        # there's no repository, `git diff --stat` must never run at all
        # -- not just have its output discarded after the fact.
        self.assertEqual(mock_run.call_count, 1)
        self.assertIn("not a git repository", snapshot)
        self.assertIn("(not a git repository)", snapshot)

    def test_diff_failure_inside_a_real_repo_does_not_leak_raw_stderr(self):
        with mock.patch("handoff_bridge.short_run") as mock_run:
            mock_run.side_effect = [(0, "", ""), (1, "", "some unexpected git internal error")]
            snapshot = hb.git_snapshot()
        self.assertIn("(unavailable)", snapshot)
        self.assertNotIn("unexpected git internal error", snapshot)

    def test_real_git_binary_outside_a_repo_stays_short(self):
        # No mocking -- exercises the actual documented bug against a real
        # installed git, in a real non-git directory (the same condition
        # every auto-created workspace starts in), not just the mocked
        # shape assumed above.
        with tempfile.TemporaryDirectory() as tmp:
            orig_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                snapshot = hb.git_snapshot()
            finally:
                os.chdir(orig_cwd)
        self.assertNotIn("usage: git diff", snapshot)
        self.assertLess(len(snapshot), 500, msg=snapshot)


class ProviderCommandAgyTests(unittest.TestCase):
    def test_first_call_in_a_workspace_has_no_conversation_flag(self):
        state = {"sessions": {"agy": None}}
        command = hb.provider_command("agy", state)
        self.assertEqual(command[0], "agy")
        self.assertEqual(command[1:3], ["--output-format", "json"])
        self.assertNotIn("--conversation", command)

    def test_a_prior_clean_run_resumes_that_conversation_by_id(self):
        state = {"sessions": {"agy": "941d1baa-2dcf-4f2a-a9c9-d33c5c2e36ea"}}
        command = hb.provider_command("agy", state)
        idx = command.index("--conversation")
        self.assertEqual(command[idx + 1], "941d1baa-2dcf-4f2a-a9c9-d33c5c2e36ea")

    def test_model_is_passed_through(self):
        state = {"sessions": {"agy": None}}
        command = hb.provider_command("agy", state, model="claude-sonnet-5-5-medium")
        idx = command.index("--model")
        self.assertEqual(command[idx + 1], "claude-sonnet-5-5-medium")

    def test_prompt_travels_via_stdin_not_a_print_flag(self):
        # Verified against agy 1.4.8: piped stdin alone triggers print mode,
        # while `-p` followed by another flag swallows that flag as its
        # prompt and exits 2.
        command = hb.provider_command("agy", {"sessions": {"agy": None}})
        for flag in ("-p", "--print", "--prompt"):
            self.assertNotIn(flag, command)

    def test_agy_gets_a_runtime_note_telling_it_to_always_reply_in_text(self):
        for state in ({"task": "t", "sessions": {}}, {"task": "t", "sessions": {"agy": "c-1"}}):
            prompt = hb.build_prompt("agy", state, "hello")
            self.assertIn("## Runtime Note (agy)", prompt)
            self.assertIn("never end your", prompt)

    def test_agy_is_not_told_to_edit_current_md_itself(self):
        prompt = hb.build_prompt("agy", {"task": "t", "sessions": {}}, "hello")
        self.assertIn("do not edit that file yourself", prompt)
        self.assertNotIn("Before stopping, update `.handoff/current.md`", prompt)

    def test_compact_agy_prompt_has_no_instruction_to_write_current_md(self):
        # Found in review: the Required Behavior bullet said "do not edit"
        # while the Working Rules summary still said "append a concise entry
        # to .handoff/current.md" -- a contradiction the model resolved by
        # trying (and failing) to write.
        for state in ({"task": "t", "sessions": {}}, {"task": "t", "sessions": {"agy": "c1"}}):
            prompt = hb.build_prompt("agy", state, "hello", reason="quota: x")
            for phrase in ("append a concise entry", "Before stopping, update", "update `.handoff/current.md`"):
                self.assertNotIn(phrase, prompt, msg=phrase)

    def test_full_agy_prompt_ends_with_an_override_after_the_real_docs(self):
        prompt = hb.build_prompt("agy", {"task": "t", "sessions": {}}, "hello", full=True)
        docs_at = prompt.index("## Verification Playbook")
        override_at = prompt.index("## Final Instruction (agy)")
        self.assertGreater(override_at, docs_at)
        self.assertIn("do not do that", prompt[override_at:])
        # It is the last section of the prompt, so it is the last word.
        self.assertNotIn("\n## ", prompt[override_at + 5 :])

    def test_other_providers_prompts_are_unchanged_by_the_agy_overrides(self):
        for provider in ("codex", "claude"):
            compact = hb.build_prompt(provider, {"task": "t", "sessions": {}}, "hello")
            full = hb.build_prompt(provider, {"task": "t", "sessions": {}}, "hello", full=True)
            self.assertIn("append a concise entry to `.handoff/current.md`", compact)
            self.assertNotIn("Final Instruction", compact + full)
            self.assertNotIn("@@FINISH@@", compact)

    def test_protocol_summary_placeholder_is_always_resolved(self):
        for provider in hb.PROVIDERS:
            self.assertNotIn("@@FINISH@@", hb.protocol_summary_for(provider))

    def test_codex_and_claude_still_update_current_md_themselves(self):
        for provider in ("codex", "claude"):
            prompt = hb.build_prompt(provider, {"task": "t", "sessions": {}}, "hello")
            self.assertIn("Before stopping, update `.handoff/current.md`", prompt)

    def test_other_providers_get_no_runtime_note(self):
        self.assertNotIn("Runtime Note", hb.build_prompt("codex", {"task": "t", "sessions": {}}, "hello"))

    def test_no_permission_bypass_flag_by_default(self):
        # --dangerously-skip-permissions approves every tool with no
        # scoping; unless the user opts in, the bridge leaves permissions to
        # agy's own settings.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hb.AGY_ALLOW_WRITES_ENV, None)
            command = hb.provider_command("agy", {"sessions": {"agy": "id"}}, model="m")
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--sandbox", command)

    def test_opt_in_adds_the_bypass_flag_and_nothing_else(self):
        for value in ("1", "true", "YES", " on "):
            with mock.patch.dict(os.environ, {hb.AGY_ALLOW_WRITES_ENV: value}):
                command = hb.provider_command("agy", {"sessions": {"agy": None}})
            self.assertEqual(command, ["agy", "--output-format", "json", "--dangerously-skip-permissions"], msg=value)

    def test_other_values_do_not_enable_it(self):
        for value in ("", "0", "false", "no", "off", "maybe"):
            with mock.patch.dict(os.environ, {hb.AGY_ALLOW_WRITES_ENV: value}):
                self.assertFalse(hb.agy_allow_writes_enabled(), msg=value)
                self.assertNotIn(
                    "--dangerously-skip-permissions", hb.provider_command("agy", {"sessions": {"agy": None}})
                )

    def test_the_opt_in_only_affects_agy(self):
        with mock.patch.dict(os.environ, {hb.AGY_ALLOW_WRITES_ENV: "1"}):
            for provider in ("codex", "claude"):
                self.assertNotIn(
                    "--dangerously-skip-permissions",
                    hb.provider_command(provider, {"sessions": {provider: None}}),
                )

    def test_cli_flag_sets_the_environment_variable_for_the_process(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hb.AGY_ALLOW_WRITES_ENV, None)
            with tempfile.TemporaryDirectory() as tmp:
                orig = os.getcwd()
                try:
                    hb.main(["--workspace", tmp, "run", "agy", "--agy-allow-writes", "preview"])
                finally:
                    os.chdir(orig)
            self.assertTrue(hb.agy_allow_writes_enabled())

    def test_without_the_flag_main_leaves_the_environment_alone(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hb.AGY_ALLOW_WRITES_ENV, None)
            with tempfile.TemporaryDirectory() as tmp:
                orig = os.getcwd()
                try:
                    hb.main(["--workspace", tmp, "run", "agy", "preview"])
                finally:
                    os.chdir(orig)
            self.assertFalse(hb.agy_allow_writes_enabled())

    def test_runtime_note_switches_to_a_caution_when_everything_is_pre_approved(self):
        with mock.patch.dict(os.environ, {hb.AGY_ALLOW_WRITES_ENV: "1"}):
            prompt = hb.build_prompt("agy", {"task": "t", "sessions": {}}, "hello")
        self.assertIn("pre-approved", prompt)
        self.assertIn("destructive or irreversible", prompt)
        self.assertNotIn("is denied without a prompt", prompt)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hb.AGY_ALLOW_WRITES_ENV, None)
            default_prompt = hb.build_prompt("agy", {"task": "t", "sessions": {}}, "hello")
        self.assertIn("is denied without a prompt", default_prompt)

    def test_run_records_and_announces_the_elevated_permissions(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                def fake_run(command, **kwargs):
                    if command[0] == "git":
                        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
                    self.assertIn("--dangerously-skip-permissions", command)
                    return subprocess.CompletedProcess(
                        command, returncode=0, stderr="",
                        stdout='{"conversation_id": "c", "status": "SUCCESS", "response": "done"}',
                    )
                args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=False,
                                             timeout_seconds=0, model=None, instruction_type="continue")
                state = {"task": "t", "primary_provider": "agy", "status": "ready"}
                buffer = io.StringIO()
                with mock.patch.dict(os.environ, {hb.AGY_ALLOW_WRITES_ENV: "1"}), mock.patch.object(
                    hb.shutil, "which", return_value="/usr/bin/x"
                ), mock.patch.object(hb.subprocess, "run", side_effect=fake_run), contextlib.redirect_stdout(buffer):
                    hb.run_provider("agy", args, state)
                self.assertIn("WARNING: agy runs with every tool call auto-approved", buffer.getvalue())
                self.assertEqual(state["history"][0]["permissions"], "allow-all")
                self.assertIn("- Permissions: all tool calls auto-approved", hb.CURRENT_FILE.read_text(encoding="utf-8"))
            finally:
                os.chdir(orig)

    def test_default_runs_carry_no_permissions_field(self):
        orig = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                def fake_run(command, **kwargs):
                    if command[0] == "git":
                        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")
                    return subprocess.CompletedProcess(
                        command, returncode=0, stderr="",
                        stdout='{"conversation_id": "c", "status": "SUCCESS", "response": "done"}',
                    )
                args = hb.argparse.Namespace(prompt="hi", prompt_file=None, execute=True, auto_fallback=False,
                                             timeout_seconds=0, model=None, instruction_type="continue")
                state = {"task": "t", "primary_provider": "agy", "status": "ready"}
                with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(
                    hb.shutil, "which", return_value="/usr/bin/x"
                ), mock.patch.object(hb.subprocess, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                    os.environ.pop(hb.AGY_ALLOW_WRITES_ENV, None)
                    hb.run_provider("agy", args, state)
                self.assertNotIn("permissions", state["history"][0])
            finally:
                os.chdir(orig)

    def test_chat_banner_warns_when_enabled(self):
        args = hb.build_parser().parse_args(["chat", "agy"])
        out = []
        with mock.patch.dict(os.environ, {hb.AGY_ALLOW_WRITES_ENV: "1"}), mock.patch.object(
            hb.shutil, "which", return_value="/usr/bin/x"
        ):
            hb.chat_command(args, input_fn=lambda prompt: (_ for _ in ()).throw(EOFError), out=out.append)
        self.assertIn("WARNING: agy will run with every tool call auto-approved", "\n".join(out))


class SummarizeAgyTests(unittest.TestCase):
    ENVELOPE = {
        "conversation_id": "941d1baa-2dcf-4f2a-a9c9-d33c5c2e36ea",
        "status": "SUCCESS",
        "response": "PONG\n",
        "duration_seconds": 3.9,
        "num_turns": 1,
        "usage": {"input_tokens": 6858, "output_tokens": 6, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 6864},
    }

    def test_successful_envelope_is_parsed(self):
        summary = hb.summarize_agy(json.dumps(self.ENVELOPE), exit_code=0)
        self.assertEqual(summary["provider"], "agy")
        self.assertEqual(summary["final_text"], "PONG\n")
        self.assertEqual(summary["usage"]["total_tokens"], 6864)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["session_id"], "941d1baa-2dcf-4f2a-a9c9-d33c5c2e36ea")

    def test_usage_is_understood_by_the_chat_token_readout(self):
        summary = hb.summarize_agy(json.dumps(self.ENVELOPE), exit_code=0)
        self.assertEqual(hb.usage_token_counts(summary["usage"]), {"input": 6858, "output": 6, "cached": 0})

    def test_nonzero_exit_never_marks_a_conversation_resumable(self):
        summary = hb.summarize_agy(json.dumps(self.ENVELOPE), exit_code=1)
        self.assertIsNone(summary["session_id"])
        self.assertEqual(summary["final_text"], "PONG\n")

    def test_non_success_status_is_an_error_and_not_resumable(self):
        envelope = dict(self.ENVELOPE, status="ERROR", response="")
        summary = hb.summarize_agy(json.dumps(envelope), exit_code=0)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["status"], "ERROR")
        self.assertIsNone(summary["session_id"])

    def test_error_field_is_captured(self):
        summary = hb.summarize_agy(json.dumps({"status": "SUCCESS", "error": {"message": "boom"}}), exit_code=0)
        self.assertEqual(summary["errors"], [{"message": "boom"}])
        self.assertIsNone(summary["session_id"])

    def test_denied_actions_with_an_empty_response_become_a_tool_failure(self):
        envelope = dict(
            self.ENVELOPE,
            response="",
            denied_actions=[{"action": "write_file", "display_name": "WriteFile"}, {"action": "command"}],
        )
        summary = hb.summarize_agy(json.dumps(envelope), exit_code=0)
        self.assertEqual([e["type"] for e in summary["errors"]], ["tool_failure"])
        self.assertIn("command", summary["final_text"])
        self.assertIn("write_file", summary["final_text"])
        self.assertIn("permissions.allow", summary["final_text"])
        # The conversation itself is fine and may still be resumed.
        self.assertEqual(summary["session_id"], self.ENVELOPE["conversation_id"])

    def test_denied_actions_alongside_a_real_response_are_still_a_tool_failure(self):
        # Found in review: "I could not write the file, here is the diff"
        # means the file was NOT changed. Treating a non-empty reply as
        # success would stop auto-fallback with the work undone.
        envelope = dict(self.ENVELOPE, response="I could not write the file, here is the diff instead.",
                        denied_actions=[{"action": "write_file"}])
        stdout = json.dumps(envelope)
        summary = hb.summarize_agy(stdout, exit_code=0)
        self.assertEqual([e["type"] for e in summary["errors"]], ["tool_failure"])
        # The reply is preserved byte-for-byte for the next provider...
        self.assertEqual(summary["final_text"], envelope["response"])
        self.assertIn("write_file", summary["note"])
        self.assertEqual(summary["session_id"], self.ENVELOPE["conversation_id"])
        # ...and the run is classified as a handoff.
        needed, reason = hb.classify_handoff(0, stdout, "", summary)
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("tool_failure:"), msg=reason)

    def test_malformed_json_does_not_raise(self):
        summary = hb.summarize_agy("not json at all")
        self.assertEqual(summary["provider"], "agy")
        self.assertIsNone(summary["session_id"])
        self.assertEqual(summary["final_text"], "")

    def test_empty_stdout_does_not_raise(self):
        self.assertIsNone(hb.summarize_agy("")["session_id"])

    def test_a_json_array_top_level_does_not_raise(self):
        summary = hb.summarize_agy("[1, 2, 3]")
        self.assertIsNone(summary["session_id"])
        self.assertEqual(summary["final_text"], "")

    def test_falls_back_to_stderr_when_stdout_has_nothing_parseable(self):
        stderr = json.dumps({"status": "ERROR", "message": "boom"})
        summary = hb.summarize_agy("", stderr, exit_code=1)
        self.assertEqual(summary["errors"][0]["status"], "ERROR")

    def test_falls_back_to_stderr_when_stdout_parses_but_is_not_a_dict(self):
        stderr = json.dumps({"status": "ERROR", "message": "boom"})
        summary = hb.summarize_agy("null", stderr, exit_code=1)
        self.assertEqual(len(summary["errors"]), 1)

    def test_prefers_stdout_over_stderr_when_both_are_present(self):
        summary = hb.summarize_agy(json.dumps(self.ENVELOPE), "some unrelated stderr noise", exit_code=0)
        self.assertEqual(summary["final_text"], "PONG\n")


class SummarizeCodexTests(unittest.TestCase):
    def test_agent_message_becomes_final_text_and_is_not_in_quoted_text(self):
        events = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "the answer"}},
            {"type": "turn.completed", "usage": {"tokens": 5}},
        ]
        summary = hb.summarize_codex(events)
        self.assertEqual(summary["final_text"], "the answer")
        self.assertEqual(summary["quoted_text"], "")
        self.assertEqual(summary["session_id"], "t1")

    def test_command_execution_output_is_collected_into_quoted_text(self):
        # Real shape (captured from a real `codex exec --json` run,
        # 2026-09-03): a command_execution item's aggregated_output echoes
        # whatever the command printed -- e.g. a file's contents -- which is
        # not a signal about codex's own run.
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "cat README.md",
                    "aggregated_output": "rate limit and quota, see billing docs",
                    "exit_code": 0,
                },
            },
        ]
        summary = hb.summarize_codex(events)
        self.assertIn("rate limit and quota", summary["quoted_text"])
        self.assertIn("cat README.md", summary["quoted_text"])

    def test_turn_failed_and_error_events_are_captured_as_errors(self):
        events = [{"type": "turn.failed", "error": {"message": "boom"}}]
        summary = hb.summarize_codex(events)
        self.assertEqual(summary["errors"], events)


class SummarizeClaudeTests(unittest.TestCase):
    def test_result_event_becomes_final_text(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "result", "result": "the answer", "usage": {"tokens": 5}, "total_cost_usd": 0.01},
        ]
        summary = hb.summarize_claude(events)
        self.assertEqual(summary["final_text"], "the answer")
        self.assertEqual(summary["session_id"], "s1")
        self.assertEqual(summary["quoted_text"], "")

    def test_tool_result_content_is_collected_into_quoted_text(self):
        # Real shape (captured from a real `claude --verbose` run,
        # 2026-09-03): a Read tool's result echoes the file's contents back
        # as a "user" event, both in message.content and the duplicate
        # tool_use_result.file.content -- neither is a signal about
        # claude's own run.
        events = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "t1",
                            "type": "tool_result",
                            "content": "rate limit and quota, see billing docs",
                        }
                    ],
                },
                "tool_use_result": {
                    "type": "text",
                    "file": {"content": "rate limit and quota, see billing docs"},
                },
            },
        ]
        summary = hb.summarize_claude(events)
        self.assertIn("rate limit and quota", summary["quoted_text"])

    def test_tool_use_input_is_collected_into_quoted_text(self):
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Write", "input": {"content": "quota exceeded example"}}
                    ]
                },
            },
        ]
        summary = hb.summarize_claude(events)
        self.assertIn("quota exceeded example", summary["quoted_text"])

    def test_is_error_result_is_captured_as_error(self):
        events = [{"type": "result", "is_error": True, "result": "oops"}]
        summary = hb.summarize_claude(events)
        self.assertEqual(len(summary["errors"]), 1)


class AgyIntegrationTests(unittest.TestCase):
    """Real subprocess, fake `agy` binary -- same pattern as
    RunProviderTimeoutIntegrationTests/AutoFallbackPromptPropagationTests
    above, extended to the third provider."""

    def setUp(self):
        if os.name != "posix" or not hb.shutil.which("sh"):
            self.skipTest("POSIX shell not available for fake provider scripts")

    def test_successful_agy_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            fake_agy = fake_bin / "agy"
            fake_agy.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                'echo \'{"conversation_id": "c-123", "status": "SUCCESS", "response": "fake agy reply", '
                '"usage": {"input_tokens": 7, "output_tokens": 3}}\'\n',
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)

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
                    "agy",
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
            self.assertEqual(record["provider"], "agy")
            self.assertEqual(record["final_text"], "fake agy reply")
            self.assertFalse(record["handoff_needed"])
            # The real conversation id, captured by summarize_agy(), is saved
            # so the *next* agy call in this workspace resumes it.
            self.assertEqual(state["sessions"]["agy"], "c-123")

    def _run_agy(self, fake_script_body: str, workspace: Path):
        fake_bin = workspace / "fake-bin"
        fake_bin.mkdir()
        fake_agy = fake_bin / "agy"
        fake_agy.write_text(fake_script_body, encoding="utf-8")
        fake_agy.chmod(0o755)
        prompt_path = workspace / "prompt.txt"
        prompt_path.write_text("hello", encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
        return subprocess.run(
            [sys.executable, str(bridge_script), "--workspace", str(workspace), "run", "agy", "--execute",
             "--prompt-file", str(prompt_path)],
            text=True, capture_output=True, env=env, check=False, timeout=30,
        )

    def test_agy_denied_write_with_a_proposed_diff_still_hands_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run_agy(
                "#!/bin/sh\ncat >/dev/null\n"
                "echo '{\"conversation_id\": \"c-5\", \"status\": \"SUCCESS\", "
                "\"response\": \"I could not write it; here is the diff.\", "
                "\"denied_actions\": [{\"action\": \"write_file\"}]}'\n",
                workspace,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            record = state["history"][0]
            self.assertTrue(record["handoff_needed"])
            self.assertTrue(record["reason"].startswith("tool_failure:"), msg=record["reason"])
            self.assertTrue(record["final_text"].startswith("I could not write it; here is the diff."))
            self.assertIn("were not applied", record["final_text"])
            self.assertEqual(state["status"], "handoff_needed")

    def test_unauthenticated_agy_run_end_to_end(self):
        # A headless run with no cached login exits non-zero with
        # "authentication required" on stderr and nothing on stdout.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run_agy(
                "#!/bin/sh\ncat >/dev/null\necho 'Error: authentication required' >&2\nexit 1\n", workspace
            )
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            record = state["history"][0]
            self.assertTrue(record["handoff_needed"])
            self.assertTrue(record["reason"].startswith("auth:"), msg=record["reason"])
            self.assertIsNone(state["sessions"].get("agy"))

    def test_agy_run_that_was_denied_every_tool_is_recorded_as_a_tool_failure(self):
        # Exit 0 + status SUCCESS + empty response + denied_actions: what
        # agy 1.4.8 really prints when print mode refuses a file write.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run_agy(
                "#!/bin/sh\ncat >/dev/null\n"
                "echo '{\"conversation_id\": \"c-9\", \"status\": \"SUCCESS\", \"response\": \"\", "
                "\"denied_actions\": [{\"action\": \"write_file\"}]}'\n",
                workspace,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            record = state["history"][0]
            self.assertTrue(record["handoff_needed"])
            self.assertTrue(record["reason"].startswith("tool_failure:"), msg=record["reason"])
            self.assertIn("permissions.allow", record["final_text"])
            self.assertEqual(state["status"], "handoff_needed")

    def test_auto_fallback_skips_an_uninstalled_middle_provider_to_reach_agy(self):
        # The exact scenario a review flagged as reachable only once
        # PROVIDERS grew past two entries: codex fails, claude isn't
        # installed at all, agy is -- the single-hop auto-fallback
        # must still land on agy, not silently stop after failing to
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

            fake_agy = fake_bin / "agy"
            fake_agy.write_text(
                "#!/bin/sh\ncat >/dev/null\n"
                "echo '{\"conversation_id\": \"c-7\", \"status\": \"SUCCESS\", \"response\": \"agy picked up the handoff\"}'\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)

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
            self.assertEqual(state["history"][1]["provider"], "agy")
            self.assertEqual(state["history"][1]["final_text"], "agy picked up the handoff")
            self.assertFalse(state["history"][1]["handoff_needed"])

    def _run_codex_with_auto_fallback(self, workspace: Path, fake_bin: Path):
        prompt_path = workspace / "prompt.txt"
        prompt_path.write_text("hello", encoding="utf-8")
        # Minimal system baseline + fake_bin, not the inherited PATH, so a
        # real claude/agy CLI on this machine can't answer for a
        # provider this test needs to be absent (see the skip-uninstalled
        # test above for why a fully-empty PATH doesn't work).
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin"
        bridge_script = Path(__file__).resolve().parent.parent / "handoff_bridge.py"
        return subprocess.run(
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

    def test_auto_fallback_does_not_rerun_the_same_provider_when_it_is_the_only_one_installed(self):
        # next_available_provider() returns `current` itself when no other
        # CLI is installed; run_provider() used to recurse into it anyway,
        # re-running the same provider and appending a duplicate failure
        # record (found in review of the API-key-mode removal, where a
        # user with no CLIs at all hit this on every send).
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

            result = self._run_codex_with_auto_fallback(workspace, fake_bin)
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            self.assertIn("not retrying codex", result.stdout)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["history"]), 1)
            self.assertEqual(state["history"][0]["provider"], "codex")

    def test_auto_fallback_with_no_cli_installed_records_exactly_one_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()  # deliberately empty: no codex/claude/agy

            result = self._run_codex_with_auto_fallback(workspace, fake_bin)
            self.assertEqual(result.returncode, 127, msg=result.stderr)
            state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["history"]), 1)
            self.assertEqual(state["history"][0]["exit_code"], 127)


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
