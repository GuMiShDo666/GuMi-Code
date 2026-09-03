# Cross-Layer Thinking Guide

> Trace a change through the complete Harness runtime before editing.

## Runtime Map

```text
CLI input/config
-> Agent request messages + dynamic tool schemas
-> model Tool Calls
-> allow-set + registry + permission/preflight
-> tool handler side effect/result
-> matching Tool Results
-> context budgeting/compaction
-> Session snapshot
-> terminal events/final answer
```

Most important regressions happen between these owners rather than inside one
function.

## Tool Contract Checklist

When adding or changing a tool, inspect:

- schema name, arguments, bounds, and `additionalProperties`;
- handler signature and return format;
- risk registration and default visibility;
- preflight behavior before approval;
- workspace, symlink, sensitive-data, timeout, and output limits;
- exact Tool Result `tool_call_id` closure in `agent.py`;
- focused handler tests plus registry/Agent integration tests.

Do not assume a schema constraint authorizes execution or replaces runtime
validation. The model can emit malformed or disabled calls.

## Message and Context Checklist

When changing OpenAI message shape or Agent sequencing, inspect:

- `_assistant_message_to_dict` round-trip preservation;
- tool-batch ordering and one result per call ID;
- Session tail analysis and interrupted recovery;
- snip/reactive compaction boundaries;
- active compaction after mixed tool batches;
- snapshot timing before and after side effects;
- Fake Client request assertions.

Do not compact or finalize in the middle of an unresolved tool batch.

## Configuration and CLI Checklist

When adding a CLI flag or setting, decide explicitly:

- whether it is a per-run capability or durable Session identity;
- where validation belongs (`argparse`, config loading, or runtime);
- whether it changes the tool schemas sent to the model;
- whether resume must match, ignore, or re-request it;
- what warning/event is safe to display;
- which parse, main-wiring, help, and resume tests need updates.

`allow_run_command` is the reference per-run capability: it changes dynamic
schemas but is deliberately not persisted in Session. It is valid only with a
per-run user-selected local sandbox image. Resolve the image to an immutable
local ID before the first model request, pass the config explicitly, and never
let model tool arguments modify it.

## Durable-State Checklist

For Session, transcript, or Tool Result writes:

- validate size and path before writing;
- reject unsafe symlink/path shapes;
- use a same-directory temporary file plus `os.replace`;
- use mode `0600` for private runtime artifacts;
- save exact data before replacing it with a summary/marker;
- test crash-sensitive states such as pending and in-flight tools.

## Security Review Questions

- Can untrusted file/tool/summary text authorize a new action?
- Can a model-visible tool bypass the per-run allow-set?
- Can approval occur before parameter validation?
- Can a path resolve outside the workspace or into internal/sensitive files?
- Can a resumed operation repeat an unknown side effect?
- Does an error/event expose arguments, output, or secrets?
- Is a Harness-level defense being overstated as OS isolation?

Every high-severity finding must be traced to current source and reproduced or
covered by a focused test before changing behavior.
