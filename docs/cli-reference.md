# CLI Reference

All commands are safe preview commands unless `--execute` or
`--allow-execute` is present.

## `handoff_bridge.py`

### Diagnose

```bash
python3 handoff_bridge.py diagnose
```

Checks local CLI paths and auth status for Codex and Claude Code.

### Install Into A Workspace

```bash
python3 handoff_bridge.py --workspace /path/to/project install
```

Installs shared handoff files into the selected project. Existing files are
preserved unless `--force` is provided.

```bash
python3 handoff_bridge.py --workspace /path/to/project install --force
```

### Initialize A Task

```bash
python3 handoff_bridge.py --workspace /path/to/project init \
  "Implement the requested feature" \
  --primary codex \
  --target-model "app-selected default"
```

Creates `.handoff/current.md` and `.handoff/state.json` for a new task.

### Preview A Run

```bash
python3 handoff_bridge.py --workspace /path/to/project run auto \
  --instruction-type continue \
  "Continue the task"
```

Builds `.handoff/next-prompt.md` without calling a model provider.

### Execute A Run

```bash
python3 handoff_bridge.py --workspace /path/to/project run auto \
  --execute \
  --auto-fallback \
  --instruction-type continue \
  "Continue the task"
```

Calls the selected provider and records the result. Use deliberately because it
may spend provider tokens.

### Model Labels And Overrides

```bash
python3 handoff_bridge.py run codex --model "app-selected default" "Preview"
```

Records the model label only.

```bash
python3 handoff_bridge.py run codex --model "exact-model-id" "Preview"
```

Records and passes the exact model ID as a provider override.

### Check

```bash
python3 handoff_bridge.py check
```

Runs no-token consistency checks for files, JSON, and Python syntax.

## `handoff_control.py`

Open the guided menu:

```bash
python3 handoff_control.py
```

Run a one-shot preview setup:

```bash
python3 handoff_control.py --workspace /path/to/project \
  --provider auto \
  --primary codex \
  --model "app-selected default" \
  "Implement the requested feature"
```

Execute through the controller:

```bash
python3 handoff_control.py --workspace /path/to/project \
  --execute \
  "Implement the requested feature"
```

The controller asks for confirmation before spending tokens unless `--yes` is
also supplied.

## Optional HTTP Remote

Start a preview-only local server:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765
```

Submit a preview task:

```bash
python3 remote_handoff_submit.py \
  --url http://127.0.0.1:8765 \
  --workspace /path/to/project \
  --wait \
  "Inspect the handoff setup"
```

Allow remote requests to call providers:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765 --allow-execute
```

Do this only for trusted automation.
