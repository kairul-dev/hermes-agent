# Stage 4A Independent Review — Session Usage Read Surface

Review date: 2026-09-04

Capability: session.usage.detail.v1

Public endpoint: GET /api/sessions/{session_id}/usage

Final verdict: **CHANGES REQUIRED**

## 1. Reviewed revisions and repository state

| Checkout | Branch | Reviewed HEAD/base | Status before review |
|---|---|---|---|
| C:\Users\khairul\Projects\hermes-agent-forge-runtime | forge/stage-4-session-usage-read-surface | HEAD 87efab652f8f7a3ea64a48ce86b693e9e6e259e7; forge/runtime 4c4b37a7d792924f6e060baac2bed069fc85ddb8 | Clean |
| C:\Users\khairul\Projects\hermes-agent | forge/run-session-binding | 4c4b37a7d792924f6e060baac2bed069fc85ddb8 | Clean; treated as read-only |
| C:\Users\khairul\Projects\hermes-forge | feat/stage-4-session-usage-contract | 1c4039e85d9cd81d5401ec6fe8c2ae2cf4cb5ca8 | Clean; untouched |

The implementation is exactly two commits ahead of forge/runtime:

1. 47d03dad78 feat(usage): persist exact session usage detail
2. 87efab652f feat(dashboard): expose session usage read API

The reviewed delta is eight files, 1,179 insertions and 5 deletions. No implementation source was changed during this review. This report is the only permanent review artifact added to the implementation checkout, so the expected final implementation status is one untracked report file. The reference checkout and Hermes Forge remain clean.

## 2. Executive assessment

The high-level architecture is sound:

- The HTTP route is behind the existing non-public /api authentication middleware.
- The route accepts a profile name rather than a path, resolves it through Hermes' profile router, opens the profile store read-only, and delegates all SQL work to a public SessionDB method.
- Incremental main-loop and auxiliary accounting converge on SessionDB._record_model_usage.
- Aggregate session_model_usage and detailed session_usage_events writes occur in the same SQLite write transaction.
- Exact window totals are calculated independently of the bounded public projection.
- Historical aggregate rows are not copied into fabricated detail events.
- Successful profile-isolation, auth, response-redaction, transaction-rollback, queue-stress, boundary, and query-plan probes provide positive evidence for these controls.

The implementation is not ready to merge. Four BLOCKER findings can produce silent accounting loss, incorrect exact values, or unsafe lineage attribution. Four HIGH findings break required detail, coverage, snapshot, or schema-lifecycle contracts. One MEDIUM finding returns an internally inverted future window as COMPLETE.

## 3. Findings summary

| ID | Severity | Finding |
|---|---|---|
| S4A-01 | BLOCKER | Failed queued accounting is dropped, flush reports success, and the API returns COMPLETE zero |
| S4A-02 | BLOCKER | Malformed, conflicting, and cyclic lineage state is accepted as an exact compression lineage |
| S4A-03 | BLOCKER | Querying an explicit branch root omits its legitimate compression successors |
| S4A-04 | BLOCKER | Negative token counts and non-finite costs are persisted and reported as exact |
| S4A-05 | HIGH | The public detail response returns grouped routes, not capped event rows |
| S4A-06 | HIGH | Upgraded stores report a fully post-activation exact window as PARTIAL |
| S4A-07 | HIGH | Usage reads do not use one SQLite snapshot and can observe one atomic write inconsistently |
| S4A-08 | HIGH | A partially initialized detail table can prevent writable database startup |
| S4A-09 | MEDIUM | A future start with omitted end returns an inverted effective interval as COMPLETE zero |

## 4. Detailed findings

### S4A-01 — Failed queued accounting is silently lost

- Severity: BLOCKER
- Affected code: hermes_state.py:10147-10169 and hermes_state.py:10762-10790
- Reproduction: Stop the background writer, append a valid 9-token queued delta, replace update_token_counts with a function that raises a simulated transient error, and call flush_token_counts. Restore the writer method and flush again, then read usage.
- Expected: The failed delta remains retryable, the first or second flush reports failure until it commits, or coverage becomes UNAVAILABLE because exactness is no longer provable.
- Actual: The first flush returned true, the failed item was removed from the queue, the retry flush returned true, zero aggregate/detail rows existed, and get_session_usage_detail returned COMPLETE with input_tokens=0.
- Root cause: _apply_token_batch catches each apply exception after the batch has already been removed from the queue. It logs the failure but neither requeues the delta nor records a sticky accounting-loss state. flush_token_counts only observes queue/busy state, so it cannot distinguish a successful drain from a dropped batch.
- Existing tests: tests/agent/test_async_token_accounting.py:447-511 tests coalescing failure and busy ordering, but not an update failure followed by retry. tests/state/test_session_usage_detail.py:310-338 tests only successful synchronization.
- Minimal remediation: Retain failed items in ordered retry state and make flush return false until every claimed delta commits. Persist or retain a sticky loss/unavailable marker if recovery is impossible. Add a forced apply-failure/retry test that verifies no duplication and no COMPLETE result while a delta is unresolved.

