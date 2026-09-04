# Stage 4A Second Independent Re-Review — Trusted Cutover

Review date: 2026-09-04

Capability: `session.usage.detail.v1`

Public endpoint: `GET /api/sessions/{session_id}/usage`

Final verdict: **CHANGES REQUIRED**

## 1. Reviewed SHAs

| Item | Verified value |
|---|---|
| Reviewed HEAD | `dc8ec986fa0012b1b7861630e84a03a3633842ed` |
| Branch | `forge/stage-4a-s4a06-cutover-fix` |
| Tracking branch | `origin/forge/stage-4a-s4a06-cutover-fix` at the same SHA (`+0/-0`) |
| Previous failed remediation | `50706d1e448fe5d1ac404d55596369dfa4dfeca3` |
| Original reviewed Stage 4A | `87efab652f8f7a3ea64a48ce86b693e9e6e259e7` |
| Runtime base | `forge/runtime` at `4c4b37a7d792924f6e060baac2bed069fc85ddb8` |
| Hermes Forge | `1c4039e85d9cd81d5401ec6fe8c2ae2cf4cb5ca8` |

All named commits resolved as commit objects. The ancestry chain
`runtime base -> original review -> failed remediation -> reviewed HEAD` was
verified. The implementation and Hermes Forge working trees were clean before
review. Hermes Forge remained untouched.

## 2. Second-remediation commit list

Oldest first:

1. `78f70d5979` — `docs(stage-4a): record independent remediation re-review`
2. `961a56f113` — `test(usage): reproduce activation baseline gap absorption`
3. `77a117b307` — `fix(usage): establish trusted accounting cutover`
4. `a6ecfa63fe` — `test(usage): prevent activation baseline gap absorption`
5. `dc8ec986fa` — `docs(stage-4a): document trusted cutover remediation`

The delta from `50706d1e...` changes nine files with 1,503 insertions and 174
deletions. All four required Stage 4A reports were read completely and their
conclusions were treated as hypotheses.

## 3. Git cleanliness

The implementation checkout was clean at the start of review. The independent
probe used only a disposable temporary database outside all named repositories.
At completion this report is the only intended untracked review artifact in the
implementation checkout. Hermes Forge remains clean at its expected HEAD.

## 4. Original blocker reproduction

Source and regression inspection confirms the second remediation no longer
subtracts existing detail from aggregate during the initial v2 cutover. It
captures the full route aggregate and the event-id high-water in one write
transaction. The included exact regression constructs aggregate `3 calls / 105
tokens` with existing detail `1 call / 1 token` and asserts the persisted v2
baseline is `3 / 105`, not `2 / 104`.

The fresh review found the material defect below before executing that suite.
Per the brief's stop-on-material-defect instruction, the remaining prescribed
test commands, including an independent rerun of that fixture, were not run.

## 5. Trusted-cutover architecture assessment

The normal first v2 cutover uses `BEGIN IMMEDIATE`, captures
`MAX(session_usage_events.id)`, copies the complete route-level
`session_model_usage` table, computes a canonical SHA-256 manifest digest and
row count, writes compatibility metadata and the trusted marker, then commits.
Existing detail is not subtracted. On an intact marker, startup validates the
complete manifest before proceeding.

The architecture is not safe across repair. If the v2 marker is absent,
`_ensure_usage_detail_activation()` cannot distinguish first deployment from
loss/corruption after a completed cutover. It unconditionally deletes the old
trusted baseline, snapshots current aggregates, advances the detail high-water,
and writes a new trusted marker. This recomputation can hide a genuine gap.

## 6. High-water mark assessment

For an intact cutover, the event-id boundary is stronger than timestamps:
rows at or below the captured ID are excluded even if future-dated, and rows
above it participate in reconciliation independently of timestamp ordering.

The missing-marker repair makes the boundary mutable. In the independent
reproduction it advanced from `0` to `1`, reclassifying a legitimate
post-cutover `1 call / 5 tokens` event as pre-cutover history. Thus the proposed
high-water semantics are correct only while the marker survives; repair can
silently move the epoch.

