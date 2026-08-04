# Quality Gates

This document is the single list of rules this repository actually enforces,
how each one is enforced, and where the enforcement code lives. It grew out
of a full-repo review that found several rules described in other docs
(`docs/shared-agent-contract.md`, `docs/verification-playbook.md`,
`docs/security-model.md`) were aspirational — written down but not checked by
anything. Every rule below has a script backing it; if a rule has no script,
it does not belong in this document.

## Enforcement Levels

Each rule is enforced at up to three layers:

- **Doc**: written down so a human or agent knows the rule exists.
- **Local hook**: runs automatically on `git commit` / `git push` once
  `scripts/install_git_hooks.sh` has been run. Opt-in per clone.
- **CI**: runs on every push to `main` and every pull request. Cannot be
  skipped by forgetting to install hooks locally.

Run `scripts/install_git_hooks.sh` once per clone to get the local-hook
layer; see [Platform Setup](platform-setup.md).

## Rule: Branch Naming

Branches must match `type/short-kebab-case-description`.

Allowed types: `feature`, `fix`, `docs`, `chore`, `refactor`, `test`,
`release`, `hotfix`.

```text
fix/state-json-race
feature/branch-name-gate
docs/quality-gates
```

`main` and `master` are exempt. Detached HEAD checkouts (e.g. CI checking out
a merge commit) are skipped rather than failed.

- **Doc**: this section; the pattern and type list also live in
  `scripts/check_branch_name.py` (source of truth — keep both in sync).
- **Local hook**: `.githooks/pre-push` checks every branch ref being pushed.
- **CI**: `.github/workflows/ci.yml`'s `branch-name` job checks
  `github.head_ref` on every pull request.
- **Script**: `python3 scripts/check_branch_name.py [branch]`

This convention is specific to contributing to this bridge tool itself. If
you install this repo's files into a downstream project
(`handoff_bridge.py install`), that project's own branch convention is
unaffected — `check_branch_name.py` is not part of `handoff_bridge.py check`
and is not copied by `install`.

## Rule: No Secrets In Tracked Files

No API key, private key, auth token, or credential file may be committed.
This was previously only documented in `docs/security-model.md`'s
"Credential Boundaries" section with nothing to actually catch a violation.

- **Doc**: this section and `docs/security-model.md`.
- **Local hook**: `.githooks/pre-commit` scans staged files.
- **CI / no-token check**: `handoff_bridge.py check` runs a full-tree scan.
- **Script**: `python3 scripts/scan_secrets.py [--staged]`

The scanner is pattern-based (AWS keys, private key blocks, GitHub/Slack/
Anthropic/OpenAI-style tokens, generic `key = "..."` assignments, and banned
filenames like `auth.json`/`.env`). It is a safety net, not a guarantee —
review diffs before pushing regardless.

**Testing this scanner is self-referential**: its own test fixtures need
secret-shaped strings to verify detection, but once `tests/test_scan_secrets.py`
is committed, its literal fixtures would themselves be flagged by the
full-tree scan (this happened once — CI failed on the first push of that
file). `tests/test_scan_secrets.py` assembles every fixture at runtime via a
`_fake()` helper (string concatenation) instead of writing the pattern as a
contiguous literal, so the committed source text never matches the scanner's
own regexes. Keep that pattern for any new scanner test fixture.

## Rule: Failure Classification Stays In Sync

`docs/shared-agent-contract.md` defines the canonical handoff failure labels:
`quota`, `rate_limit`, `auth`, `billing`, `context_limit`, `overloaded`,
`tool_failure`, `unknown`. `handoff_bridge.py`'s `classify_handoff()` must
recognize all of them — before this rule existed, `tool_failure` was
documented but never actually produced by the classifier, so a class of
handoffs was silently mislabeled.

- **Doc**: this section; canonical list also duplicated as
  `HANDOFF_LABELS` in `handoff_bridge.py` and
  `HANDOFF_CLASSIFICATION_LABELS` in `scripts/validate_handoff.py`.
- **CI / no-token check**: `handoff_bridge.py check` fails if the contract
  and the classifier vocabulary drift apart.
- **Script**: `check_failure_classification()` in `scripts/validate_handoff.py`.

