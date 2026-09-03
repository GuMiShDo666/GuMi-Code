# DeepSeek V4 Tokenizer Context Design

## Decision Summary

Add one narrow `DeepSeekV4TokenCounter` that converts the exact Harness message
shape plus enabled tool schemas into the official DeepSeek V4 prompt format,
then counts token IDs with Hugging Face `tokenizers`. Inject this counter into
the existing context pipeline. Keep character limits only for local string and
artifact safeguards.

This does not use `tiktoken`: it is an OpenAI-model tokenizer and cannot
identify DeepSeek V4's vocabulary or DSML chat framing. It also does not rely
on raw compact JSON tokenization because that would omit DeepSeek's message,
tool-schema, Tool Call, and Tool Result framing.

## Components

### `token_counting.py`

Own a small protocol and DeepSeek implementation:

```text
RequestTokenCounter.count_request(messages, tools) -> positive int

DeepSeekV4TokenCounter
  -> copy messages
  -> attach enabled tools to the system message for local encoding only
  -> official encode_messages(..., thinking_mode="thinking")
  -> tokenizer.encode(prompt, add_special_tokens=False)
  -> len(token_ids)
```

The original messages and schemas are never mutated. Every Agent model request
explicitly sends `thinking.type="enabled"`, and the local encoder uses
`thinking_mode="thinking"` to match that request mode.

The loader accepts either:

1. `--tokenizer-file /absolute/path/tokenizer.json`; or
2. the official Hub id `deepseek-ai/DeepSeek-V4-Flash` at a pinned revision.

The reviewed pin is
`60d8d70770c6776ff598c94bb586a859a38244f1` (reviewed 2026-09-03).

Use the lightweight `tokenizers` package, not the full `transformers` stack.
`Tokenizer.from_file` and `Tokenizer.from_pretrained` load data only; remote
Python code is never trusted or executed.

### Official encoder snapshot

Vendor the official self-contained `encoding_dsv4.py` at the pinned
DeepSeek V4 revision under a clearly marked internal vendor module. Preserve
its license notice/source URL and add a third-party notice. Do not edit its
behavior as part of normal Harness logic; the adapter performs only the
messages/tools placement needed for the existing OpenAI-compatible request.

Vendoring the 28 KB encoder is preferred over reimplementing the DSML protocol
because tool schema rendering and special-token ordering are part of token
identity. The 6.4 MB tokenizer vocabulary is not committed; it is loaded from
the explicit local path or the pinned official Hub revision and cached by the
library.

## Runtime Flow

```text
CLI parse
-> load API settings
-> validate model == deepseek-v4-flash
-> load one pinned/local DeepSeekV4TokenCounter
-> run_agent(counter, max_context_tokens)
-> encode/count current messages + actual enabled tool schemas
-> existing low-loss-to-lossy compaction pipeline
-> emit local context_usage
-> model call
-> emit provider_usage if returned
```

The counter is also passed into `apply_micro_compact`,
`apply_fit_tool_results`, `compact_history`, `reactive_compact`, active compact,
and finalization. No module-level mutable tokenizer is introduced.

Because Agent requests carry tools and explicitly enable thinking mode,
assistant `reasoning_content` is preserved in the plain message dict and in
Session snapshots. This both satisfies DeepSeek's multi-step tool-calling
protocol and lets the local encoder count the same reasoning content sent in
the next request. The field is never rendered in normal terminal events.

## Summary Request Budget

Replace the fixed 80,000-character summary input limit with a token limit. The
summary request budget is the smaller of its configured ceiling and a safe
fraction of the run's context budget, leaving room for the summary system
message, framing, and `SUMMARY_MAX_TOKENS` output.

To preserve the existing beginning/end evidence policy:

1. serialize history as JSON;
2. count the complete summary request;
3. if over budget, binary-search the number of retained characters while
   keeping one quarter from the beginning and three quarters from the end;
4. count the final request again and fail closed if it cannot fit.

Character slicing remains only an implementation mechanism for selecting text;
the stopping condition is tokens.

## API and Naming Migration

- `DEFAULT_MAX_CONTEXT_CHARS` -> `DEFAULT_MAX_CONTEXT_TOKENS`
- `max_context_chars` -> `max_context_tokens`
- `estimate_request_chars` -> injected `count_request_tokens`
- `target_chars` -> `target_tokens`
- context event fields `*_chars` -> `*_tokens`
- CLI `--max-context-chars` -> `--max-context-tokens`

Local artifact controls such as `LARGE_RESULT_CHAR_LIMIT`, preview lengths,
`max_chars` for reading tool output, and Session byte limits keep their names
and behavior.

## Provider Usage

Read usage defensively from successful SDK responses. Emit only available
integer fields:

```json
{
  "step": 1,
  "local_prompt_tokens": 1234,
  "prompt_tokens": 1241,
  "completion_tokens": 98,
  "total_tokens": 1339
}
```

The terminal may show a compact comparison, but absence of usage is normal for
some OpenAI-compatible gateways and does not cause failure. The Harness never
uses post-response usage to retroactively authorize an oversized request.

## Compatibility and Failure Behavior

- Session version remains 1 because persisted data contains neither the budget
  unit nor the tokenizer.
- Programmatic `run_agent` accepts an injected counter for deterministic tests;
  production CLI always supplies the DeepSeek counter.
- Unsupported model names fail explicitly. Generic multi-provider dispatch is
  deferred to a separate task.
- Tokenizer load/encode errors are sanitized and wrapped in actionable Harness
  errors without printing paths outside the explicitly supplied local path,
  environment values, request content, or credentials.
- Provider context-length errors still trigger at most one reactive compaction,
  protecting against server-side wrapper/revision differences.

## Test Strategy

- Unit-test the adapter with a tiny deterministic fake tokenizer plus pinned
  golden token-count fixtures generated from the official V4 tokenizer.
- Test local-file and Hub loader selection without real network calls.
- Inject deterministic counters into context/Agent tests so the offline suite
  never downloads tokenizer assets.
- Add regressions for tool schemas, Tool Results, mutation safety, summary
  fitting, CLI migration, event field migration, missing usage, and partial
  usage.
- Run a separate opt-in integration probe against the pinned official
  tokenizer, then a real provider E2E only in an isolated workspace and without
  exposing configuration values.

## Rollback

Rollback removes the counter module/vendor snapshot/dependency and restores
the character API. Session files and persisted transcripts remain readable.
Because event and CLI names change, rollback documentation must state which
unit each release expects; values must never be silently reinterpreted.
