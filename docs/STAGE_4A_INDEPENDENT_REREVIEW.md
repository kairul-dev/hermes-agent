# Stage 4A Independent Re-Review — Remediation Verification

Review date: 2026-09-04

Capability: `session.usage.detail.v1`

Public endpoint: `GET /api/sessions/{session_id}/usage`

Final verdict: **CHANGES REQUIRED**

## 1. Reviewed revisions and provenance

| Item | Verified value |
|---|---|
| Original independently reviewed Stage 4A SHA | `87efab652f8f7a3ea64a48ce86b693e9e6e259e7` |
| Remediation SHA | `50706d1e448fe5d1ac404d55596369dfa4dfeca3` |
| Branch | `forge/stage-4a-remediation` |
| Tracking branch | `origin/forge/stage-4a-remediation` at the same SHA |
| Runtime base | `forge/runtime` at `4c4b37a7d792924f6e060baac2bed069fc85ddb8` |
| Reference checkout | `forge/run-session-binding` at `4c4b37a7d792924f6e060baac2bed069fc85ddb8` |
| Hermes Forge | `feat/stage-4-session-usage-contract` at `1c4039e85d9cd81d5401ec6fe8c2ae2cf4cb5ca8` |

The implementation, reference, and Hermes Forge checkouts were clean before
the review. The original reviewed SHA resolves unchanged and is an ancestor of
the remediation SHA. The runtime base is an ancestor of the original reviewed
SHA. No unexpected commits were present.

Complete remediation commit list, oldest first:

1. `e84a6181c7` — `docs(stage-4a): record independent review findings`
2. `b5d23bd4fa` — `fix(usage): harden exact accounting and detail reads`
3. `1c107883c0` — `test(usage): cover stage 4a adversarial regressions`
4. `50706d1e44` — `docs(stage-4a): record remediation evidence`

The remediation delta from the original reviewed SHA changes nine files with
1,832 insertions and 186 deletions. Both required reports were read completely,
and their claims were treated as hypotheses rather than evidence.

## 2. Executive assessment

The remediation materially fixes the originally reported queue retry, strict
lineage, branch-root successor, numeric validation, event projection, stable
snapshot, partial-schema, and future-window defects. The required test groups
match the remediation report's results, including the claimed 43-test new
adversarial/lineage suite.

It is not safe to merge. A new independent upgrade reproduction demonstrates
that activation-baseline migration can absorb a genuine post-activation
aggregate/detail gap into the historical baseline. The resulting API response
reports `COMPLETE` exact coverage while omitting accounted usage. This violates
the explicit requirement that activation remediation must not hide missing
post-activation detail and meets the brief's BLOCKER definition for incorrect
exact usage and fabricated precision.

Per the review brief, discovery of this material defect ended further
verification after reproduction and documentation. Static-check and standalone
query-plan commands not already reached are therefore recorded as not run,
rather than inferred from the remediation report.

## 3. Findings

### S4A-RR-01 — BLOCKER — Upgrade migration launders post-activation ledger gaps into the historical baseline

Affected code:

- `hermes_state_schema.py:1251-1258` sums all detail at or after the existing
  Stage 4A activation marker.
- `hermes_state_schema.py:1265-1301` derives the new baseline as current
  aggregate minus that detail.
- `hermes_state.py:11162-11185` later compares current aggregate minus the
  derived baseline with the same post-activation detail and labels equality
  `COMPLETE`.

Independent reproduction:

1. Create a store with activation marker `T` and 99 historical aggregate
   tokens/one API call.
2. Record one valid detailed post-activation token/call.
3. Emulate a genuine post-activation ledger gap by advancing aggregate usage
   another five tokens/one call without a corresponding detail row.
4. Emulate the reviewed Stage 4A schema by retaining the coverage marker while
   removing the remediation-only baseline marker/table.
5. Open the store at remediation HEAD and query `[T, now)`.

Observed evidence:

| Value | API calls | Input tokens |
|---|---:|---:|
| Aggregate before migration | 3 | 105 |
| Recorded post-activation detail | 1 | 1 |
| Baseline derived by remediation | 2 | 104 |
| Publicly reported post-activation total | 1 | 1 |

The response returned:

```text
coverage.status = COMPLETE
coverage.reason = exact_detail_available
totals.api_call_count = 1
totals.input_tokens = 1
```

The missing five tokens/one call were reclassified as historical merely because
the baseline metadata was being introduced. The migration has no evidence that
the aggregate/detail difference predates activation, so subtracting all
post-marker detail from the current aggregate cannot distinguish legitimate
historical aggregate from a post-activation ledger gap.

