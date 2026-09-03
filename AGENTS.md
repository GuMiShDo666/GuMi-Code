<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

## Project Git Workflow

Every project update must use the standard Git workflow:

1. Start from an up-to-date `main` branch using a fast-forward-only pull.
2. Create a scoped branch named `feat/*`, `fix/*`, `docs/*`, `test/*`, or
   `chore/*`; do not commit ordinary changes directly to `main`.
3. Make only task-related changes and run focused checks plus the relevant
   full test suite.
4. Review `git status`, the diff, and staged filenames. Confirm `.env`, keys,
   tokens, Session files, transcripts, and generated tool outputs are absent.
5. Stage explicit paths, create a Conventional Commit, and push the branch.
6. Open a pull request for review before merging to `main`.

Do not force-push, rewrite published history, or bypass failed validation
unless GuMiShDo explicitly requests and approves that exception.
