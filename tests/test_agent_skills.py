#!/usr/bin/env python3
"""Unit tests for this repo's own portable Agent Skill(s)
(.agents/skills/, the agentskills.io open standard Codex CLI, Antigravity
CLI and OpenCode read; Claude Code reads .claude/skills/ instead, so each
skill also gets a symlink there -- see docs/architecture.md "Portable Agent
Skill"). Covers the actual SKILL.md frontmatter contract (required
fields, naming/length constraints) and this project's own three-manifest
consistency convention (INSTALL_FILES/REQUIRED_FILES/COMMON_FILES must
all agree, per the 2026-08-20 structure audit).

Run with: python3 -m unittest tests.test_agent_skills -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import handoff_bridge as hb  # noqa: E402
import package_platforms as pp  # noqa: E402
import validate_handoff as vh  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = ROOT / ".agents" / "skills" / "handoff-status" / "SKILL.md"
SKILL_REL_PATH = ".agents/skills/handoff-status/SKILL.md"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Deliberately not a full YAML parser -- this project avoids third-party
# runtime dependencies (docs/production-audit-2026-09-02.md's own
# verification notes this explicitly), and SKILL.md frontmatter here is
# just flat "key: value" pairs, no nesting.
_FIELD_RE = re.compile(r"^(name|description):\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> dict:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    return dict(_FIELD_RE.findall(match.group(1)))


class HandoffStatusSkillFrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL_PATH.read_text(encoding="utf-8")
        self.frontmatter = _parse_frontmatter(self.text)

    def test_file_exists_and_is_not_empty(self):
        self.assertTrue(SKILL_PATH.exists())
        self.assertGreater(SKILL_PATH.stat().st_size, 0)

    def test_has_required_frontmatter_fields(self):
        self.assertIn("name", self.frontmatter)
        self.assertIn("description", self.frontmatter)

    def test_name_matches_the_agent_skills_naming_rules(self):
        # Per platform.claude.com/docs/en/agents-and-tools/agent-skills/
        # overview (same spec Codex CLI/Gemini CLI now share): max 64
        # chars, lowercase letters/digits/hyphens only, no "anthropic"/
        # "claude" reserved words (this skill deliberately avoids those so
        # it stays portable/vendor-neutral across all three CLIs).
        name = self.frontmatter["name"]
        self.assertLessEqual(len(name), 64)
        self.assertRegex(name, r"^[a-z0-9-]+$")
        self.assertNotIn("anthropic", name)
        self.assertNotIn("claude", name)

    def test_description_is_present_and_within_length_limit(self):
        description = self.frontmatter["description"]
        self.assertTrue(description.strip())
        self.assertLessEqual(len(description), 1024)

    def test_description_says_both_what_and_when(self):
        # The spec requires this explicitly: a description that only says
        # what the skill does (not when to use it) makes discovery
        # unreliable -- the agent has nothing to match a request against.
        description = self.frontmatter["description"].lower()
        self.assertIn("use when", description)

    def test_body_references_the_shared_agent_contract(self):
        # This skill's whole point is teaching any CLI this project's own
        # handoff convention -- it should point at the same contract doc
        # AGENTS.md/CLAUDE.md/handoff_bridge.py already reference.
        self.assertIn("docs/shared-agent-contract.md", self.text)


class HandoffStatusSkillManifestConsistencyTests(unittest.TestCase):
    """The 2026-08-20 structure audit found new files repeatedly landing
    in some, but not all, of these three manifests. Same check, applied
    to this skill file."""

    def test_registered_in_install_files(self):
        self.assertIn(
            (SKILL_REL_PATH, SKILL_REL_PATH),
            hb.INSTALL_FILES,
        )

    def test_registered_in_validate_handoff_required_files(self):
        self.assertIn(SKILL_REL_PATH, vh.REQUIRED_FILES)

    def test_registered_in_package_platforms_common_files(self):
        self.assertIn(ROOT / SKILL_REL_PATH, pp.package_files())



def _symlinks_work() -> bool:
    """Whether this machine/user may create a directory symlink (Windows
    needs developer mode or elevation)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.mkdir(Path(tmp) / "target")
            os.symlink("target", Path(tmp) / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINKS_WORK = _symlinks_work()
needs_symlinks = unittest.skipUnless(SYMLINKS_WORK, "this environment cannot create symlinks")


class ClaudeSkillsLinkTests(unittest.TestCase):
    """Claude Code does not read .agents/skills/ (verified against a real
    run, 2026-10-28), so every skill the bridge installs is also linked under
    .claude/skills/."""

    def setUp(self):
        import os
        import tempfile

        self._orig = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig)
        self.link = Path(".claude/skills/handoff-status")

    def _install(self, force=False):
        return dict(hb.install_standard_files(force=force))

    def test_this_repo_links_its_own_skill_for_claude_code(self):
        link = ROOT / ".claude" / "skills" / "handoff-status"
        if not (ROOT / ".git").exists():
            # The packaged source zips carry no .claude/ link: it only exists
            # for developing this repo (install creates it in user workspaces).
            self.skipTest("not a git checkout of this repo (e.g. an extracted release zip)")
        if not SYMLINKS_WORK:
            self.skipTest("this environment cannot create symlinks")
        self.assertTrue(link.is_symlink(), "run `handoff_bridge.py install` to relink .claude/skills/handoff-status")
        self.assertEqual(link.resolve(), (ROOT / ".agents" / "skills" / "handoff-status").resolve())
        self.assertTrue((link / "SKILL.md").is_file())

    def test_bridge_skill_names_come_from_the_install_list(self):
        self.assertEqual(hb.BRIDGE_SKILL_NAMES, ("handoff-status",))

    @needs_symlinks
    def test_install_creates_a_relative_symlink_to_the_single_original(self):
        results = self._install()
        self.assertEqual(results[".claude/skills/handoff-status"], "linked")
        self.assertTrue(self.link.is_symlink())
        self.assertFalse(Path(__import__("os").readlink(self.link)).is_absolute())
        self.assertEqual(self.link.resolve(), Path(".agents/skills/handoff-status").resolve())
        self.assertTrue((self.link / "SKILL.md").is_file())

    @needs_symlinks
    def test_install_is_idempotent(self):
        self._install()
        self.assertEqual(self._install()[".claude/skills/handoff-status"], "ok")
        self.assertEqual(self._install(force=True)[".claude/skills/handoff-status"], "ok")

    def test_a_real_directory_the_user_owns_is_never_replaced(self):
        self.link.mkdir(parents=True)
        (self.link / "SKILL.md").write_text("---\nname: handoff-status\ndescription: mine\n---\n", encoding="utf-8")
        for force in (False, True):
            self.assertEqual(self._install(force=force)[".claude/skills/handoff-status"], "skipped")
            self.assertFalse(self.link.is_symlink())
            self.assertIn("description: mine", (self.link / "SKILL.md").read_text(encoding="utf-8"))

    @needs_symlinks
    def test_a_symlink_to_somewhere_else_is_kept_unless_forced(self):
        Path("elsewhere/handoff-status").mkdir(parents=True)
        Path("elsewhere/handoff-status/SKILL.md").write_text("x", encoding="utf-8")
        self.link.parent.mkdir(parents=True)
        import os
        os.symlink("../../elsewhere/handoff-status", self.link, target_is_directory=True)
        self.assertEqual(self._install()[".claude/skills/handoff-status"], "skipped")
        self.assertIn("elsewhere", os.readlink(self.link))
        self.assertEqual(self._install(force=True)[".claude/skills/handoff-status"], "linked")
        self.assertEqual(self.link.resolve(), Path(".agents/skills/handoff-status").resolve())

    @needs_symlinks
    def test_a_broken_symlink_is_repaired_with_force_and_otherwise_left_alone(self):
        import os
        self.link.parent.mkdir(parents=True)
        os.symlink("../../nowhere", self.link, target_is_directory=True)
        self.assertEqual(self._install()[".claude/skills/handoff-status"], "skipped")
        self.assertEqual(self._install(force=True)[".claude/skills/handoff-status"], "linked")

    def _install_without_symlinks(self, force=False):
        from unittest import mock

        with mock.patch.object(hb.os, "symlink", side_effect=OSError("symlinks not permitted")):
            return self._install(force=force)

    def test_falls_back_to_a_managed_copy_where_symlinks_are_unavailable(self):
        results = self._install_without_symlinks()
        self.assertEqual(results[".claude/skills/handoff-status"], "copied")
        self.assertFalse(self.link.is_symlink())
        self.assertTrue((self.link / "SKILL.md").is_file())
        self.assertTrue((self.link / hb.MANAGED_COPY_MARKER).is_file())

    def test_a_managed_copy_is_left_alone_until_forced_and_then_refreshed(self):
        self._install_without_symlinks()
        self.assertEqual(self._install_without_symlinks()[".claude/skills/handoff-status"], "ok")
        original = Path(".agents/skills/handoff-status/SKILL.md")
        original.write_text(original.read_text(encoding="utf-8") + "\nNEWER-LINE\n", encoding="utf-8")
        self._install_without_symlinks()
        self.assertNotIn("NEWER-LINE", (self.link / "SKILL.md").read_text(encoding="utf-8"))
        # --force reinstalls the original too, so the refreshed copy must match
        # whatever is installed under .agents/skills/ afterwards.
        self.assertEqual(self._install_without_symlinks(force=True)[".claude/skills/handoff-status"], "copied")
        self.assertEqual(
            (self.link / "SKILL.md").read_text(encoding="utf-8"),
            Path(".agents/skills/handoff-status/SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertTrue((self.link / hb.MANAGED_COPY_MARKER).is_file())

    def test_a_directory_without_the_marker_is_never_refreshed_even_when_forced(self):
        self.link.mkdir(parents=True)
        (self.link / "SKILL.md").write_text("mine", encoding="utf-8")
        self.assertEqual(self._install_without_symlinks(force=True)[".claude/skills/handoff-status"], "skipped")
        self.assertEqual((self.link / "SKILL.md").read_text(encoding="utf-8"), "mine")

    def test_a_git_symlink_placeholder_file_is_repaired_by_a_plain_install(self):
        # git without symlink support checks a link out as a file whose whole
        # content is the link target.
        self.link.parent.mkdir(parents=True)
        self.link.write_text("../../.agents/skills/handoff-status\n", encoding="utf-8")
        results = self._install_without_symlinks()
        self.assertEqual(results[".claude/skills/handoff-status"], "copied")
        self.assertTrue((self.link / "SKILL.md").is_file())

    @needs_symlinks
    def test_a_git_symlink_placeholder_file_becomes_a_real_link_when_possible(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("../../.agents/skills/handoff-status", encoding="utf-8")
        self.assertEqual(self._install()[".claude/skills/handoff-status"], "linked")
        self.assertTrue(self.link.is_symlink())

    def test_some_other_plain_file_at_that_path_is_not_touched(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("something the user wrote", encoding="utf-8")
        for force in (False, True):
            self.assertEqual(self._install_without_symlinks(force=force)[".claude/skills/handoff-status"], "skipped")
        self.assertEqual(self.link.read_text(encoding="utf-8"), "something the user wrote")

    def test_only_the_bridges_own_skills_are_linked(self):
        other = Path(".agents/skills/my-own-skill")
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: x\n---\n", encoding="utf-8")
        self._install()
        self.assertFalse(Path(".claude/skills/my-own-skill").exists())


if __name__ == "__main__":
    unittest.main()
