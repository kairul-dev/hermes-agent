# Stage 4A Remediation Report

Date: 2026-09-04

Final implementation status: **READY FOR INDEPENDENT RE-REVIEW**

This report records implementation evidence only. It is not approval to merge.
Stage 4B and Stage 5 were not started.

## Revisions

- Reviewed Stage 4A SHA: `87efab652f8f7a3ea64a48ce86b693e9e6e259e7`
- Remediation implementation/test HEAD before this report-only commit:
  `1c107883c0352180056d75de4b229861e8045813`
- Branch: `forge/stage-4a-remediation`
- Base runtime: `4c4b37a7d792924f6e060baac2bed069fc85ddb8`
- The reviewed branch and reviewed SHA were not amended, rebased, or rewritten.

## Findings

### S4A-01 — BLOCKER — Failed queued accounting

Root cause: the writer removed a claimed batch from the deque before applying
it, while `_apply_token_batch` caught per-item exceptions and discarded the
failed item. Flush could therefore observe an empty, idle queue and report
success after data loss.

Remediation: `_apply_token_batch` now returns the ordered uncommitted suffix.
The background writer, synchronous flush, and shutdown drain put that suffix
back at the head of the queue. Successfully committed prefix items are not
retried. A writer retires after failure instead of hot-looping. Flush returns
false while work is unresolved, the usage service returns `UNAVAILABLE`, and
`close()` raises while preserving retryable work if shutdown drain fails.
Coalesced retry groups retain their original ordered detail-event list.

Files: `hermes_state.py`, `tests/state/test_stage4a_remediation.py`.

Regression evidence:

- `test_failed_queue_batch_is_preserved_and_retry_is_exact`
- `test_partial_batch_retry_does_not_duplicate_committed_prefix`
- `test_failed_flush_fails_usage_read_closed_and_close_is_observable`
- `test_concurrent_producer_stays_after_failed_claimed_batch`
- Existing four-thread queue, coalescing, busy ordering, finalization drain,
  and transaction rollback tests remain green.

The original reproduction no longer loses the nine-token delta: failed flush
returns false with the delta still queued; a later successful retry commits one
aggregate delta and one event without duplication.

Compatibility: `flush_token_counts()` can now return false for an apply error,
not only timeout. `close()` raises `RuntimeError` if queued usage still cannot
be persisted; this intentional behavior makes shutdown loss observable.

### S4A-02 — BLOCKER — Malformed/conflicting/cyclic lineage

Root cause: failure to prove an explicit fork was treated as positive proof of
a compression edge. Parse failures, non-dict metadata, conflicting markers,
cycles, and multiple eligible children therefore produced apparently exact
lineages.

Remediation: exact usage lineage has a strict graph classifier. It validates
marker shape and inheritance, predecessor identity, marker targets, successor
cardinality, chronology, dangling links, cycles, and self-links. Any ambiguous
or malformed graph returns `UNAVAILABLE` with null totals.

Files: `hermes_state.py`,
`tests/state/test_stage4a_remediation.py`,
`tests/state/test_compression_lineage_guard.py`.

Regression evidence:

- `test_malformed_or_conflicting_lineage_fails_closed`
- `test_conflicting_successors_cycle_self_cycle_and_dangling_fail_closed`
- `test_exact_compression_lineage_rejects_unverifiable_foreign_markers`

Malformed JSON, non-dict JSON, conflicting predecessor metadata, competing
successors, cycles, self-cycles, and dangling parents now fail closed instead
of returning exact totals.

Compatibility: `get_compression_lineage()` now returns an empty list when the
graph is not provably exact. Recovery/adoption helpers retain their separate,
existing operational behavior.

### S4A-03 — BLOCKER — Explicit branch-root successors omitted

Root cause: an explicit fork was treated as both a backward boundary and a
forward terminal.

Remediation: the strict resolver treats an explicit branch/delegate/tool row
as a backward boundary only, then follows its verified compression successors.
Inherited root markers must match across the branch-local chain. Ordinary
parents and sibling branches are not included.

Files: `hermes_state.py`, `tests/state/test_stage4a_remediation.py`.

Regression evidence:

- `test_branch_root_includes_only_its_compression_successors`
- Existing physical/compression scope and sibling isolation tests remain green.

Queries at the branch root, middle, and tip now return the same three-node
branch-local chain and total, excluding the ordinary parent and sibling.

Compatibility: no public request or response shape is removed.