`classify_handoff()` always returns a reason string prefixed with one of
these labels (or `none:` when no handoff is needed) — see
`tests/test_handoff_bridge.py::ClassifyHandoffTests` for the coverage this
depends on.

## Rule: Core Logic Has Unit Tests

`handoff_bridge.py`'s provider fallback and classification logic
(`classify_handoff`, `choose_auto_provider`, `model_override_arg`), the
shared-state write path (`atomic_write_text`, `WriteLock`), and the two
quality-gate scripts that themselves gate everything else
(`scan_secrets.py`'s pattern matching, `check_branch_name.py`'s pattern and
subprocess-failure handling) must have test coverage under `tests/`. This is
the highest-risk untested surface in the repo: there is no CI provider
integration test (that would spend real tokens), so these pure-logic paths
are the only regression safety net.

`check_branch_name.py`'s `current_branch()` originally crashed with an
uncaught `FileNotFoundError` when `root` didn't exist or `git` wasn't
installed, instead of returning `None` like the rest of the module expects —
found by writing `tests/test_check_branch_name.py`, not by inspection.
Exactly the failure mode this rule exists to catch.

- **Doc**: this section.
- **CI / no-token check**: `handoff_bridge.py check` runs
  `python3 -m unittest discover -s tests` and fails the whole check if any
  test fails or if `tests/` has no `test_*.py` files.
- **Script**: `check_tests()` in `scripts/validate_handoff.py`; tests live in
  `tests/test_handoff_bridge.py`, `tests/test_scan_secrets.py`, and
  `tests/test_check_branch_name.py`.

Uses the standard library `unittest` rather than `pytest` deliberately — the
repo has no dependency file (`requirements.txt`/`pyproject.toml`) and
`docs/shared-agent-contract.md` favors boring, dependency-free tooling.

## Rule: Shared State Files Are Written Atomically And Under Lock

`.handoff/state.json` and `.handoff/current.md` are read and written by
concurrent processes (the HTTP remote server spawns one `handoff_bridge.py`
subprocess per task via `threading.Thread`, per `remote_handoff_server.py`).
A plain `path.write_text(...)` can leave a torn/partial file if two writers
overlap, and unsynchronized reads-then-writes can silently drop an update.

- **Doc**: this section, `docs/architecture.md`'s "State Boundaries", and
  (for the Web UI's own store) [Web UI Chat Storage](webui-chat-storage.md).
- **Code**: `handoff_bridge.py`'s `WriteLock` (cross-process exclusive-create
  lock at `.handoff/.write.lock`) and `atomic_write_text` (write-to-temp,
  `os.replace`). `write_json` and `append_current` both go through these —
  never call `STATE_FILE.write_text(...)` or `CURRENT_FILE.open("a")`
  directly. `handoff_webui.py` imports the same `WriteLock` (not a
  reimplementation) to guard `.handoff/webui/chat/` — see
  `append_chat_message()` and `archive_old_months()`.
- **Test**: `tests/test_handoff_bridge.py::AtomicWriteTests` and
  `::WriteLockTests`; `tests/test_handoff_webui.py::ChatStorageTests`'s
  multi-month archive coverage and `EnsureChatGitignoreTests` for the
  `handoff_webui.py` side.

Known residual limitation: the lock covers each individual write, not a full
read-modify-write cycle across two separate `handoff_bridge.py` invocations.
Two concurrent `run` calls against the same workspace can still race at the
read step; only the write itself is guaranteed non-corrupting. Document any
change that needs stronger guarantees before relying on this for anything
beyond the current single-operator use case.

## Adding A New Gate

1. Write the check as a script under `scripts/` (or extend
   `scripts/validate_handoff.py` if it belongs in the no-token suite).
2. Add it to `handoff_bridge.py check` only if it should run for every
   consumer of this repo, including projects that installed it — otherwise
   wire it into `.githooks/` and `.github/workflows/ci.yml` directly, as
   done for branch naming.
3. Document it here with the same Doc/Local hook/CI/Script structure.
4. Add the new files to `REQUIRED_FILES` (and `PYTHON_FILES`/`JSON_FILES` if
   applicable) in `scripts/validate_handoff.py` so the suite catches an
   accidental deletion.
