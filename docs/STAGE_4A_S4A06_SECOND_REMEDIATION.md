# Stage 4A S4A-06 Second Remediation — Trusted Accounting Cutover

Date: 2026-09-04

Final implementation status: **READY FOR SECOND INDEPENDENT RE-REVIEW**

This report records implementation evidence only. It is not approval to merge.
Stage 4B and Stage 5 were not started.

## Revisions and provenance

- Failed remediation SHA preserved unchanged:
  `50706d1e448fe5d1ac404d55596369dfa4dfeca3`
- Original reviewed Stage 4A SHA preserved unchanged:
  `87efab652f8f7a3ea64a48ce86b693e9e6e259e7`
- Runtime base preserved unchanged:
  `4c4b37a7d792924f6e060baac2bed069fc85ddb8`
- Dedicated branch: `forge/stage-4a-s4a06-cutover-fix`
- Implementation and test HEAD before this report-only commit:
  `a6ecfa63fef369e3ad79a6f72e652cb6e4ef9a78`

The independent re-review was first preserved in its own documentation commit.
The exact failing reproduction was then preserved in a separate test commit
before any production source changed.

## Exact blocker reproduction

The regression constructs the independently reviewed upgrade state:

| State before upgrade | API calls | Input tokens |
|---|---:|---:|
| Current aggregate | 3 | 105 |
| Existing detail | 1 | 1 |

Against production source at failed-remediation SHA `50706d1e...`, the new
test failed with:

```text
expected baseline: (3, 105)
actual baseline:   (2, 104)
```

That is the original blocker: the migration treated the difference as history
and could return `COMPLETE` for only `1 call / 1 token`.

At the second-remediation implementation, the same upgrade records:

```text
trusted baseline:            3 calls / 105 tokens
pre-cutover detail high-water: event id 1
post-cutover aggregate delta: 0 calls / 0 tokens
eligible post-cutover detail: 0 calls / 0 tokens
```

A wholly post-cutover zero-use window is therefore `COMPLETE` zero. After a
legitimate new event adds `1 call / 5 tokens`, the same window is `COMPLETE`
with exactly `1 call / 5 tokens`.

## Why aggregate minus detail was unsound

An upgraded v1 store provides no proof that an existing detail row and an
existing aggregate value belong to the same trustworthy epoch. Subtracting
detail from the current aggregate assumes every residual predates detail
activation. A missing post-activation event produces the same numerical
residual as legitimate historical aggregate, so subtraction can launder the
missing event into a fabricated historical baseline.

The second remediation performs no such subtraction. Numerical equality also
does not establish provenance: old detail is excluded whether it is absent,
partial, equal to aggregate, or greater in some dimension.

## Trusted-cutover architecture

The internal cutover marker is:

```text
session.usage.detail.reconciliation.v2.trusted_cutover
```

The public capability remains `session.usage.detail.v1`; no public endpoint or
request shape changed.

On the first writable open without a valid v2 marker, initialization:

1. Reconciles the required detail and baseline table columns.
2. Executes `BEGIN IMMEDIATE`, acquiring SQLite's database write authority.
3. Captures the complete current `session_model_usage` state into an immutable
   route-level baseline.
4. Captures `MAX(session_usage_events.id)` as the pre-cutover detail high-water.
5. Computes a canonical SHA-256 digest and row count for the complete baseline.
6. Writes the cutover timestamp, high-water, count, and digest as one marker.
7. Commits the baseline snapshot and marker together.

Existing detail rows are retained unchanged for forensic/history purposes.
Rows at or below the event-id high-water cannot participate in v2 exactness.

## Schema changes

`session_usage_reconciliation_baseline` mirrors the aggregate route key:

```text
session_id
model
billing_provider
billing_base_url
billing_mode
task
```

It stores every exact accounting metric for each route. This is deliberately
more precise than the old per-session baseline: missing usage on one model,
provider, billing route, mode, or task cannot be canceled by excess detail on
another route.

The old `session_usage_activation_baseline` table and v1 metadata remain for
compatibility, but they are no longer reconciliation authority. On v2 cutover
they receive the same full aggregate snapshot/timestamp; no detail is
subtracted.

## Atomicity proof

The baseline delete/insert, legacy compatibility snapshot, v2 marker, baseline
row count, digest, cutover time, and event high-water are in one explicit
SQLite transaction. Any exception rolls the transaction back.

The rollback regression installs a trigger that aborts the v2 marker insert
after baseline capture. After failed initialization, direct inspection finds
neither marker nor baseline rows. Removing the trigger and reopening performs
one complete cutover. Thus a crash-equivalent failure cannot expose marker
without baseline, baseline without marker, or a partially committed session
set.

The marker binds the immutable baseline by count and SHA-256 digest. Writable
startup raises on a marker whose baseline is incomplete. A read-only usage API
request against such state returns `UNAVAILABLE`. Baseline rows without a
marker are untrusted and are replaced by a new full cutover under the write
lock.

## Concurrency proof

`BEGIN IMMEDIATE` is acquired before either aggregate or detail watermarks are
read. Every normal model-usage update uses the same SQLite write-lock boundary
through `SessionDB._execute_write`.

Therefore a writer must fall into one of two complete cases:

- It commits before cutover acquires the lock. Its aggregate is in the full
  baseline and its detail id is at or below the high-water.
- It commits after cutover commits. Its aggregate contributes to the delta and
  its detail id is above the high-water.

The deterministic concurrency regression pauses cutover after its aggregate
snapshot while the transaction remains open. A separate `BEGIN IMMEDIATE`
writer is rejected as locked. After cutover commits, the normal accounting
writer succeeds and reconciles exactly. No application-level readiness flag is
used.

