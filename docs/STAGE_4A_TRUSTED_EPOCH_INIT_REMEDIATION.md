# Stage 4A Trusted Epoch Initialization Remediation

Date: 2026-09-04

Final implementation status: **READY FOR FOURTH INDEPENDENT RE-REVIEW**

This is an implementation report, not approval to merge. Stage 4B and Stage 5
were not started.

## Revisions and provenance

- Reviewed SHA preserved unchanged:
  `46c402f4de086a4ce4113344724c8bfe1a86a10c`
- Dedicated remediation branch:
  `forge/stage-4a-trusted-epoch-init-fix`
- Failing-regression commit:
  `2cd6b94d76` (`test(usage): reproduce complete trusted cutover erasure`)
- Final implementation/test SHA:
  `21e500c886a869f45a8fa8f7f153fb0c4601f1be`
- Report-bearing SHA: the separate commit containing this document; its exact
  object id is recorded in the final handoff because a commit cannot contain
  its own hash.

All previous review and remediation reports were preserved. The reviewed
branch, `forge/runtime`, fork `main`, and Hermes Forge were not modified.

## S4A-TIRR-01 reproduction

The permanent regression establishes this trusted state:

| State | API calls | Input tokens |
|---|---:|---:|
| Trusted baseline | 3 | 105 |
| Detail high-water | 0 | 0 |
| Current aggregate | 5 | 115 |
| Eligible post-cutover detail | 1 | 5 |
| Aggregate delta | 2 | 10 |

Before corruption, the response is correctly `PARTIAL` because aggregate
delta `2 / 10` does not equal detail `1 / 5`.

The regression then deletes both v3 metadata records and drops the trusted
baseline table. At the reviewed SHA, writable reopen treated the database as
virgin, captured `5 / 115`, moved the high-water to `1`, and could report
incorrect `COMPLETE` zero.

The remediation retains two independent activation proofs: the trusted epoch
identity table and `schema_version.trusted_usage_epoch_generation`. After the
same erasure, read-only accounting is unavailable and writable startup fails
closed. The epoch identity and schema generation remain unchanged, the
high-water is not advanced, and no replacement baseline is created.

An extended regression also removes the epoch identity table together with
both metadata records and the baseline. The schema-bound generation remains
`1`, proves prior activation, and prevents reinitialization.

## S4A-TIRR-02 reproduction

The prior initializer ran generic schema DDL before entering the cutover
transaction. A failure after that DDL left a canonical but empty baseline
table. On the next writable open, mere table preexistence classified the
fresh database as damaged, permanently poisoning legitimate initialization.

The crash matrix injects failures before DDL, after table creation, after
deferred index creation, throughout trusted-state capture, immediately before
commit, and immediately after commit. Every precommit failure either leaves no
schema or a recognizable canonical schema shell with schema generation `0`.
Restart safely retries the same first activation. A postcommit failure leaves
a complete active epoch which restart validates without recapturing data.

## Root causes

S4A-TIRR-01 was caused by using absence of the current marker, epoch metadata,
and baseline as sufficient proof of virgin state. All of those records were
ordinary cutover artifacts, so their joint erasure removed the only memory of
prior activation.

S4A-TIRR-02 was caused by treating any pre-existing baseline table as evidence
of a committed cutover even though SQLite schema preparation had already
committed that empty table before activation began.

## Authoritative epoch identity

Trusted activation now has three mutually bound representations:

1. `schema_version.trusted_usage_epoch_generation`, initialized to `0` and
   atomically advanced to `1` on the first committed trusted epoch;
2. singleton table `session_usage_trusted_epoch`, containing the positive
   generation, immutable random epoch id, and activation timestamp;
3. mirrored version-4 reconciliation marker/integrity metadata containing the
   same generation and epoch id plus baseline digest/count and detail
   high-water.

The schema-generation field uses Hermes' existing authoritative schema state
and is independent of the ordinary cutover tables and metadata. Normal
startup never decrements it, resets it to zero, or creates a later epoch. Any
missing, malformed, or inconsistent binding after generation reaches `1` is
damage and fails closed.

