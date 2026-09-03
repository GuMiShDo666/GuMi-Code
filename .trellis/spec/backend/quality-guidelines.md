# Quality and Testing

> Verification rules for a security-sensitive local Agent Harness.

## Change Discipline

- Make the smallest change that satisfies the current requirement.
- Preserve existing module ownership and public behavior.
- Do not add dependencies or abstractions for hypothetical future use.
- Treat tool schemas, runtime validation, permission policy, events, and tests
  as one cross-layer contract.
- Do not read `.env` or place real credentials in source, tests, fixtures,
  output, documentation, or Session examples.

## Python Style in This Repository

- Target Python 3.11 or newer.
- Use type annotations on public and boundary functions.
- Use frozen dataclasses for immutable transport/config objects such as
  `Settings`, `SessionData`, and recovery plans.
- Keep limits as named module constants; share one constant between schema and
  runtime validation when both enforce the same contract.
- Prefer short module-level helpers over broad service classes.
- Use `Path` and explicit UTF-8 for local text files.
- Do not introduce a linter/formatter claim: none is configured in
  `pyproject.toml` today. Match the existing formatting manually.

## Test Structure

- Use pytest and mirror source capabilities with focused files such as
  `test_context.py`, `test_session.py`, and `test_tool_registry.py`.
- Use `tmp_path`/the `workspace` fixture for filesystem effects. Never run
  mutating Agent tests against the repository workspace.
- Use fake OpenAI-compatible clients (`SimpleNamespace`, scripted responses)
  for Agent and compaction tests. Unit tests must not require network access or
  real credentials.
- Use `monkeypatch` for environment variables, subprocess behavior, callbacks,
  and module boundaries.
- Assert both the returned value and the absence/presence of side effects.
- Test successful, denied, invalid, boundary, and adversarial paths.
- For protocol behavior, assert exact message order, role, raw tool arguments,
  and matching `tool_call_id` values.
- For security filters, prove that safe near-neighbors still work so a deny
  rule does not become an unusable blanket blacklist.

Representative tests:

- `tests/test_agent.py` for Fake Client requests and message protocol.
- `tests/test_tool_registry.py` for schema/risk/handler consistency.
- `tests/test_filesystem_tools.py` and `tests/test_search_tools.py` for path,
  symlink, sensitive-name, and output boundaries.
- `tests/test_command_tool.py` for Docker argv, sandbox preflight, sensitive
  masks, bounded output, and exact-name cleanup.
- `tests/test_docker_sandbox_integration.py` for opt-in live containment using
  only a caller-supplied preloaded image; the offline suite must never pull.
- `tests/test_session.py` and `tests/test_context.py` for persistence and
  recovery invariants.

## Required Verification

Run focused tests for the changed module first, then the full suite:

```bash
PYTHONPATH=src \
PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/vibe-harness/pycache \
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider
```

Historical test counts are evidence for an old checkpoint, not proof of the
current tree. Report the command and result from the current run.

Live Docker verification is opt-in:

```bash
VIBE_HARNESS_DOCKER_E2E_IMAGE=<preloaded-image-with-python3> \
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_docker_sandbox_integration.py
```

Do not place registry credentials in that variable or let the test pull/build
an image. A skipped live test is not proof that the Docker boundary works.

## Git Workflow

- Initial repository publication establishes `main`; subsequent work uses a
  scoped `feat/*`, `fix/*`, `docs/*`, `test/*`, or `chore/*` branch.
- Pull `main` with `--ff-only` before branching; do not hide divergence with a
  merge or history rewrite.
- Review working-tree and staged diffs, stage explicit paths, and run a
  filename-only secret scan before every commit.
- Use Conventional Commit messages that describe one coherent change.
- Push the task branch and open a pull request before merging to `main`.
- Do not force-push or commit `.env`, Session snapshots, transcripts, persisted
  Tool Results, credentials, or tokens.

## Review Checklist

- Does every changed line serve the requested behavior?
- Is the runtime path connected, rather than only defining an unused helper?
- Are schema and runtime limits consistent?
- Does every Tool Call receive one matching Tool Result?
- Can paths, glob patterns, symlinks, environment variables, or persisted
  markers cross their intended boundary?
- Are writes atomic where interruption could corrupt durable state?
- Are errors bounded and free of secrets?
- Are stateful and mutating tests isolated?
- Is the documented safety boundary honest, especially for `run_command`?
