# Stage 4A Cutover Marker Repair Remediation

Date: 2026-09-04

Final implementation status: **READY FOR THIRD INDEPENDENT RE-REVIEW**

This report records implementation evidence only. It is not approval to merge.
Stage 4B and Stage 5 were not started.

## Revisions and provenance

- Failed reviewed SHA preserved unchanged:
  `dc8ec986fa0012b1b7861630e84a03a3633842ed`
- Failed reviewed branch: `forge/stage-4a-s4a06-cutover-fix`
- Dedicated remediation branch:
  `forge/stage-4a-cutover-marker-repair-fix`
- Final implementation/test HEAD before this report-only commit:
  `4e161750c37e557765700ff2107dbe3777f7b44c`
- Final report-bearing HEAD: the commit containing this file,
  `docs(stage-4a): document marker-loss remediation`. Its exact object id is
  recorded in the completion handoff because a Git commit cannot contain its
  own hash.
- Fork `main` remained at `63279301bcbdc185c1b07b98a9312eb0c862f26d`.

The second independent re-review was preserved first. The exact failing
regression was committed before production source changed.

## Exact blocker reproduction

The permanent regression starts with a legacy aggregate of `3 calls / 105
tokens` and establishes a trusted cutover with detail high-water `0`. It then
records one valid post-cutover detail event of `1 call / 5 tokens` and advances
the aggregate by another `1 call / 5 tokens` without detail.

Before marker deletion:

| State | API calls | Input tokens |
|---|---:|---:|
| Trusted baseline | 3 | 105 |
| Current aggregate | 5 | 115 |
| Eligible post-cutover detail | 1 | 5 |
| Aggregate delta | 2 | 10 |

The response is correctly `PARTIAL / aggregate_detail_mismatch`, returning
only the eligible `1 call / 5 tokens` detail.

The regression then deletes only
`session.usage.detail.reconciliation.v2.trusted_cutover` and reopens the
database. Against `dc8ec986...`, the test failed because startup did not raise:
it replaced the baseline with `5 / 115`, advanced the high-water to `1`, and
could return `COMPLETE` zero. The test now proves writable reopen fails before
accounting and direct inspection still finds baseline `3 / 105`, aggregate
`5 / 115`, and detail `1 / 5`.

## Root cause

`_ensure_usage_detail_activation()` used marker presence as the only
distinction between an established cutover and a database that had never
performed one. If the marker was absent, it unconditionally deleted the
route-level baseline, copied mutable current aggregates, captured the current
event-id high-water, and wrote a replacement marker. The replacement was
transactional but semantically unsafe: atomic rebaselining still launders a
post-cutover accounting gap into history.

## Never initialized versus damaged

The initialization state machine now separates these cases before any
baseline mutation:

### Genuine first-time cutover

A database is eligible only when the trusted marker and permanent epoch record
are absent and the v2 reconciliation-baseline table did not exist before the
current schema open. This covers fresh and pre-v2 legacy stores. Only this
state may snapshot current aggregates and detail high-water under
`BEGIN IMMEDIATE`.

### Damaged existing cutover

Any of the following is durable evidence of an earlier trusted cutover:

- a pre-existing `session_usage_reconciliation_baseline` table, including an
  empty or partial table;
- the trusted marker;
- the permanent epoch record.

If that evidence is incomplete, malformed, inconsistent, or fails the
baseline manifest digest/count check, writable startup raises before normal
accounting begins. It never deletes baseline rows, advances the high-water, or
recaptures aggregate state. A read-only usage request returns `UNAVAILABLE`
rather than `COMPLETE`.

## Remediation

The internal marker format advances from version 2 to version 3 and gains a
random immutable `generation`. The complete v3 record contains:

- generation;
- cutover timestamp;
- detail event-id high-water;
- baseline row count;
- baseline SHA-256 digest.

The identical record is stored independently at
`session.usage.detail.reconciliation.v2.epoch`. Marker/epoch disagreement is
corruption. A valid legacy v2 marker and its digest-bound baseline may be
upgraded once to v3; that upgrade copies only already-trusted boundary data and
does not inspect mutable aggregates or advance the boundary.

For a genuine first cutover, baseline, high-water, marker, epoch, and legacy
compatibility metadata are written in the existing `BEGIN IMMEDIATE`
transaction. If that initial transaction fails, only the newly created empty
v2 baseline table is removed so a true first initialization can retry. A table
that predated the open is never removed or treated as authorization to recut.

The read path parses both records in its pinned SQLite snapshot, requires
exact equality, and separately verifies the complete route-baseline count and
digest before exact accounting is available.

## Schema and state implications

- No new table or public API field is introduced.
- One additive `state_meta` epoch key is created.
- The internal reconciliation marker version changes from 2 to 3; the public
  capability remains `session.usage.detail.v1`.
- Existing intact v2 cutovers upgrade atomically without changing cutover
  time, high-water, baseline rows, or digest.