The reproduction is preserved outside all three repositories at:

`C:\Users\khairul\.codex\visualizations\2026\09\04\01a06be0-39d8-78b2-9cdc-37eefb37f747\stage4a_activation_gap_probe.py`

Required behavior: when upgrading an already-active Stage 4A store, an
unprovable aggregate/detail difference must fail closed. It must not be minted
as a trustworthy historical baseline. A safe remediation needs provenance for
the old baseline or must conservatively mark affected sessions/windows
`PARTIAL`/`UNAVAILABLE` until exactness can be established.

## 4. Verification status for S4A-01 through S4A-09

| ID | Status at remediation HEAD | Verification |
|---|---|---|
| S4A-01 | Resolved in exercised paths | Queue failures remain retryable; flush/read/close expose failure; committed-prefix retry avoids duplication; concurrent producer ordering tests passed. |
| S4A-02 | Resolved in exercised paths | Malformed JSON/non-dict markers, conflicting predecessors/successors, cycles, self-cycles, dangling state, and foreign markers fail closed in the 43-test suite and strict graph inspection. |
| S4A-03 | Resolved in exercised paths | Branch root, middle, and tip resolve only `A + A1 + A2`; root and siblings are excluded by the branch-local regression. |
| S4A-04 | Resolved in exercised paths | Negative count dimensions, invalid API-call count, NaN, positive/negative infinity, and malformed persisted rows are rejected or reported unavailable by the adversarial suite and schema/read guards. |
| S4A-05 | Resolved in exercised paths | 150 equal-timestamp events return 100 stable `(recorded_at,id)`-ordered public event rows, accurate truncation, unbounded totals, and no private accounting fields. |
| S4A-06 | **Not resolved — BLOCKER** | Ordinary historical baseline/post-activation coverage case passes, but S4A-RR-01 proves the migration hides a genuine post-activation detail gap and fabricates `COMPLETE` precision. |
| S4A-07 | Resolved in exercised paths | The concurrency regression establishes one pinned SQLite snapshot; the in-flight response sees the old atomic state and the next response sees the committed state. |
| S4A-08 | Resolved in exercised paths | Missing/incomplete detail and baseline tables are reconciled before deferred index/activation setup; irreconcilable paired metadata fails startup; repair is idempotent in tests. |
| S4A-09 | Resolved in exercised paths | `start > synchronized effective_end` with omitted end is rejected; ordinary equal/zero windows remain valid in the existing boundary suite. |

These statuses are evidence summaries, not an approval: the stop-on-material-
defect rule prevented continuing to add separate ad hoc reproductions for every
already-green case after S4A-RR-01 was confirmed.

## 5. Accounting assessment

The queue remediation preserves an ordered uncommitted suffix, requeues it at
the head, retires the failed writer to avoid hot looping, and makes flush and
shutdown failures observable. Healthy four-thread queueing, aggregate/detail
transactional atomicity, producer timestamp preservation, and central
`SessionDB._record_model_usage` routing remain covered by the executed suites.

The accounting contract is nevertheless not trustworthy across upgrade because
S4A-RR-01 can transform an aggregate/detail mismatch into `COMPLETE` coverage.
This is an exactness failure, not a bounded presentation defect.

## 6. Compression-lineage assessment

The strict resolver classifies links as compression, ordinary, or invalid;
checks marker shape/inheritance, predecessor identity, chronology, dangling
targets, successor cardinality, cycles, and self-links; and treats an explicit
branch as a backward boundary rather than a forward terminal. The required
lineage/adversarial suite passed 43 tests in total with the remediation suite.
No new lineage defect was found before the stop condition.

## 7. Coverage and activation assessment

Fresh activation, wholly pre-activation, crossing, and wholly post-activation
behavior is covered by the executed tests. Historical aggregate is not copied
into fabricated event rows, and a normal post-activation window can become
`COMPLETE`.

The upgrade derivation is unsound when the existing Stage 4A store already has
an aggregate/detail gap. It assumes every aggregate value not represented by
post-marker detail is pre-activation history. S4A-RR-01 demonstrates that this
assumption hides genuinely missing post-activation detail and is the decisive
failure of the re-review.

## 8. Snapshot and concurrency assessment

The read path synchronizes matching live writers, begins one explicit read
transaction, establishes its SQLite snapshot, captures `read_at`, and pins all
nested read helpers to the same connection through thread-local state. The
deterministic writer-interleaving regression passed. No mixed-snapshot response
was observed in the executed suite.

