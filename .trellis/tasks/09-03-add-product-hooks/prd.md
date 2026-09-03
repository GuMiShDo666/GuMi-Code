# Add product lifecycle Hooks

## Goal

Add an in-process four-event Hooks system adapted from learn-claude-code s04 while preserving the existing non-bypassable permission and context safety boundaries.

## Requirements

- Provide an in-process Hook registry for Python callers without adding a new
  dependency, configuration format, or external command runner.
- Support the four lifecycle events from the s04 reference:
  `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`.
- Run callbacks in registration order. The first non-`None` result stops the
  remaining callbacks for that event.
- Treat a non-`None` `PreToolUse` result as an additional veto for the current
  tool call. A Hook must never authorize a call, bypass hard-deny policy,
  bypass preflight validation, or replace terminal approval.
- Run `PostToolUse` only after an enabled registry tool handler has actually
  been attempted. Do not run it for Gate 0 failures, unknown tools, policy
  denials, approval denials, duplicate-denial suppression, Agent control tools,
  or `PreToolUse` vetoes.
- Trigger `UserPromptSubmit` for the current user request before that request
  is appended to model-visible messages. Its return value is observational and
  does not rewrite the request.
- Trigger `Stop` before a natural no-tool return. A non-empty string adds a new
  user message and continues the loop, while the existing `max_steps` bound
  remains authoritative.
- Preserve OpenAI Tool Call/Tool Result pairing, Session snapshot safety,
  context-compaction ordering, concise terminal output, and existing public
  behavior when no Hook registry is supplied.
- Pass defensive copies of mutable Hook inputs so callbacks cannot mutate live
  tool arguments or message history.
- Document the programmatic API and its security boundary. Do not claim that
  Hooks are a sandbox or permission replacement.

## Acceptance Criteria

- [x] A caller can register callbacks for all four event names and pass the
  registry to `run_agent()`.
- [x] Callback order and first-result short-circuit behavior have focused unit
  tests, including invalid event handling and registry-instance isolation.
- [x] A `PreToolUse` veto returns a matching OpenAI Tool Result and invokes
  neither approval nor the tool handler.
- [x] Hard deny, risk policy, preflight, approval, and handler enforcement stay
  independent of Hooks; Hooks can only make execution stricter.
- [x] `PostToolUse` observes the final bounded tool result only after a handler
  attempt and cannot mutate live arguments.
- [x] `Stop` can request another bounded model step, and cannot create an
  unbounded continuation beyond `max_steps`.
- [x] Existing runs without Hooks remain behavior-compatible.
- [x] Focused Hook/Agent/registry tests and the complete test suite pass.
- [x] README, HANDOFF, and Trellis backend specifications describe the current
  implementation and remaining limitations accurately.

## Notes

- Reference behavior: `shareAI-lab/learn-claude-code/s04_hooks`.
- Out of scope for this task: external process Hooks, shell Hooks, user config,
  async callbacks, Hook discovery/loading, built-in auto-git behavior, and
  moving Permission into the Hook registry.
