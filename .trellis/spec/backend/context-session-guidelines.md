# Context and Session Contracts

> Rules for bounded model requests, durable checkpoints, and safe recovery.

## Context Budgeting

`context.py::estimate_request_chars` compact-serializes both `messages` and the
actual per-run tool schemas. It is a conservative character estimate, not a
tokenizer. Every normal model request uses this pipeline in order:

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

Do not persist credentials, OpenAI client objects, approval callbacks, or the
per-run `allow_run_command` switch.

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