### S4A-04 — BLOCKER — Invalid numeric accounting

Root cause: count and cost inputs were coerced at insertion time without
domain validation; SQLite accepted negative integers and non-finite reals.
Reads trusted persisted sums without integrity checks.

Remediation: all queued, direct, and auxiliary accounting validates counts as
non-negative integers, costs as finite numbers, and event timestamps as finite
non-negative values before any session or usage write. Fresh detail tables add
defensive `CHECK` constraints. Reads validate detail, aggregate, and activation
baseline rows and return `UNAVAILABLE` on malformed persisted values rather
than fabricating corrections.

Files: `hermes_state.py`, `hermes_state_common.py`,
`tests/state/test_stage4a_remediation.py`.

Regression evidence:

- `test_invalid_usage_values_are_rejected_before_any_write` covers every token
  dimension, invalid API-call counts, NaN, positive infinity, and negative
  infinity.
- `test_malformed_persisted_ledger_row_fails_closed` bypasses constraints to
  emulate legacy corruption and verifies null totals.

The original negative-token/infinite-cost reproduction now raises `ValueError`
before a session, aggregate, or detail row is created.

Compatibility: callers passing non-integral/negative counts or non-finite
costs/timestamps receive `ValueError` instead of SQLite-coerced accounting.

### S4A-05 — HIGH — Public detail-event contract

Root cause: the bounded projection grouped rows by route and never returned
event identity.

Remediation: responses now include `events`, `events_truncated`, and
`event_limit`. Events are selected with `ORDER BY recorded_at, id LIMIT 101`,
then capped at 100. Totals remain an independent unbounded SUM. Public events
contain only stable event identity, physical session, timestamp, approved
route labels, and usage metrics. Internal billing URL/provenance fields are
not selected. Existing grouped `routes` remain as an additive convenience.

Files: `hermes_state.py`, `tests/state/test_stage4a_remediation.py`,
`tests/hermes_cli/test_session_usage_api.py`.

Regression evidence:

- `test_detail_events_are_capped_stable_and_redacted`
- `test_public_api_returns_capped_detail_events`

For 150 equal-timestamp events, exactly 100 stable event rows are returned,
the truncation flag is true, repeated responses have the same order, and totals
still report all 150 events. Private billing data does not serialize.

Compatibility: response fields are additive; `routes` remains available.

### S4A-06 — HIGH — Upgrade activation reconciliation

Root cause: lifetime aggregate usage was compared with forward-only lifetime
detail, permanently classifying upgraded sessions with historical aggregates
as mismatched.

Remediation: activation now records a per-session aggregate baseline in
`session_usage_activation_baseline`, paired with a state-meta marker. Reads
compare `current aggregate - activation baseline` with detail recorded at or
after activation. Missing-marker repair resets activation at the current
aggregate without fabricating events. Upgrade from the reviewed Stage 4A
schema derives the historical baseline as aggregate minus already-recorded
post-marker detail. Marker and baseline capture use one SQLite savepoint.

Files: `hermes_state_common.py`, `hermes_state_schema.py`, `hermes_state.py`,
`tests/state/test_session_usage_detail.py`,
`tests/state/test_stage4a_remediation.py`.

Regression evidence:

- `test_upgrade_baseline_allows_exact_post_activation_window`
- Existing crossing-window historical tests remain partial and never include
  historical aggregate values in exact totals.

A 99-token historical aggregate plus one post-activation detail event now
returns `COMPLETE` total 1 for the wholly post-activation window and `PARTIAL`
total 1 for a crossing window.

Compatibility: a new additive metadata table/key is created on writable open.
No historical event backfill occurs.

### S4A-07 — HIGH — Stable SQLite snapshot

Root cause: lineage, marker, totals, routes, aggregate, and detail statements
used independent `_read_ctx` scopes, allowing separate SELECTs to observe
different WAL commits.

Remediation: after all matching live writers synchronize, one explicit read
transaction establishes a snapshot before capturing `read_at`. A dedicated
thread-local pins every nested read helper to that connection until the full
response is constructed, then rolls the read transaction back.

Files: `hermes_state.py`, `tests/state/test_stage4a_remediation.py`.

Regression evidence:

- `test_usage_read_uses_one_sqlite_snapshot`

The deterministic WAL test commits a two-token atomic write after the read
snapshot is established. The in-flight response consistently reports the old
one-event/one-token state as `COMPLETE`; the next response reports both events
and three tokens.