## 7. Integrity-binding assessment

The digest covers every route key (`session_id`, model, provider, billing base
URL, billing mode, and task) and all eight accounting metrics. Canonical sorting
makes physical row order irrelevant. The marker separately stores the baseline
row count, digest, cutover time, and event-id high-water. With a present marker,
missing, inserted, deleted, or modified baseline rows are detected and malformed
metadata fails closed.

The binding does not preserve trust when the marker itself is missing. Startup
replaces both the baseline and its binding from mutable current state. That is
the decisive integrity failure: absence is treated as authorization to mint a
new epoch rather than as unsafe ambiguity.

## 8. Atomicity assessment

Inspection confirms baseline replacement, compatibility snapshot, high-water,
digest/count binding, and marker creation are committed within one explicit
`BEGIN IMMEDIATE` transaction and roll back together on an exception. No
partial-transaction defect was found before the stop condition.

Atomic creation does not cure later marker-loss recovery: the unsafe recut is
also atomic, but atomically trusting an already-inconsistent aggregate is still
incorrect.

## 9. Concurrency assessment

The write lock serializes committed aggregate/detail writers against an intact
cutover, placing each committed transaction on one side of the boundary. The
new blocker does not require concurrency: deleting only the durable marker and
performing an ordinary writable reopen is sufficient to move the baseline and
high-water. The requested independent concurrency matrix was not continued
after the BLOCKER was reproduced.

## 10. Multi-route accounting assessment

The new baseline is route-granular and reconciliation unions aggregate,
baseline, and eligible-detail routes, so discrepancies cannot normally cancel
between models/providers/tasks. The missing-marker recut applies to every route,
however, and can absorb a discrepancy in any or all dimensions by replacing
the entire manifest from current aggregates.

## 11. Coverage/window assessment

With intact metadata, pre-cutover windows fail closed, crossing windows are
partial, wholly post-cutover windows require reconciliation, and zero delta may
return complete zero. After the reproduced repair, the API returned
`COMPLETE / exact_detail_available_no_usage` for a new wholly post-recut window
despite a previously detected accounting gap. The apparent zero is created by
moving the trust boundary, not by proving the missing usage.

## 12. Lineage assessment

The read path reconciles each physical session/route before combining lineage
events, which is the correct structure. No new lineage-specific defect was
identified before the stop condition. Because the trusted baseline is global,
marker-loss recut can launder gaps affecting any physical session in a lineage;
lineage-level checks cannot recover the lost epoch distinction afterward.

## 13. Schema-repair assessment

Schema repair is unsafe. The explicit test
`test_incomplete_marker_fails_closed_and_baseline_without_marker_recuts`
codifies the problematic behavior: marker plus damaged baseline fails, but
baseline without marker is silently recut. The review brief explicitly requires
missing cutover metadata to fail closed when ambiguity could conceal
post-cutover missing usage. The implementation does the opposite.

## 14. S4A-01 through S4A-09 status

| Finding | Result | Basis |
|---|---|---|
| S4A-01 | RESOLVED | Previously independently established; no new queue-loss path found before stop. |
| S4A-02 | RESOLVED | Previously independently established; strict lineage code is outside the recut defect. |
| S4A-03 | RESOLVED | Previously independently established; branch-forward traversal is unchanged. |
| S4A-04 | RESOLVED | Previously independently established; numeric guards remain present. |
| S4A-05 | RESOLVED | Previously independently established; bounded event projection remains present. |
| S4A-06 | NOT RESOLVED | Missing-marker recut absorbs a genuine post-cutover aggregate/detail gap and can return incorrect `COMPLETE`. |
| S4A-07 | RESOLVED | Pinned read snapshot remains present; not implicated in the reproduced recut. |
| S4A-08 | NOT RESOLVED | New cutover schema repair fails open by silently recreating trust from mutable aggregate state. |
| S4A-09 | RESOLVED | Synchronized future-window validation remains present. |

Statuses other than S4A-06/S4A-08 carry forward the prior independent
re-review plus source inspection. They were not regression-rerun after the
material defect because the brief required the review to stop.