- Existing v1 activation metadata remains compatibility-only and is not used
  as reconciliation authority.
- Repeated startup, schema probes, read API access, and normal repair leave the
  v3 generation, marker, epoch, baseline, and high-water unchanged.

## Corruption matrix

`test_damaged_cutover_corruption_matrix_never_rebaselines` parameterizes the
required states over a three-route `3 / 105` baseline and a live `5 / 115`
aggregate versus `1 / 5` detail gap.

| Case | Mutation | Result |
|---:|---|---|
| 1 | Marker only deleted | Read unavailable; writable startup fails |
| 2 | Entire baseline deleted | Read unavailable; writable startup fails |
| 3 | High-water field deleted | Read unavailable; writable startup fails |
| 4 | Epoch/integrity record deleted | Read unavailable; writable startup fails |
| 5 | One baseline route deleted | Read unavailable; writable startup fails |
| 6 | Multiple baseline routes deleted | Read unavailable; writable startup fails |
| 7 | Marker deleted and epoch high-water deleted | Read unavailable; writable startup fails |
| 8 | Marker and epoch/integrity record deleted | Pre-existing table proves damage; startup fails |
| 9 | Baseline and marker deleted while epoch remains | Read unavailable; writable startup fails |
| 10 | Baseline value altered | Digest mismatch; startup fails |
| 11 | High-water altered | Marker/epoch mismatch; startup fails |
| 12 | Baseline hash altered | Marker/epoch mismatch; startup fails |

For every case, the test snapshots marker/epoch and every baseline row before
the attempted writable reopen, then proves the failed startup made no change.
Aggregate remains `5 / 115`, detail remains `1 / 5`, and no case returns
`COMPLETE`.

## First initialization and idempotency

`test_true_first_cutover_atomically_captures_all_initial_state` removes every
v2 artifact from a legacy store containing aggregate `3 / 105` and one old
detail row. First initialization captures baseline `3 / 105`, high-water `1`,
v3 marker, generation, and identical epoch record. Reopen proves all values
remain unchanged.

`test_intact_legacy_v2_cutover_upgrades_without_rebaselining` converts a valid
fixture to the reviewed v2 marker format, removes only the not-yet-existing
epoch key, and reopens it. The upgrade preserves cutover time, high-water `0`,
every baseline row, and the visible `PARTIAL` accounting gap while adding the
new generation/epoch binding.

The existing rollback, repeated-open, zero-baseline, new-session,
multi-session lineage, and deterministic write-lock tests remain green.

## Verification results

All pytest commands used `scripts/run_tests.sh` through Git Bash and the
checkout's Windows virtual environment.

| Suite | Result |
|---|---|
| Trusted cutover plus 12-case corruption matrix | 29 passed |
| Usage API and all accounting/persistence producers | 71 passed |
| `tests/test_hermes_state.py` | 259 passed, 2 skipped |
| Schema/repair four-file group | 36 passed, 1 known baseline failure, 4 skipped |
| Dashboard/auth/profile six-file group | 284 passed, 1 known baseline failure, 5 skipped |
| Expanded adversarial/lineage/cutover group | 72 passed |
| Deterministic cutover concurrency test, isolated | 1 passed |

Static and structural checks:

- Ruff 0.16.6 on all eight changed Python files: passed.
- Repository-wide Ruff 0.16.6: passed.
- Python compilation on all eight changed Python files: passed.
- `git diff --check dc8ec986...HEAD`: passed.
- SQLite query plan:
  `SEARCH session_usage_events USING INDEX
  idx_session_usage_events_session_time (session_id=? AND recorded_at>?
  AND recorded_at<?)`.

The dashboard group also reported one unrelated analytics clamp test that
failed once and passed on the runner's automatic fresh-process retry. It is
recorded as a flake and did not affect the final result.

## Established environment baseline failures

The two accepted failures remain signature-for-signature unchanged:

1. `test_repair_rebuilds_stale_btree_indexes`: SQLite 3.45.1 reports
   `row N missing from index` rather than the expected
   `wrong # of entries in index` wording.
2. `TestSystemStatsEndpoint.test_stats_shape`: the hermetic Windows runner
   returns an empty `arch` because `PROCESSOR_ARCHITECTURE` is absent.

Neither failure touches the Stage 4A remediation.

## S4A-06 and S4A-08 conclusion

S4A-06 is covered because the post-cutover aggregate/detail gap remains
visible and cannot be absorbed into a replacement baseline. S4A-08 is covered
because every partial or malformed trusted-cutover state fails closed instead
of self-healing from mutable accounting data.

The failed reviewed SHA was not amended, rebased, or rewritten. Nothing was
merged into `forge/runtime` or fork `main`. No NousResearch pull request was
opened. The reference checkout and Hermes Forge were not modified. Stage 4B
and Stage 5 were not started.

## Conclusion

**READY FOR THIRD INDEPENDENT RE-REVIEW**