The same detectability problem applies to best-effort auxiliary accounting: producer-level exceptions may be swallowed, leaving aggregate and detail equally absent, which lifetime reconciliation cannot detect.

### S4A-02 — Malformed or cyclic lineage fails open

- Severity: BLOCKER
- Affected code: hermes_state.py:10812-10836, hermes_state.py:15573-15587, and hermes_state.py:15679-15729
- Reproduction:
  1. End parent p with reason compression.
  2. Create child c with parent_session_id=p and model_config equal to malformed JSON, a non-dict JSON value, or {_branched_from: someone-else}.
  3. Record 1 token on p and 10 on c; read p with scope=compression_lineage.
  4. Separately create a parent cycle a -> b -> a with both rows compression-ended and read a.
- Expected: Malformed/conflicting metadata and cycles make lineage ambiguous, so coverage must be UNAVAILABLE with totals=null.
- Actual: Every malformed/conflicting case returned COMPLETE, session_ids [p,c], and total 11. The cycle returned COMPLETE, session_ids [b,a], and total 8. A valid {_branched_from:p} control was correctly excluded.
- Root cause: _is_explicit_fork_child_row maps parse failure, non-dict content, and a marker naming another parent to false. _is_compression_child_row then interprets “not an explicit fork” as positive proof of compression. Traversal breaks cycles as if they were ordinary termination instead of reporting invalid lineage. The later ambiguity check counts the same fail-open classifier.
- Existing tests: tests/state/test_session_usage_detail.py:365-379 covers two eligible children only. It should also cover malformed JSON, non-dict JSON, conflicting parent markers, and cycles.
- Minimal remediation: Make lineage classification tri-state: verified fork, verified compression continuation, or invalid/ambiguous. Propagate invalid state to an UNAVAILABLE response. Detect cycles explicitly. Do not use failure to prove a fork as proof of compression.

### S4A-03 — Branch-root compression descendants are omitted

- Severity: BLOCKER
- Affected code: hermes_state.py:15686-15690
- Reproduction: Create p, create explicit branch b with {_branched_from:p}, record 8 tokens, compression-end b, create its valid compression successor b2 carrying the inherited marker, and record 16 tokens. Read scope=compression_lineage first for b and then for b2.
- Expected: Both requests identify the same branch-local compression lineage [b,b2] with total 24, without crossing into p.
- Actual: Reading b returned COMPLETE with [b] and total 8. Reading b2 returned COMPLETE with [b,b2] and total 24.
- Root cause: get_compression_lineage immediately returns [session_id] whenever the requested row is itself an explicit fork. That correctly prevents traversal into the fork's parent, but it also prevents forward traversal through the fork's own legitimate compression successors.
- Existing tests: tests/state/test_session_usage_detail.py:343-360 covers an ordinary compression pair, but not the required “child branch that later compresses” case.
- Minimal remediation: Treat an explicit fork as a backward boundary, not a forward terminal. Start the lineage root at that fork and continue through verified compression descendants. Add equivalent tests for branch, delegate, and tool-rooted compression chains as applicable.

### S4A-04 — Invalid numeric accounting is accepted

