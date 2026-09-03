# DeepSeek V4 Tokenizer Context Budget

## Goal

Replace the current compact-JSON character estimate with model-matched token
counting for `deepseek-v4-flash`. Context decisions, terminal output, and
events must use tokens while preserving the existing low-loss-to-lossy
compaction order and Session safety invariants.

## Confirmed Scope

- Target API model: `deepseek-v4-flash`.
- Target interface: the existing OpenAI-compatible Chat Completions path.
- Count the encoded request before every Agent and finalization model call.
- Count summary requests with the same tokenizer and bound their input by a
  token budget.
- Include the actual per-run tool schemas in the count.
- Report provider-returned usage after a successful call when available.
- Rename the public context-budget unit from characters to tokens.

## Requirements

### R1. Model-matched counting

- Use the official DeepSeek V4 tokenizer and official DeepSeek V4 message
  encoder, including its handling of system/user/assistant/tool messages and
  tool schemas.
- Load the tokenizer once per CLI/Agent run and reuse the same counter.
- Pin the official tokenizer/encoder revision in source and documentation so a
  future upstream change cannot silently alter counts.
- Allow an explicit local `tokenizer.json` path for offline or reproducible
  use. Otherwise, cache the pinned official tokenizer from the official
  `deepseek-ai/DeepSeek-V4-Flash` repository.
- Do not execute tokenizer repository code and do not use
  `trust_remote_code=True`.
- Fail before the first model request with an actionable error if the
  tokenizer cannot be loaded or the configured model is unsupported. There is
  no silent fallback to character estimation or an OpenAI tokenizer.

### R2. Request coverage

- Count the same `messages` and enabled tool schemas that are passed to the
  provider.
- Finalization requests must be counted without tools, matching their actual
  request.
- Summary requests must be token-bounded, including their system prompt and
  message framing; character-only summary truncation is not sufficient.
- The API's returned `usage.prompt_tokens` is the source of truth after a
  request. Local preflight counting must be described as model-matched local
  counting, not guaranteed billing identity.

### R3. Context pipeline

- Preserve this order for normal Agent requests:

  ```text
  tool_result_budget
  -> snip_compact
  -> micro_compact (when over token budget)
  -> fit_tool_results (when still over token budget)
  -> compact_history (last resort)
  -> fail closed if still over token budget
  ```

- Micro/fit stopping decisions and final over-budget checks must use the same
  injected token counter.
- Keep artifact-size and preview safeguards character-based where they bound
  local text or disk output rather than model context.
- Preserve Tool Call/Tool Result pairing, transcript writes, snapshots,
  reactive retry count, active compact behavior, and Permission ordering.

### R4. Public unit migration

- Replace `--max-context-chars` with `--max-context-tokens`.
- Use `DEFAULT_MAX_CONTEXT_TOKENS = 100_000`; this intentionally changes the
  default from 100,000 characters to 100,000 model tokens and must be called
  out in release documentation.
- Rename context event fields to `used_tokens`, `max_tokens`,
  `before_tokens`, and `after_tokens`.
- Terminal context and compaction messages must display `tokens`, not
  characters.
- Do not reinterpret the removed character flag as tokens. Documentation must
  give a direct migration example.

### R5. Provider usage observability

- After each successful model response, emit a bounded `provider_usage` event
  when usage fields exist.
- The payload may contain numeric prompt/completion/total token counts and the
  local preflight prompt count; it must not contain messages, tool arguments,
  response content, headers, credentials, or raw provider objects.
- Missing or partial usage data must not break Agent execution.

## Non-goals

- Supporting OpenAI, Anthropic, DeepSeek V3, legacy `deepseek-chat`, or
  arbitrary gateway model aliases in this task.
- Claiming that a local preflight count is always identical to provider
  billing; provider-side wrappers and model revisions can differ.
- Changing Session schema or persisting tokenizer/cache state in Session JSON.
- Converting file previews, command output limits, persisted Tool Result
  thresholds, or Session byte limits from characters/bytes to tokens.
- Adding a generic tokenizer plugin framework, automatic model detection, or
  runtime execution of remote Python code.

## Acceptance Criteria

- [x] Known Chinese, English, JSON, and special-token fixtures produce stable
      DeepSeek V4 token counts that differ appropriately from character count.
- [x] Tool schemas and Tool Results increase the count through the official V4
      message encoding path.
- [x] Every normal and finalization request gates on token count and preserves
      the existing compaction order.
- [x] Summary requests are token-bounded and retain both the beginning and end
      truncation strategy.
- [x] Missing/unloadable tokenizer and unsupported model fail before a model
      call; no character fallback occurs.
- [x] `--max-context-tokens` validates positive integers and reaches
      `run_agent`; `--max-context-chars` is no longer accepted.
- [x] Context events and terminal rendering use token-named fields and units.
- [x] Provider usage is emitted when present and safely ignored when absent.
- [x] Thinking-mode `reasoning_content` survives tool turns and Session
      round-trips so subsequent tool-bearing requests remain protocol-valid.
- [x] Session save/resume, Permission, Hooks, Tool Call pairing, reactive
      compaction, and step-finalization regression tests still pass.
- [x] README, HANDOFF, Trellis backend spec, dependency metadata, and migration
      guidance match the implemented behavior.
- [x] Focused tests and the full offline suite pass without network access.

## Risks Requiring Review

- The pinned tokenizer can drift from a future provider deployment behind the
  stable `deepseek-v4-flash` API name. Provider usage is therefore exposed for
  comparison and the pinned revision is documented for deliberate upgrades.
- First use without `--tokenizer-file` requires network access to populate the
  tokenizer cache. Failure is explicit and occurs before any model call.
- The same numeric default (`100_000`) permits more content than the previous
  character budget. It remains far below the current DeepSeek V4 context limit,
  but it is a real behavior change rather than a unit-only rename.
