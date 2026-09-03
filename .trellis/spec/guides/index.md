# Project Thinking Guides

> Cross-cutting checks for changes that span Vibe Harness modules.

## Available Guides

| Guide | Use it when |
|---|---|
| [Code Reuse Thinking Guide](./code-reuse-thinking-guide.md) | Adding constants, validators, schemas, helpers, or another capability |
| [Cross-Layer Thinking Guide](./cross-layer-thinking-guide.md) | Changing tools, messages, CLI flags, context, Session, or durable artifacts |

## Quick Routing

- New or changed tool: read both guides and the backend tool/permission spec.
- Message/context/session change: read the cross-layer guide and the backend
  context/session spec.
- New validation or shared limit: read the reuse guide and search all source
  and test consumers before editing.
- Security finding: verify it against current source and tests before treating
  it as a defect; distinguish Harness controls from OS/container isolation.

These guides are prompts for analysis. Executable coding contracts live in
`.trellis/spec/backend/` and remain the source of truth for implementation and
review.