- Severity: BLOCKER
- Affected code: hermes_state.py:10266-10385 and hermes_state.py:10574-10587; schema lacks validating constraints at hermes_state_common.py:554-561
- Reproduction: Call update_token_counts with input_tokens=-5 and estimated_cost_usd=float("inf"), then read physical usage. Also exercise the HTTP route over the resulting store.
- Expected: Negative token/API-call counts and non-finite numeric values are rejected before any aggregate or detail write, and invalid stored data cannot be labeled exact.
- Actual: The detail row stored -5 and infinity. The state response returned COMPLETE with input_tokens=-5 and an infinite estimated cost. JSON serialization at the HTTP boundary failed and produced a generic 500.
- Root cause: The public state methods coerce with int/float but never enforce non-negative count fields or finite numeric fields; the table has NOT NULL defaults but no CHECK constraints.
- Existing tests: tests/state/test_session_usage_detail.py:84-137 validates ordinary field separation but has no adversarial numeric cases.
- Minimal remediation: Validate every count as a non-negative integer and every cost/timestamp as finite before opening the write transaction; add defensive schema constraints where compatible. Reads encountering legacy invalid rows should return UNAVAILABLE or a clearly non-exact error, never COMPLETE.

### S4A-05 — Detail rows are absent from the public contract

- Severity: HIGH
- Affected code: hermes_state.py:10922-10952 and hermes_state.py:10991-11008
- Reproduction: Record 150 distinct accounting events for one session at the same timestamp and route, then read usage.
- Expected: Totals cover all 150 eligible events, while an events collection returns at most 100 rows ordered by (recorded_at,id), with truncation metadata.
- Actual: Totals correctly returned 150, but the response had no events key. It returned one grouped routes row and routes_truncated=false. Therefore the required event-row cap, timestamp-collision ordering, and repeated event-order stability are not exposed by the API.
- Root cause: The bounded query groups by task/model/provider/mode before applying LIMIT. It is a route summary query, not a detail-event query, and id is never projected.
- Existing tests: tests/state/test_session_usage_detail.py:166-184 creates 101 distinct routes and proves a route cap, not a greater-than-100 event cap. That test encodes the wrong contract and should have caught the omission.
- Minimal remediation: Return a separate events projection selected with ORDER BY recorded_at,id LIMIT 101; expose the first 100 plus events_truncated. Keep totals as the unbounded SUM query. A grouped routes summary may remain as an additional field.

### S4A-06 — Upgraded-store reconciliation makes post-activation windows falsely partial

- Severity: HIGH
- Affected code: hermes_state.py:10939-10985
- Reproduction: Create a legacy aggregate row of 99 tokens before a marker at t=100, record one exact event at t=150, and request [100,200).
- Expected: The requested window is wholly after activation and its one-token detail is exact, so coverage is COMPLETE with total 1.
- Actual: Total was correctly 1, but coverage was PARTIAL with reason aggregate_detail_mismatch.
- Root cause: Coverage compares lifetime session_model_usage, including legitimate pre-activation aggregate history, against lifetime detail, which is intentionally forward-only. No activation-time aggregate baseline is retained, so every upgraded session with historical usage remains mismatched forever, even for later exact windows.
- Existing tests: tests/state/test_session_usage_detail.py:239-262 covers historical aggregate with no detail in a crossing window, not a wholly post-activation window after new detail appears.
- Minimal remediation: Persist an activation-time aggregate baseline (per session/route or an equivalent monotonic baseline) and reconcile only post-activation aggregate deltas against detail. Do not fabricate historical events.

### S4A-07 — Reconciliation is not read from one synchronized snapshot

- Severity: HIGH
- Affected code: hermes_state.py:10798-10948
- Reproduction: Use WAL for a disposable store and inject an atomic writer commit after the aggregate SELECT returns but before the lifetime-detail SELECT. Read again after the writer settles.
- Expected: After queue synchronization and read_at capture, every SQL statement contributing to one response observes one SQLite snapshot.
- Actual: The interleaved response returned total 1 and PARTIAL aggregate_detail_mismatch; the immediately settled response returned total 2 and COMPLETE. The write itself remained atomic, but separate read statements observed different commits.
- Root cause: Session/lineage/schema/total/route/aggregate/detail reads span multiple _read_ctx uses and no explicit read transaction. Each SELECT may establish a new SQLite snapshot.
- Existing tests: tests/state/test_session_usage_detail.py:310-338 flushes before reading but does not interleave an atomic commit between reconciliation SELECTs.
- Minimal remediation: After synchronization and read_at capture, run all response-building reads on one connection inside one explicit read transaction (or one statement/CTE snapshot). Add a deterministic interleaving test.

### S4A-08 — Partial detail schema blocks writable startup