## Reconciliation semantics

For every selected physical session and complete aggregate route:

```text
current route aggregate - trusted route baseline
    ==
sum(detail rows with id > cutover high-water)
```

Every count and cost dimension is compared. Integer dimensions compare
exactly; cost dimensions retain the established bounded floating comparison.
The route union includes current aggregate, baseline, and detail-only routes,
so missing or extra state on either side fails closed.

Public window totals and event projections additionally require both:

```text
event id > cutover high-water
recorded_at in [effective_start, effective_end)
```

This preserves timestamp-window semantics while preventing any pre-cutover
row, including a future-dated old row, from establishing v2 exactness.

## Coverage semantics

- Entirely before cutover: `UNAVAILABLE`; no precision is inferred from old
  detail.
- Crossing cutover: `PARTIAL`; only eligible detail at/after the exact boundary
  is returned.
- Wholly after cutover: `COMPLETE` only when every route and metric reconciles.
- Zero post-cutover use: `COMPLETE` zero when aggregate equals baseline and no
  eligible detail exists.
- Missing post-cutover detail: `PARTIAL` with
  `aggregate_detail_mismatch`; the baseline is immutable and cannot absorb it.
- Future-ended windows retain the existing partial/not-yet-complete behavior.

## Per-session and lineage behavior

Sessions existing at cutover receive route-level baseline rows only where
aggregate usage exists. Sessions created later need no fabricated zero rows;
absence naturally means a zero baseline.

Compression-lineage reads reconcile each physical session and route before
combining their public event totals. A regression covers an existing parent
with historical usage, a parent event after cutover, a child created by
compression after cutover, and another independent existing session. The
lineage returns `COMPLETE` only for the exact post-cutover parent/child events.

## Schema repair and idempotency

The new table participates in declarative column reconciliation. Tests cover:

- missing v2 marker and baseline state;
- a partial v2 baseline table repaired before cutover;
- marker with a deleted baseline row (fail closed);
- baseline without marker (new trusted cutover);
- rollback followed by reopen;
- repeated initialization with byte-for-byte stable marker and rows.

An empty database and a database with no existing aggregate produce a valid
zero-row baseline whose empty-set digest is bound into the marker.

## S4A-01 and S4A-07 interactions

The S4A-01 regression queues a post-cutover event, forces its flush to fail,
and verifies the API returns `UNAVAILABLE/accounting_flush_failed`. The queued
event remains retryable; after retry it appears exactly once and coverage is
`COMPLETE`. Cutover never converts the failed item into baseline history.

The S4A-07 pinned read transaction is unchanged. Marker, complete baseline,
current aggregate, eligible detail, totals, routes, and returned events are
read through the same synchronized SQLite snapshot.

## Verification results

All pytest commands used `scripts/run_tests.sh` through Git Bash and the
checkout's Windows virtualenv.

| Suite | Result |
|---|---|
| New trusted-cutover suite | 14 passed |
| `test_session_usage_detail.py` + usage API | 29 passed |
| Accounting/persistence six-file group | 59 passed |
| `test_hermes_state.py` | 259 passed, 2 skipped |
| Schema/repair four-file group | 36 passed, 1 known baseline failure, 4 skipped |
| Dashboard/auth/profile six-file group | 284 passed, 1 known baseline failure, 5 skipped |
| Established adversarial/lineage plus new cutover suite | 57 passed |

The original failing test was run before production changes and failed with
the independently reported `(2, 104)` derived baseline. The final targeted
run passes all 14 parametrized outcomes.

Static and structural checks:

- Ruff 0.16.6 on every changed Python file: passed.
- Repository-wide Ruff 0.16.6: passed with the same two pre-existing
  invalid-`noqa` warnings.
- Python compilation on every changed Python file: passed.
- `git diff --check` from failed-remediation SHA through implementation/test
  HEAD: passed.
- Deterministic cutover concurrency test: passed.
- Standalone SQLite query plan:
  `SEARCH session_usage_events USING INDEX
  idx_session_usage_events_session_time (session_id=? AND recorded_at>?
  AND recorded_at<?)`.

## Baseline failure status

The two established failures remain signature-for-signature identical to the
untouched runtime-base results:

1. `test_repair_rebuilds_stale_btree_indexes`: SQLite 3.45.1 reports
   `row N missing from index` rather than the test's expected
   `wrong # of entries in index` wording.
2. `TestSystemStatsEndpoint.test_stats_shape`: the hermetic Windows runner
   returns an empty `arch` because `PROCESSOR_ARCHITECTURE` is absent.

Neither failure touches the Stage 4A diff.

## Compatibility implications

- The public capability and HTTP response contract remain v1.
- A new additive internal baseline table and state metadata key are created on
  writable open.
- Old detail and aggregate rows are never deleted, rewritten, retimestamped,
  or converted into manufactured events.
- Upgraded stores establish a new forward-only exactness epoch. Windows before
  or across it no longer inherit exactness from the older ambiguous epoch.
- Damaged trusted marker/baseline pairs fail writable startup and read-only API
  reads fail closed instead of silently recutting over ambiguous state.

## Git and scope confirmation

The failed-remediation SHA and original reviewed SHA were not amended, rebased,
or rewritten. No commit was merged into `forge/runtime` or fork `main`. No
NousResearch pull request was opened. The reference checkout and Hermes Forge
were not modified. Stage 4B and Stage 5 were not started.

No historical precision is fabricated. The remediation changes only the
boundary from which accounting can be proven exact.

## Conclusion

**READY FOR SECOND INDEPENDENT RE-REVIEW**
