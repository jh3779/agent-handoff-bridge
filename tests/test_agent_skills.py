#!/usr/bin/env python3
"""Unit tests for this repo's own portable Agent Skill(s)
(.agents/skills/, the agentskills.io open standard Codex CLI/Claude Code/
Gemini CLI all discover -- see docs/research.md's provider-selection
discussion). Covers the actual SKILL.md frontmatter contract (required
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


if __name__ == "__main__":
    unittest.main()
