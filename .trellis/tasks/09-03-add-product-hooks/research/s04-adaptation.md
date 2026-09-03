# s04 Hooks Adaptation Notes

## Source

- `https://github.com/shareAI-lab/learn-claude-code/blob/main/s04_hooks/README.zh.md`
- Local mirror: `../learn-claude-code/s04_hooks/README.zh.md`
- Local reference implementation: `../learn-claude-code/s04_hooks/code.py`

## Retained Concepts

- Four fixed lifecycle events cover prompt, pre-tool, post-tool, and stop.
- Callbacks register outside the Agent Loop and run in registration order.
- The first non-`None` result short-circuits a trigger.
- `PreToolUse` may veto a call and `Stop` may request continuation.

## Required Project Adaptations

- Use OpenAI `assistant.tool_calls` and matching `role=tool` messages.
- Keep Vibe Harness hard deny, risk policy, preflight, and approval outside and
  authoritative over Hooks.
- Keep `shell=False`, isolated child environment, workspace boundaries, output
  caps, and sensitive-path rules.
- Do not print Hook arguments/results directly; terminal output remains bounded
  by the existing event renderer.
- Use per-instance state to avoid callback leakage between runs/tests.

## Rejected Reference Details

- Moving Permission entirely into an optional Hook.
- Shell-string execution with `shell=True`.
- Treating substring command lists as a complete security boundary.
- Logging raw argument values by default.
- Anthropic content-block message structures.
