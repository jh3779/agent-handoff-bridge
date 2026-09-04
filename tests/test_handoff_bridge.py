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
    workspace project context folded into every CLI-mode prompt
    regardless of provider. API-key mode reads the same file directly
    (webui_api_key_mode.py), not through this module."""

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
        prompt = hb.build_prompt("codex", state, "hello")
        self.assertIn("## Agent Targeting Protocol", prompt)
        self.assertIn("## Shared Agent Contract", prompt)
        self.assertIn("## Verification Playbook", prompt)

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
        self.assertIn("## Shared Agent Contract", prompt)
        self.assertIn("## Handoff Reason", prompt)

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
        prompt = hb.build_prompt("codex", {"task": "do the thing", "sessions": {}}, "hello")
        self.assertIn("no need to", prompt)
        self.assertIn("open these files again", prompt)
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


class ProviderCommandCodexTests(unittest.TestCase):
    """Regression coverage for a real bug a user hit in production
    (2026-09-03): codex exec refuses to run at all ("Not inside a
    trusted directory and --skip-git-repo-check was not specified") in
    any workspace that isn't itself a git repo -- which every workspace
    this bridge auto-creates is not, by default. provider_command() had
    no test coverage at all for codex/claude before this (only Gemini,
    see ProviderCommandGeminiTests below) -- exactly the kind of gap
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