Compatibility: one usage read holds one read snapshot for its duration.

### S4A-08 — HIGH — Partially initialized schema

Root cause: `SCHEMA_SQL` created the usage index before additive column
reconciliation, so an existing partial table made startup fail on missing
`recorded_at`.

Remediation: the usage index is deferred until after `_reconcile_columns`.
Detail and activation-baseline tables are additively reconciled before index
and activation initialization. Missing markers are reset conservatively;
conflicting/non-finite marker pairs fail writable startup clearly. Repeated
startup after repair is stable and valid ledger history is not deleted.

Files: `hermes_state_common.py`, `hermes_state_schema.py`,
`tests/state/test_stage4a_remediation.py`.

Regression evidence:

- `test_partial_detail_table_repairs_before_index_creation`
- `test_partial_activation_metadata_fails_before_writable_use`
- `test_partial_activation_baseline_table_repairs_idempotently`
- Existing missing-table, missing-index, missing-marker, read-only incompatible
  schema, and repeated-startup tests remain green.

The original two-column detail table now opens writable, gains the required
columns and index, and remains stable on the next startup.

Compatibility: irreconcilable activation metadata raises `RuntimeError` during
writable initialization instead of allowing normal writes against an
untrustworthy capability state.

### S4A-09 — MEDIUM — Future start with omitted end

Root cause: interval validation ran before the omitted end was resolved to the
synchronized `read_at` value.

Remediation: the SQLite snapshot establishes the effective end first, then the
service rejects `start > effective_end`. Observable `start == end` remains a
valid empty half-open window.

Files: `hermes_state.py`, `tests/state/test_stage4a_remediation.py`,
`tests/hermes_cli/test_session_usage_api.py`.

Regression evidence:

- `test_future_start_with_omitted_end_is_rejected`
- `test_future_start_without_end_is_rejected`

The original `start=9e15` request now returns HTTP 400 instead of `COMPLETE`
with inverted effective bounds.

Compatibility: future-start requests with omitted end are now invalid.

## Verification results

All commands used the repository-required hermetic `scripts/run_tests.sh`
runner through Git Bash and the checkout's Windows virtualenv.

| Suite | Result |
|---|---|
| `tests/state/test_session_usage_detail.py tests/hermes_cli/test_session_usage_api.py -q` | 29 passed |
| Accounting/persistence six-file suite from the remediation brief | 59 passed |
| `tests/test_hermes_state.py -q` | 259 passed, 2 skipped |
| Schema/repair four-file suite | 36 passed, 1 known baseline failure, 4 skipped |
| Dashboard/auth/profile six-file suite | 284 passed, 1 known baseline failure, 5 skipped |
| New remediation plus compression-lineage guard suites | 43 passed |

Static and structural checks:

- Ruff on changed Python files: passed.
- Repository-wide Ruff: passed.
- Python bytecode compilation on all changed Python files: passed.
- `git diff --check`: passed.
- SQLite query plan: `SEARCH session_usage_events USING INDEX
  idx_session_usage_events_session_time (session_id=? AND recorded_at>?
  AND recorded_at<?)`.

## Baseline failure status

The two failures independently classified against the untouched base remain
identical and are not Stage 4A regressions:

1. `test_repair_rebuilds_stale_btree_indexes`: SQLite 3.45.1 reports
   `row N missing from index` instead of the test's expected
   `wrong # of entries in index` wording.
2. `TestSystemStatsEndpoint.test_stats_shape`: the hermetic Windows runner
   returns an empty `arch` because `PROCESSOR_ARCHITECTURE` is absent.

## Security, isolation, and atomicity status

- Dashboard authentication regressions pass; unauthenticated access remains
  401.
- Named-profile duplicate session IDs remain isolated and path-like profile
  values remain rejected.
- Public payload regressions verify that billing base URLs, cost provenance,
  credentials, paths, SQL, and transcript content do not leak.
- Aggregate/detail transaction rollback, no-bypass accounting-path tests,
  healthy four-thread queueing, and `[start,end)` boundaries remain green.
- The usage window query continues to use the intended composite index.

## Repository state

At report preparation, all implementation and regression changes were
committed in separate commits on `forge/stage-4a-remediation`. This report is
the only pending file and will be committed separately. After that commit,
the working tree is expected to be clean. The reviewed branch, `forge/runtime`,
fork `main`, the reference checkout, and Hermes Forge were not modified.

## Conclusion

**READY FOR INDEPENDENT RE-REVIEW**