## 15. Test results

| Check | Result |
|---|---|
| Independent missing-marker/gap probe | **BLOCKER reproduced** |
| Targeted trusted-cutover suite (reported 14) | Not run after stop condition |
| State/API suite (reported 29) | Not run after stop condition |
| Accounting/persistence (reported 59) | Not run after stop condition |
| `tests/test_hermes_state.py` (reported 259 passed, 2 skipped) | Not run after stop condition |
| Schema/repair group | Not run after stop condition |
| Dashboard/auth/profile group | Not run after stop condition |
| Expanded adversarial/lineage suite (reported 57) | Not run after stop condition |
| Changed-file Ruff / repository Ruff | Not run after stop condition |
| `git diff --check 50706d1e...HEAD` | Passed |
| Python compilation | Not run after stop condition |
| SQLite query-plan verification | Not run after stop condition |

The disposable probe used the checkout's Windows virtual environment and
SQLite 3.45.1, with imports pinned to reviewed HEAD.

## 16. Baseline-failure status

The two accepted baseline failures were not rerun after the BLOCKER triggered
the stop condition. Their prior independently established signatures remain:

1. `test_repair_rebuilds_stale_btree_indexes` — SQLite diagnostic wording.
2. `TestSystemStatsEndpoint::test_stats_shape` — empty Windows `arch` under the
   hermetic environment.

No new test-suite failure was classified.

## 17. New findings

### S4A-SRR-01 — BLOCKER — Missing trusted marker silently recuts over a live accounting gap

Affected code:

- `hermes_state_schema.py:1217-1267` treats absence of the trusted marker as a
  new cutover regardless of surviving post-cutover state.
- `hermes_state_schema.py:1268-1287` deletes the old baseline, snapshots current
  aggregates, and binds them as trusted.
- `hermes_state_schema.py:1304-1330` writes the replacement marker and commits.
- `tests/state/test_stage4a_s4a06_cutover.py:413-440` explicitly expects a
  baseline without marker to recut.

Independent reproduction:

1. Establish trusted baseline `3 calls / 105 tokens`, high-water `0`.
2. Record one valid post-cutover event `1 call / 5 tokens`, producing aggregate
   `4 / 110` and eligible detail `1 / 5`.
3. Advance aggregate only by another `1 / 5`, producing aggregate `5 / 115`
   versus detail `1 / 5`.
4. Confirm the API returns `PARTIAL / aggregate_detail_mismatch`.
5. Delete only `session.usage.detail.reconciliation.v2.trusted_cutover` and
   reopen writable.
6. Inspect persisted state and query from the replacement cutover.

Observed:

| Value | Before marker loss | After reopen/recut |
|---|---:|---:|
| Trusted baseline | `3 / 105` | `5 / 115` |
| Event high-water | `0` | `1` |
| Current aggregate | `5 / 115` | `5 / 115` |
| Previously eligible detail | `1 / 5` | excluded by new high-water |
| Coverage | `PARTIAL` mismatch | `COMPLETE` zero |

The missing `1 call / 5 tokens` is absorbed into the replacement baseline, and
the one valid post-cutover event is simultaneously moved behind the new
high-water. This produces an incorrect `COMPLETE`, hides an accounting gap,
and violates baseline immutability. It therefore meets the brief's BLOCKER
definition.

Required direction: durable state must distinguish legitimate first v2
activation from loss/corruption after activation. Once any evidence of a prior
cutover or post-cutover epoch exists, missing trusted metadata must fail closed;
startup must not mint a replacement baseline from current aggregate state.

## 18. Final verdict

**CHANGES REQUIRED**

Approval criteria are not met: S4A-06 and S4A-08 remain unresolved, and
S4A-SRR-01 is a new BLOCKER that can return incorrect `COMPLETE` and hide a
missing accounting event.

Nothing was merged, rebased, committed, pushed, or force-pushed. Fork `main`
was not modified. No NousResearch pull request was opened. Hermes Forge was
untouched. Stage 4B and Stage 5 were not started.