- Severity: HIGH
- Affected code: hermes_state_common.py:545-564 and hermes_state_schema.py:1222-1238
- Reproduction: Create a valid store, replace session_usage_events with a partial table containing only id and session_id, then reopen SessionDB in normal writable mode.
- Expected: Additive reconciliation repairs missing columns/indexes non-destructively, or startup succeeds with the capability marked unavailable.
- Actual: Writable SessionDB construction raised sqlite3.OperationalError: no such column: recorded_at before reconciliation ran.
- Root cause: SCHEMA_SQL executes CREATE INDEX idx_session_usage_events_session_time before _reconcile_columns. CREATE TABLE IF NOT EXISTS leaves the partial table unchanged, and the index statement references missing columns. This is the same ordering hazard the schema initializer already documents for other deferred indexes at hermes_state_schema.py:1251-1254.
- Existing tests: tests/state/test_session_usage_detail.py:287-307 checks only a read-only open and fails closed at read time. It does not test normal writable startup or self-healing.
- Minimal remediation: Move this index to the post-reconciliation deferred-index section. Validate/heal the table shape before index creation and preserve existing rows whenever compatible.

### S4A-09 — Future start plus omitted end yields an inverted COMPLETE window

- Severity: MEDIUM
- Affected code: hermes_state.py:10890-10902 and hermes_state.py:10984-10989
- Reproduction: On a valid store call get_session_usage_detail with start=9e15 and omit end.
- Expected: The response rejects the not-yet-observable window or returns a clearly partial/not-started empty intersection with coherent effective bounds.
- Actual: Coverage was COMPLETE/exact_detail_available_no_usage while exact_start and effective_start were 9e15 and exact_end/effective_end were the current time, so start was greater than end.
- Root cause: Validation compares start and end only when end is explicitly supplied. The synchronized effective_end is computed later, but start is never compared with it; the future-window partial rule only checks an explicit end.
- Existing tests: tests/state/test_session_usage_detail.py:384-397 covers NaN and explicit start>end, but not the required extremely large timestamp with omitted end.
- Minimal remediation: Define an empty future-window contract and enforce it after read_at is captured. Either reject start>read_at for omitted end or return PARTIAL/not-yet-started with a non-inverted exact intersection.

## 5. Accounting-path inventory

| Producer/path | Production path to persistence | Detail result |
|---|---|---|
| Normal CLI/model call | agent/conversation_loop.py queue_token_counts -> update_token_counts -> _record_model_usage | One detail event per queued producer event, even when aggregate deltas coalesce |
| Codex/non-CLI runtime | agent/codex_runtime.py queue_token_counts -> update_token_counts -> _record_model_usage | Same central path; no direct aggregate bypass found |
| Auxiliary/internal calls, including compression/title/vision/search | agent/aux_accounting.py normalize_usage -> record_auxiliary_usage -> _record_model_usage | One auxiliary event with task/category |
| Background review | agent/background_review.py record_auxiliary_usage -> _record_model_usage | One authoritative batch event; api_call_count may be greater than one |
| MoA reference/aggregator | Excluded from auxiliary recording and folded into the main-loop delta | Avoids documented double-accounting |
| Queue flush, shutdown, reader synchronization | SessionDB queue/writer/flush methods | Healthy paths preserve events; failed apply is silently dropped (S4A-01) |
| Session continuation/compression/children | Accounting context carries the active physical session id | Physical writes stay physical; lineage aggregation has S4A-02/S4A-03 |
| Schema migration, PK heal, recovery/lost-and-found | Direct SQL lifecycle/history operations | Non-incremental maintenance, not a live accounting bypass |
| Absolute cumulative update | update_token_counts(absolute=True) updates session summary only by design | No production absolute=True caller was found in the reviewed tree; not an incremental writer |

Repository-wide searches for update/insert/replace statements on session_model_usage and for calls to update_token_counts, queue_token_counts, and record_auxiliary_usage found no live incremental writer that bypasses SessionDB._record_model_usage. The direct SQL occurrences are schema migration/repair or test fixtures. Aggregate/detail rollback was verified with a trigger that aborted detail insertion: sessions, aggregate, and detail all remained zero, and a later clean retry committed exactly once.

## 6. Event and coverage assessment

Positive results:

- Fresh zero usage returned COMPLETE with non-null all-zero totals.
- Fresh post-activation usage returned COMPLETE.
- Windows before activation returned UNAVAILABLE with totals=null.
- Windows crossing activation returned PARTIAL and included only post-activation detail.
- An event exactly at activation/start was included.
- Missing/invalid activation markers returned UNAVAILABLE.
- Missing detail table or incompatible read-only schema returned UNAVAILABLE with totals=null.
- Detail without aggregate returned PARTIAL aggregate_detail_mismatch.
- Aggregate without detail returned PARTIAL historical_aggregate_only.
- An explicit flush timeout returned UNAVAILABLE.
- Estimated and actual cost, cache read/write, reasoning tokens, task, model, provider, mode, and batched API-call count remained separate in valid data.
- Producer timestamps survived queue coalescing in the required suite.

