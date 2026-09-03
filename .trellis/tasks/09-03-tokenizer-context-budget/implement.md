# DeepSeek V4 Tokenizer Context Implementation Plan

## 1. Official Tokenizer Adapter

- [x] Add the `tokenizers` runtime dependency with a bounded compatible range.
- [x] Add the official DeepSeek V4 encoder snapshot and third-party notice.
- [x] Implement the immutable `DeepSeekV4TokenCounter` adapter and pinned Hub
      loader.
- [x] Support an explicit local `tokenizer.json` path and reject invalid files
      before any model call.
- [x] Reject unsupported models and remove every silent character fallback.
- [x] Add focused unit tests for model validation, stable counts, tools, Tool
      Results, special tokens, and mutation safety.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_token_counting.py
```

## 2. Context Pipeline Migration

- [x] Inject the request token counter into Agent and context helpers.
- [x] Rename budget constants, parameters, local variables, and failure text
      from characters to tokens.
- [x] Use tokens for micro/fit stopping decisions, normal request gates,
      active compaction, reactive compaction, and finalization.
- [x] Replace the summary character ceiling with token-bounded beginning/end
      fitting.
- [x] Keep disk/output/preview character limits unchanged and document the
      distinction.
- [x] Preserve Tool Call pairing, transcript safety, snapshots, Hooks, and
      Permission ordering.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_context.py tests/test_agent.py
```

## 3. CLI and Observability Migration

- [x] Replace `--max-context-chars` with `--max-context-tokens` and add the
      optional `--tokenizer-file` argument.
- [x] Construct one counter before `run_agent` and pass it through explicitly.
- [x] Rename context event payloads/rendering to token units.
- [x] Emit safe provider usage data when present and tolerate missing/partial
      usage.
- [x] Add CLI and event regression tests, including proof that the old flag is
      not silently reinterpreted.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_cli.py tests/test_events.py
```

## 4. Documentation and Specifications

- [x] Update README examples, dependency/install behavior, first-run cache,
      offline tokenizer path, and CLI migration note.
- [x] Update HANDOFF current state, runtime chain, constants, tests, and known
      accuracy boundary.
- [x] Update the Trellis context/session contract so token budgets and retained
      character-based artifact limits are unambiguous.
- [x] Record official tokenizer/encoder source, pinned revision, and licensing.

## 5. Verification and Standard Git Flow

- [x] Run all focused tests without network access.
- [x] Run the full offline test suite.
- [x] Build and install the package in a fresh temporary environment; verify
      the console script and packaged vendor module.
- [x] Run an opt-in pinned-tokenizer integration probe.
- [x] If credentials are already locally configured, run one isolated read-only
      provider E2E without reading or printing `.env` values.
- [x] Review scoped diff/status and scan staged content for likely secrets
      without reading `.env`.
- [x] Commit with a Conventional Commit, push the feature branch, and open a
      pull request into `main`.

Full offline validation:

```bash
PYTHONPATH=src \
PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/vibe-harness/pycache \
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider
```
