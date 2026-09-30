# Generic local-service authentication, contract v1

A local operator provisions a named principal with an immutable credential,
explicit HTTP method/route-template, WebSocket, JSON-RPC and server-response
grants, expiry and typed restrictions. This does not replace Dashboard Basic or
OAuth. A client authenticates with `Authorization: Bearer <credential>` over a
direct loopback connection. No query credential, browser ticket or cookie is
needed for this path. Forwarded/proxy headers and browser Origin on service
WebSockets are rejected. Configure the actual Dashboard authentication gate;
loopback alone never authenticates a service.

The attacker model includes unauthenticated local processes, remote requests,
forged proxy/principal fields, compromised browser sessions, stolen/revoked
credentials and clients that bypass their own allowlists. The operator and OS
account running Hermes are trusted. An administrator or process running as that
account can read process memory and private files; this is not a sandbox against
the OS owner. A stolen active credential retains only its explicit authority
until expiry/revocation. HTTPS is still needed for human remote access.

## Provisioning

With an existing protected, isolated or operator-selected `HERMES_HOME`, run the
matching Hermes interpreter directly:

```
python -m hermes_cli.dashboard_auth.service_cli create --principal client-name --expires 2026-10-01T00:00:00Z --grants /absolute/grants.json --output /absolute/private/client.credential
python -m hermes_cli.dashboard_auth.service_cli list
python -m hermes_cli.dashboard_auth.service_cli inspect CREDENTIAL_ID
python -m hermes_cli.dashboard_auth.service_cli revoke CREDENTIAL_ID
```

These commands avoid the interactive CLI/bootstrap and do not disable the gate.
Provisioning prints metadata only. The one-time secret is 384 random bits,
encoded as `hls1.<random credential id>.<secret>`. Exclusive file creation refuses
overwrite. The private file is secured and verified before any secret is written.
Do not put its path within a managed-file or project root, include it in backups,
logs, environment variables, browser code, URLs or source control. Provisioning
rejects granted roots containing the output file or identity HOME.

Hermes stores only a SHA-256 verifier, credential ID, principal, expiry, revocation
flag and grants in `HERMES_HOME/local-services/identities.db`. High-entropy random
secrets make offline guessing impractical. Comparisons use constant-time digest
comparison. Metadata/self inspection never returns a verifier or secret.

## Permissions

On Windows the protected DACL permits only the current account and SYSTEM, with
inheritance disabled. The exact DACL is checked on each read/store access.
Symlinks, reparse points and hard-linked files are rejected. On Unix files require
owner UID and mode 0600; the identity directory requires 0700. Unknown or unsafe
permissions fail closed. Windows qualification is exercised here; Unix behavior
requires qualification on a real Unix host. Protect parent directories from
untrusted writers: the operator-selected path and its namespace are trusted, and
these checks do not provide a race-proof filesystem sandbox against the OS owner.

## Grants and restrictions

Every list is explicit, with no wildcard/default all-access grant. HTTP matching
uses the server's actual route template and method. Unknown/wrong-method requests
are denied before handlers. `/api/ws` admission is a separate grant; it provides
no RPC authority. The bound transport carries server-verified identity. Admission,
inline handlers and queued workers reread expiry/revocation before dispatch.
Response frames require an open approval/clarify request belonging to the same
transport; unknown/foreign/alternate compute-host responses are denied.

Contract v1 is deliberately limited to the launch profile (`profile_ids:
["current"]`). Other profiles and client-supplied principal/HOME overrides are
denied. Active-session listings filter other profiles; service transports do not
receive global events that could describe another profile. Read grants generally
cover the selected profile, rather than per-user confidentiality within it.
Project paths/folder arrays and existing project IDs are checked against canonical
project roots. New sessions default to a granted root. Managed-file access also
requires Hermes's locked root to equal the grant. The underlying file handler
enforces paths on streaming uploads. This does not sandbox agent tools invoked
by an explicitly granted prompt; operator tool/approval policy remains relevant.

Settings use explicit read/write key lists and known values. Model/reasoning
changes require a live session. Plugins allow read-only list/onboarding, never
install/enable/disable. MCP mutations name explicit operator-approved resources,
add only the matching catalog preset, and cannot supply executable configuration.
Cron uses the launch profile, explicit field names and local delivery; script,
alternate profile and remote delivery parameters are denied. Task/session HTTP
mutations permit only the fields used by the client contract. Method availability
alone is not permission to grant an operation whose parameters cannot be scoped.

Authenticated `GET /api/auth/service-capabilities` returns only the caller's
contract version, principal, credential ID, expiry, revocation state, grants and
non-secret restrictions. Its own HTTP grant is required.

## Rotation, revocation and recovery

Create B for the same principal while A remains active, write B to a separate
protected staging file, verify B with self inspection, atomically replace the
client's configured protected file, reconnect the client, then revoke A by its
credential ID. Each HTTP request/new socket uses persisted state. Queued workers
recheck before invoking a handler. Active service sockets poll durable state every
250 ms, interrupt attached turns and close with code 4401 on expiry/revocation.
Polling is not a grace period for subsequent RPCs. An operation already executing
may have effects that interruption cannot undo; revocation is not a transaction
rollback or an OS process sandbox. Completed effects and detached work need the
operator's normal resource/agent policy.

The verifier DB survives restart; revoked/expired credentials remain denied.
Lost plaintext is unrecoverable: revoke it and provision a replacement. Missing,
malformed or unsafe stores fail closed; restore a protected verifier DB only from
an operator-controlled backup and account for revoked credentials before restart.
If a credential leaks, revoke it, replace it and investigate affected resources.

External hosts must feature-detect `hermes-local-service-v1` and validate their
principal, expiry, exact grants and restrictions. Unknown contract versions must
fail closed. Health advertises the non-secret contract marker; semantic version
alone is insufficient. Never fall back to an ungated server or human credentials.

Kanban is a shared coordination bus. Service board selection must reference a
project in the launch profile and within granted roots. Board/task views filter
out disallowed projects, workspaces and foreign assignees. Service-created tasks
are assigned to the launch profile; status changes require that ownership.