Negative results are S4A-01, S4A-04, S4A-05, S4A-06, S4A-07, and S4A-09.

## 7. Lineage assessment

Healthy chains behaved as intended: physical scope stayed on one id; a 40-session compression chain returned all 40 physical sessions and exact total 40; parent, middle, and tip requests on a three-node chain returned the same chain; valid explicit side branches were excluded.

Malformed/conflicting metadata and cycles failed open (S4A-02), while requesting the explicit root of a branch-local compression chain omitted its successor (S4A-03). These violate the critical fail-closed lineage requirement.

## 8. Profile isolation and security assessment

- Two independent profile databases with the same session id returned independent totals 7 and 70.
- The required API tests verified named-profile duplicate-id isolation, no fallback from a named profile to default, and rejection of path-like profile values.
- Unauthenticated access returned 401 {"detail":"Unauthorized"}.
- Successful serialization omitted billing_base_url, cost_source, transcript content, and private accounting URLs.
- Forced unexpected exceptions returned a generic 500 response without the injected private sentinel. A non-finite stored cost also returned a generic 500 without serializing Infinity.
- The route contains no SQL and accepts no filesystem/database path.

A completed Codex Security diff scan (scan c5d0febc-f8cf-451b-a5ab-f820c954b344) found no reportable security-boundary vulnerability under the repository SECURITY.md policy. The merge-blocking findings in this report are correctness, exactness, durability, and operability defects; no authentication bypass, cross-profile disclosure, credential leak, or response stack-trace leak was reproduced.

## 9. Schema lifecycle assessment

Fresh creation, repeated startup, missing-index recreation, and legacy table/marker creation were non-destructive. Repeated startup kept the activation marker unchanged and preserved a seven-token history. Dropping the usage index and reopening recreated it without changing history.

No aggregate backfill was fabricated into event rows. Removing an activation marker and reopening conservatively reset the marker and did not treat older detail as post-activation exact. The partial-table writable startup failure is S4A-08.

## 10. Queue, concurrency, and durability assessment

- Four producer threads queued 400 one-token events. Both flush calls returned true; event count and sum stayed 400; the service returned COMPLETE total 400.
- Multiple events with identical timestamps retained strictly increasing SQLite ids.
- A forced detail-insert failure rolled back the session summary, aggregate row, and detail row together. A clean retry committed exactly once.
- A background-review-style authoritative event preserved api_call_count=3 in one detail row.
- The required async tests covered enqueue order, coalescing equivalence, route-switch barriers, reader flush, concurrent flush waiting, close/reopen, shutdown drain, and finalization drain.

The failed-apply/lost-retry defect is S4A-01. The multi-SELECT snapshot race is S4A-07.

## 11. Performance assessment

The 20,000-row single-session query plan used idx_session_usage_events_session_time for totals, limited routes, and multi-id lineage totals:

    SEARCH session_usage_events USING INDEX idx_session_usage_events_session_time
      (session_id=? AND recorded_at>? AND recorded_at<?)

The grouped route query additionally used a temporary B-tree for GROUP BY, as expected. A 250-session/5,000-event fixture returned one 20-event session in approximately 1.0 ms. A 40-session lineage returned in approximately 12.6 ms. Both plans used the index. No full-table scan or unbounded Python loading was observed. The public projection problem is semantic (S4A-05), not a demonstrated index regression.

## 12. Required tests and static checks

All commands below were run on implementation HEAD 87efab652f8f7a3ea64a48ce86b693e9e6e259e7 through scripts/run_tests.sh unless noted.