Valid v2 and v3 cutovers upgrade atomically to the new representation without
reading current mutable aggregates, changing the trusted baseline, moving the
cutover timestamp, or advancing the detail high-water.

## State machine

| State | Authoritative evidence | Permitted behavior |
|---|---|---|
| `NEVER_INITIALIZED` | Schema generation absent/`0`; no epoch identity or cutover metadata; baseline did not preexist | May create the first trusted epoch |
| `INITIALIZATION_INCOMPLETE` | Schema generation absent/`0`; no committed epoch/cutover metadata; only an exact canonical empty baseline shell may remain | May retry the same first initialization; no partial capture is trusted |
| `TRUSTED_EPOCH_ACTIVE` | Positive schema generation, singleton epoch identity, v4 marker/integrity pair, and digest-bound baseline all agree | Validate only; never recapture or move the boundary |
| `TRUSTED_EPOCH_DAMAGED` | Active-era evidence is missing, invalid, or inconsistent, or a noncanonical/nonempty preactivation shell exists | Read unavailable; writable startup fails closed; never becomes virgin |

Only positive proof of the first two states authorizes aggregate baseline
capture. “No v3 artifacts found” is never sufficient once schema generation
or any other active-era evidence exists.

## Atomic initialization protocol

Generic schema preparation may commit before activation. If interrupted, its
only trusted-cutover residue is a canonical empty baseline shell and a schema
generation of `0`, which is explicitly recoverable.

The authoritative transition is one `BEGIN IMMEDIATE` transaction:

1. validate first-initialization eligibility;
2. create the trusted epoch identity table;
3. insert generation `1` and the immutable epoch id;
4. set `schema_version.trusted_usage_epoch_generation = 1`;
5. capture the complete aggregate route baseline;
6. capture the detail event-id high-water;
7. refresh compatibility-only activation state;
8. write the v4 integrity binding;
9. write the v4 trusted activation marker;
10. commit.

SQLite DDL for the epoch identity table is inside this transaction. Any
exception before commit rolls back the table, identity, schema generation,
baseline rows, high-water binding, and markers together. A failure delivered
after successful commit does not undo or disguise the active epoch.

## Crash matrix

| Injection point | Durable result | Restart result |
|---|---|---|
| Before any DDL | No activation state | Safely initializes |
| Immediately after table creation | Canonical schema shell; generation `0` | Safely retries |
| After index creation | Canonical schema shell; generation `0` | Safely retries |
| Before epoch/generation establishment | Transaction not committed | Rolls back and retries |
| After epoch identity but before baseline | Epoch table/row and schema generation roll back | Safely retries |
| During baseline capture | Partial capture rolls back | Safely retries |
| After baseline but before high-water | Baseline rolls back | Safely retries |
| After high-water but before integrity binding | Boundary and baseline roll back | Safely retries |
| After integrity binding but before marker | Integrity record and all activation state roll back | Safely retries |
| Immediately before commit | Entire activation rolls back | Safely retries |
| Immediately after successful commit | Complete active epoch remains | Validates as `TRUSTED_EPOCH_ACTIVE` |

The test reopens every case twice. Precommit cases create exactly one first
epoch on recovery; postcommit state remains unchanged.

## Corruption matrices

The exact 12-case matrix present at the reviewed SHA continues to pass:

| Case | Mutation | Result |
|---:|---|---|
| 1 | Trusted marker deleted | Unavailable / writable fail closed |
| 2 | Baseline table deleted | Unavailable / writable fail closed |
| 3 | Marker high-water deleted | Unavailable / writable fail closed |
| 4 | Integrity metadata deleted | Unavailable / writable fail closed |
| 5 | One baseline route deleted | Unavailable / writable fail closed |
| 6 | Multiple baseline routes deleted | Unavailable / writable fail closed |
| 7 | Marker deleted and integrity high-water deleted | Unavailable / writable fail closed |
| 8 | Both marker and integrity metadata deleted | Unavailable / writable fail closed |
| 9 | Baseline and marker deleted while integrity remains | Unavailable / writable fail closed |
| 10 | Baseline value altered | Digest mismatch / fail closed |
| 11 | High-water altered | Metadata mismatch / fail closed |
| 12 | Baseline hash altered | Metadata mismatch / fail closed |

