# Context and Session Contracts

> Rules for bounded model requests, durable checkpoints, and safe recovery.

## Context Budgeting

`token_counting.py::DeepSeekV4TokenCounter` renders `messages` and the actual
per-run tool schemas through the pinned official DeepSeek V4 message encoder,
then counts IDs with the matching official tokenizer. The CLI creates one
counter for `deepseek-v4-flash` and reuses it for the whole run. Unsupported
models and tokenizer load failures fail before the first model request; never
add a silent character or foreign-model fallback. Every normal model request
uses this pipeline in order:

```text
tool_result_budget
-> snip_compact
-> micro_compact (only when over budget)
-> fit_tool_results (only when still over budget)
-> compact_history (last resort)
-> fail closed if still over budget
```

Preserve this low-loss-to-lossy ordering. Large results and old transcripts
must be written before their in-context content is replaced.

Normal, summary, reactive-compaction, active-compaction, and finalization
requests must use the same injected counter. Summary-input fitting is governed
by the complete encoded request token count, even though character slicing is
used to retain one quarter of the beginning and three quarters of the end.

The local count is a preflight safety value, not a billing guarantee. Emit only
available numeric provider usage fields after successful responses; gateway
`usage.prompt_tokens` is authoritative after the request. Missing or partial
usage must not fail the Agent. Provider-usage events must never include
messages, tool arguments, response bodies, headers, or credentials.

`DEFAULT_MAX_CONTEXT_TOKENS` and public `*_tokens` names are model-context
limits. Character names remain correct for local artifact and text controls,
including Tool Result persistence thresholds, preview lengths,
`read_tool_output(max_chars)`, Todo content, and command-output limits.

## Persisted Context Artifacts

- Large Tool Results live under `.task_outputs/tool-results/`.
- Full histories live under `.transcripts/` before archiving or summarization.
- Use relative paths in model messages.
- Reject symlinked internal directories and out-of-bound persisted markers.
- Write artifacts atomically with mode `0600`.
- `read_tool_output` is the bounded, paginated route for model access to saved
  Tool Results; it only accepts direct `.txt` children of the expected folder.

These files are plaintext local artifacts, not encrypted secret storage.

## Compaction Safety

- Never split an assistant `tool_calls` message from its ordered Tool Results.
- Summary requests receive no tools.
- Treat source history as untrusted data in the summary system prompt.
- Keep `Current user request`, JSON-encoded historical summary, and transcript
  path visibly separate in compacted messages.
- Preserve the current system instruction and add the compaction constraint;
  history summaries cannot authorize new actions.
- Active `compact` is an Agent control tool, not a registry/permission tool.
  Finish every real Tool Result in a mixed batch before compacting.
- On provider context-length errors, reactive compaction may retry once. Do
  not create unbounded model retry loops.

Evidence: `context.py`, `agent.py::_is_context_length_error`, and
`tests/test_context.py`.

## Session Snapshot Format

`session.py` stores versioned JSON containing only:

- normalized absolute `workspace`;
- `model`;
- OpenAI-compatible `messages`;
- `version`.

Do not persist credentials, OpenAI client objects, approval callbacks, the
per-run `allow_run_command` switch, or the selected Docker sandbox image.

Preserve string `reasoning_content` on assistant messages. DeepSeek thinking
mode requires every prior assistant reasoning field to be passed back when a
subsequent request carries tools. It is part of resumable model-visible state,
must be included in local token counting, and must not be printed in normal
terminal events.

Session saves must:

- reject symlink targets and invalid parent paths;
- enforce the 5 MiB UTF-8 byte limit before writing;
- write a same-directory temporary file with mode `0600`;
- atomically replace the previous checkpoint;
- preserve raw Tool Call argument JSON strings during SDK-message conversion.

## Snapshot Timing

`run_agent()` calls the message snapshot callback after each meaningful state
transition: initial/current user messages, assistant replies, context changes,
real Tool Results, and final answers. Before an enabled tool executes, it also
saves an `_vibe_harness_in_flight` marker for that call.

When a `Stop` Hook requests continuation, append its non-empty string as a new
user message and snapshot it before the next model request. The existing
`max_steps` boundary remains authoritative and finalization still sends no
tools.

Do not remove the pre-execution snapshot. Without it, a crash after a side
effect but before its Tool Result would look like a never-started call.

## Recovery Rules

On resume:

1. Validate UTF-8/JSON/version/message shape and require a system first message.
2. Require stored workspace and model to match the current invocation.
3. Analyze the final Tool Call batch as an ordered result prefix.
4. Preserve completed results exactly.
5. Do not replay pending calls; close them with matching synthetic Tool Results
   that instruct the model to inspect current state and re-plan.
6. A read-only in-flight call may be closed as interrupted and re-planned.
7. Refuse automatic recovery for an in-flight mutating/destructive/execution
   call because its external side effect is unknown.
8. Deep-copy loaded messages before adding the new user request.

References: `session.py::analyze_tool_batch_tail`,
`plan_tool_batch_recovery`, `build_tool_batch_recovery_messages`, and resume
tests in `tests/test_cli.py`/`tests/test_session.py`.

## Distinctions

- Context compaction controls what is sent to the model now.
- Session persistence checkpoints resumable current message state.
- Transcript files preserve exact history removed during compaction.
- None of these is cross-session semantic long-term memory.