| Command/group | Result |
|---|---|
| tests/state/test_session_usage_detail.py tests/hermes_cli/test_session_usage_api.py -q | 27 passed |
| tests/agent/test_async_token_accounting.py tests/agent/test_background_review_usage.py tests/hermes_state/test_aux_usage_accounting.py tests/run_agent/test_token_persistence_non_cli.py tests/state/test_session_model_usage_pk_heal.py tests/state/test_session_usage_detail.py -q | 59 passed |
| tests/test_hermes_state.py -q | 259 passed, 2 skipped |
| tests/test_schema_read_probe.py tests/test_zeroed_state_db.py tests/test_state_db_malformed_repair.py tests/state/test_session_model_usage_pk_heal.py -q | 36 passed, 1 failed, 4 skipped |
| tests/hermes_cli/test_session_usage_api.py tests/hermes_cli/test_web_server.py tests/hermes_cli/test_dashboard_admin_endpoints.py tests/hermes_cli/test_dashboard_param_clamps.py tests/hermes_cli/test_profiles_sidebar_scope.py tests/hermes_cli/test_dashboard_auth_gate.py -q | 282 passed, 1 failed, 5 skipped |
| Ruff 0.16.6 on the eight changed Python files | Passed |
| Ruff 0.16.6 repository-wide | Passed; two pre-existing invalid-noqa warnings |
| In-memory compile of all eight changed Python files | Passed |
| git diff --check forge/runtime..HEAD | Passed |
| git diff --check for the working tree before this report | Passed |

The two suite failures are independently confirmed baseline/environment failures, not Stage 4A regressions; see the next section.

## 13. Adversarial probes executed

The preserved scan-owned harness at:

    C:\Users\khairul\AppData\Local\Temp\codex-security-scans-A98Abw\hermes-agent-forge-runtime\87efab652f8f7a3ea64a48ce86b693e9e6e259e7_20260904T081622Z_1s_zyzpd

was continued rather than recreated. Successful probes included:

- upgraded post-activation window reconciliation;
- malformed, non-dict, conflicting, multiple-child, valid-branch, branch-compress, and cyclic lineage;
- fresh/upgraded/before/crossing/after activation coverage matrix;
- missing/invalid marker, missing table, malformed table, detail-only, and aggregate-only states;
- negative and infinite accounting values plus HTTP serialization;
- exact start/end/adjacent/equal/invalid/future window boundaries;
- 150 identical-timestamp events versus totals and public projection;
- forced aggregate/detail read interleaving;
- forced detail-insert transaction abort and clean retry;
- failed queued apply followed by retry;
- four-thread/400-event queue stress and repeated flush;
- batched api_call_count=3 event;
- two-profile colliding-id isolation;
- unauthenticated, successful-redaction, and unexpected-error leakage checks;
- 20,000-event, 250-session, and 40-session-lineage query plans.

## 14. Independent baseline-failure verification

The untouched reference checkout remained at 4c4b37a7d792924f6e060baac2bed069fc85ddb8. Both failures reproduced there under the same canonical hermetic runner:

1. tests/test_state_db_malformed_repair.py::test_repair_rebuilds_stale_btree_indexes
   - Expected test substring: wrong # of entries in index idx_messages_session
   - Actual SQLite 3.45.1 diagnostic: row 1 missing from index ...; row 2 missing ...; row 3 missing ...
   - Classification: baseline/environment-sensitive assertion wording.
2. tests/hermes_cli/test_dashboard_admin_endpoints.py::TestSystemStatsEndpoint::test_stats_shape
   - Actual: /api/system/stats returned arch as an empty string in the clean-env Windows runner.
   - Classification: baseline/environment failure. A non-hermetic direct pytest invocation retained PROCESSOR_ARCHITECTURE and passed, confirming the environment sensitivity; the canonical clean runner reproduced the reported failure.

Neither failure is classified as a Stage 4A regression.

## 15. Final repository state and prohibited actions

- Implementation checkout: branch forge/stage-4-session-usage-read-surface at 87efab652f8f7a3ea64a48ce86b693e9e6e259e7; production implementation unchanged; only docs/STAGE_4A_INDEPENDENT_REVIEW.md is added as a review artifact.
- Reference checkout: branch forge/run-session-binding at 4c4b37a7d792924f6e060baac2bed069fc85ddb8; no tracked modifications.
- Hermes Forge: branch feat/stage-4-session-usage-contract at 1c4039e85d9cd81d5401ec6fe8c2ae2cf4cb5ca8; clean and not modified.
- Nothing was merged, rebased, committed, or pushed.
- Fork main was not modified.
- No NousResearch pull request was opened.
- Stage 4B was not started.
- Stage 5 was not started.

## 16. Final verdict

**CHANGES REQUIRED**

Stop here. Resolve and independently revalidate the BLOCKER and HIGH findings before considering a merge into forge/runtime.