The extended epoch matrix adds:

| Case | Mutation | Result |
|---|---|---|
| 1 | Baseline table recreated empty after activation | Active generation proves damage |
| 2 | Epoch identity row deleted | Fail closed |
| 3 | Epoch identity table deleted | Schema generation proves damage |
| 4 | Epoch identity generation altered | Generation mismatch / fail closed |
| 5 | Active schema shell with epoch row, baseline, and metadata missing | Fail closed |
| 6 | Integrity metadata generation altered | Integrity mismatch / fail closed |
| 7 | Epoch table plus all ordinary cutover artifacts removed | Schema generation proves damage |
| 8 | Schema generation altered | Identity mismatch / fail closed |

Every corruption fixture retains aggregate `5 / 115` and detail `1 / 5`.
None returns `COMPLETE`, none snapshots `5 / 115`, and none advances the
high-water.

## API behavior

A read-only damaged store returns unavailable accounting with no totals. If
the dashboard's stale-schema path attempts a writable heal, trusted-accounting
damage is translated to HTTP 503 with a stable public message rather than an
uncaught 500 or sensitive internal details.

## Verification

All pytest commands used `scripts/run_tests.sh` through Git Bash and the
checkout's Windows virtual environment.

| Suite | Result |
|---|---|
| Focused trusted epoch, crash, remediation, detail, and API group | 108 passed |
| Trusted-cutover file, including original 12-case and extended matrices | 51 passed |
| Accounting/persistence six-file producer group | 59 passed |
| Usage API suite | 13 passed |
| `tests/test_hermes_state.py` | 259 passed, 2 skipped |
| Schema/repair four-file group | 36 passed, 1 established failure, 4 skipped |
| Dashboard/auth/profile six-file group | 284 passed, 2 established outcomes, 5 skipped |
| Expanded adversarial/lineage/cutover group | 94 passed |
| Deterministic cutover write-lock test, isolated | 1 passed |
| Dashboard clamp file, three isolated fresh-process repetitions | 9 passed each run |

Static and structural checks:

- Ruff 0.16.6 on all changed Python files: passed.
- Repository-wide Ruff 0.16.6: passed with the same two pre-existing invalid
  `noqa` warnings.
- Python compilation on all changed Python files: passed.
- `git diff --check 46c402f4...`: passed.
- On a 20,000-row fixture, the production event projection uses covering index
  `idx_session_usage_events_session_time (session_id=? AND recorded_at>?
  AND recorded_at<?)`. The untouched reviewed SHA produces the identical
  plan.

The two established environment-sensitive failures were reconfirmed with
their prior signatures:

1. `test_repair_rebuilds_stale_btree_indexes`: SQLite 3.45.1 reports
   `row N missing from index` instead of the test's expected
   `wrong # of entries in index` wording.
2. `TestSystemStatsEndpoint.test_stats_shape`: the hermetic Windows runner has
   an empty `PROCESSOR_ARCHITECTURE`, so `arch` is empty.

The dashboard group also reproduced the previously reported cross-file clamp
flake once (`test_in_range_limit_accepted` observed an in-progress schema
mutation from another test process). The entire clamp file then passed three
consecutive isolated fresh-process runs. No Stage 4A production path was
changed in response to that known test-isolation failure.

## Conclusion

S4A-TIRR-01 is resolved because prior activation survives complete ordinary
cutover-artifact erasure and can no longer silently become a fresh epoch.
S4A-TIRR-02 is resolved because uncommitted first initialization leaves only
explicitly recoverable generation-zero state, while all trusted identity and
accounting captures commit atomically. Previous Stage 4A accounting,
snapshot, validation, repair, API, authentication, and profile-isolation
regressions remain covered.

Final implementation status: **READY FOR FOURTH INDEPENDENT RE-REVIEW**
