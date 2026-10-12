# Desktop and local import contracts

## Scope / trigger
GuMi-Code desktop is a separate Electron host over the current repository's Python package. Workbench identity, storage and credentials are preserved. CLI commands and Docker services remain compatible; desktop commands execute locally with per-call approval.

## Signatures
Named `gumi:*` IPC exposes coding, memory, files, API settings, language, URL opening and `importWorkbench`. Import calls `migrateWorkbench({sourceUserData,targetDataDir,sourceConfirmedClosed:true,runtime,credentials})`; returns counts and imported/alreadyImported only. `GUMI_CODE_PROFILE` isolates development storage. Packaged Python and source resolve within the application.

## Contracts
Only the exact application renderer main frame can invoke IPC. Credentials stay in main-process memory and Keychain; metadata/results never expose them. A fixed connection test sends only synthetic test text. Native-selected workspace/file capabilities remain host-owned.

Import requires the source closed and an empty target. Preserve session/workspace UUIDs and memory references; project bindings become canonical workspace bindings. Hold available source session leases, check fingerprints, snapshot SQLite read-only, convert legacy journals only in a private staging directory, validate all history through the actual adapter, then publish atomically. Use fresh target credential slots and read back before publication. Preserve source files/slots and roll back new slots on failure. Imported scopes restore only validated available directories; moved directories require selection again.

Settings has code and language categories with gear icons; Back hides the outer settings page. Keep API, memory and AGENTS editors mounted across category changes. Cancellation retains unsent drafts and never reports successful completion.

## Validation / error matrix
Wrong sender/frame, unknown payload, credential path, stale edit revision or unsupported URL → reject with no new capability/write. Nonempty target, active/changed source, unknown files/schema, symlink or SQLite sidecar → refuse migration without overwriting. Denied credential read/write or failed publication → sanitized error and rollback. Corrupt memory remains on disk; shutdown still closes the window safely. Never report a provider error body or raw subprocess diagnostics.

## Good / base / bad cases
Good: migrated history reopens after restart; memory UUID scope and pending input IDs survive; duplicate import is idempotent. Base: empty app has no configured provider; native picker cancellation creates no session. Bad: treating imported arbitrary project paths as grants, replacing populated target, copying old plaintext secrets into files, or claiming mocked model output proves live tool support.

## Required tests
Node host tests cover approval binding/cancel, streaming/live input, files/AGENTS, memory and shared API; migration tests cover segmented/legacy replay, source preservation/change, locks/symlinks/schema, empty target, idempotency and credential/publication failure rollback. Python checks cover desktop protocol, project rules, CLI compatibility and legacy conversion. Build, native layout/draft behavior and relocated bundle are separate checks. A live fixed-text API test proves connection only.

## Wrong / correct
Wrong: copy a v1 session and report success when desktop requires v3. Correct: convert and replay its staged copy before publishing. Wrong: unmount an editor when changing settings category. Correct: keep it mounted and hidden, so unsaved content remains local.
