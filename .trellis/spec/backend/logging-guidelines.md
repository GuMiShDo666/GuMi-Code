# Terminal Events and Logging

> The project uses structured callbacks plus concise terminal rendering, not a logging framework.

## Event Boundary

Runtime code emits `(event_type, payload)` through `AgentEventCallback`.
`events.py::print_terminal_event` is the only normal terminal renderer. Keep
orchestration independent from presentation so tests can capture events
without scraping stdout.

Current event families include:

- context usage, summary, and compaction stages;
- model-call boundaries;
- tool calls and tool errors;
- interrupted tool-batch recovery;
- final answers.

Evidence: `src/vibe_harness/events.py` and event assertions in
`tests/test_events.py`.

## What Users Should See

- Context usage and compaction counts/paths, never full history.
- `step` plus `tool_name` for normal tool progress.
- Tool error text, capped by `MAX_PREVIEW`.
- The complete final answer; do not truncate it as an ordinary preview.
- During permission approval only: tool, reason, and a bounded JSON argument
  preview from `permissions/terminal.py`.

Successful Tool Result bodies and ordinary Tool Call arguments stay hidden.
They are already available to the model/session and create noisy or sensitive
terminal output when printed twice.

## Sensitive Data Rules

Never print or persist debug output containing:

- API keys, access tokens, cookies, authorization headers, or `.env` values;
- full conversation history in progress events;
- unbounded file, diff, search, or command output;
- chain-of-thought or hidden model reasoning.

If a new event needs arguments for informed user consent, add it to the
permission UI rather than broadening ordinary logging.

## stdout and stderr

- Progress and final answers use stdout.
- Fatal CLI errors use stderr after stdout is flushed, preserving readable
  ordering in terminals and tests.
- Expected tool failures remain Tool Results and normally do not terminate the
  process.

## Verification

Update `tests/test_events.py` whenever an event payload or display contract
changes. Include assertions that sensitive arguments/results are absent, not
only assertions for desired visible text.