## 9. Schema-repair assessment

The detail index is now created after declarative column reconciliation.
Executed tests verify repair of a partial detail table, repair of a partial
activation-baseline table, idempotent reopen, and clear failure for conflicting
paired activation metadata. Existing valid history survives the covered repair
paths.

Schema activation itself still has the exactness blocker described in
S4A-RR-01. Safe physical schema repair does not make the inferred historical
baseline semantically trustworthy.

## 10. API contract assessment

The response now includes capped detail events plus independent totals, stable
ordering, accurate event truncation, and the existing grouped routes as an
additive view. `[start,end)` and future-start validation passed the required
groups. The public contract still fails its central promise in S4A-RR-01:
`COMPLETE` can be returned for incomplete detailed usage after upgrade.

## 11. Security and profile assessment

The required dashboard/auth/profile group exercised unauthenticated access,
successful responses, malformed parameters, named-profile routing, and
duplicate session IDs. The Stage 4A API tests passed, including 401 enforcement,
profile isolation, path-like profile rejection, and redaction of billing URLs,
`cost_source`, and transcript content. No credentials, authorization headers,
filesystem/database paths, SQL, or sensitive stack trace were observed in the
executed Stage 4A checks. No new security-boundary or cross-profile finding was
identified before the stop condition.

## 12. Performance and query-plan assessment

Code inspection confirms the event query remains bounded to 101 rows and
ordered by `(recorded_at,id)`, and the composite
`idx_session_usage_events_session_time` index remains part of schema setup.
The prior remediation report's standalone query-plan claim was not accepted as
independent evidence. A fresh standalone query-plan command was not reached
because the review stopped on S4A-RR-01. No performance severity is assigned.

## 13. Tests executed

All pytest commands used `scripts/run_tests.sh` through Git Bash and the
checkout's Windows virtual environment.

| Command/group | Result |
|---|---|
| `tests/state/test_session_usage_detail.py tests/hermes_cli/test_session_usage_api.py -q` | **29 passed** |
| Accounting/persistence six-file group from the brief | **59 passed** |
| `tests/test_hermes_state.py -q` | **259 passed, 2 skipped** |
| Schema/repair four-file group | **36 passed, 1 failed, 4 skipped** |
| Dashboard/auth/profile six-file group | **284 passed, 1 failed, 5 skipped** |
| `tests/state/test_stage4a_remediation.py tests/state/test_compression_lineage_guard.py -q` | **43 passed** |
| Independent activation-gap probe | **Reproduced S4A-RR-01** |

Not run after the material blocker triggered the brief's stop condition:

- Ruff on changed Python files.
- Repository-wide Ruff.
- Python compilation.
- `git diff --check`.
- Standalone SQLite query-plan checks.
- Re-execution of the two baseline tests in the reference checkout.

## 14. Baseline-failure verification

The only failures in the required remediation-head groups are signature-for-
signature equivalent to the two previously demonstrated untouched-base
failures:

1. `test_repair_rebuilds_stale_btree_indexes` expected `wrong # of entries in
   index idx_messages_session`; SQLite 3.45.1 returned `row 1 missing from
   index ...; row 2 missing ...; row 3 missing ...`.
2. `TestSystemStatsEndpoint::test_stats_shape` received an empty `arch` under
   the clean Windows environment.

Neither failure touches the Stage 4A remediation diff, and neither is classified
as a Stage 4A regression. The reference checkout remained clean at the exact
runtime base. A fresh reference-checkout rerun was not performed after the
material blocker because the brief required the review to stop.

## 15. Final repository state and prohibited actions

Before this report, all three named checkouts were clean. At completion:

- Implementation checkout: branch `forge/stage-4a-remediation` remains at
  `50706d1e448fe5d1ac404d55596369dfa4dfeca3`; production implementation is
  unchanged; this report is the only intended untracked review artifact.
- Reference checkout: branch `forge/run-session-binding` remains at
  `4c4b37a7d792924f6e060baac2bed069fc85ddb8`; no modifications.
- Hermes Forge: branch `feat/stage-4-session-usage-contract` remains at
  `1c4039e85d9cd81d5401ec6fe8c2ae2cf4cb5ca8`; no modifications.

Nothing was merged, rebased, committed, or pushed. Fork `main` was not
modified. No NousResearch pull request was opened. Stage 4B and Stage 5 were
not started.

## 16. Final verdict

**CHANGES REQUIRED**

Stop here. The activation upgrade must fail closed instead of converting an
unproven aggregate/detail difference into a trusted historical baseline.
