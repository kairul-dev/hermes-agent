# Hermes persistence failure contract

Normal Forge/TUI conversations are durable. Before a user turn is dispatched,
Hermes verifies the session key, opens the session's profile database, confirms
the exact session row after creation, and persists any branch seed without
discarding its parent lineage. The initial user input must then pass through
the existing session persistence path before model or tool execution begins.

Required failures (missing key, unavailable store, failed or no-op creation,
failed initial write, and failed incremental write) return a sanitized
persistence error containing the operation/stage, safe session identity,
exception type, and SQLite code/name when available. They do not start or
continue provider/tool work. Saved prefixes remain intact; already-executed
tool side effects are not claimed to be reversible.

The strict contract is limited to normal Forge/TUI agents. Explicitly
non-persistent internal and test contexts continue to use their existing
opt-out. No schema migration is required: the fix verifies and uses the
existing sessions, parent-session, and messages schema.
