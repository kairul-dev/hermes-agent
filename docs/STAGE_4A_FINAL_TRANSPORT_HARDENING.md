# Stage 4A Final Transport Hardening

Date: 2026-09-05 (Asia/Kuala_Lumpur)

Implementation status: **READY FOR FINAL INDEPENDENT RE-REVIEW**.
This report is an implementation handoff, not approval to merge.

## Provenance

- Reviewed SHA, unchanged: `08d055291c5929bc63bbde717a0f4c14258051fc`.
- Dedicated branch: `forge/stage-4a-final-transport-hardening`, created directly at that SHA.
- Untouched runtime comparison: `4c4b37a7d792924f6e060baac2bed069fc85ddb8` (`forge/runtime`).
- Final remediation implementation/test SHA: `fb1786f990541e0f00efb2068e853ceca4f5c039`.
- The report and raw receipts are in a subsequent documentation commit. Its exact
  SHA is supplied in the handoff; a commit cannot embed its own object ID.

Existing branches, reviewed commits, fork main, Hermes Forge, and all previous
reports were preserved. Nothing was merged. Stage 4B and Stage 5 were not started.

## MEDIUM finding and transport contract

A missing trusted activation marker returned HTTP 200 with unavailable coverage,
while complete ordinary-artifact erasure entered the writable schema-heal path
and returned HTTP 503 without a structured code. Accounting was fail closed,
but transport depended on the particular missing artifact.

Damaged ACTIVE usage state now returns HTTP **503**, `totals: null`, and
`coverage.reason_code: TRUSTED_USAGE_STATE_DAMAGED`. No exact zero is fabricated.
The existing capability payload and coverage object are retained. The existing
`coverage.reason` diagnostic identifiers remain available; the additive
`reason_code` is the stable identifier for the public failure class.

Example coverage excerpt:

```json
{
  "coverage": {
    "status": "UNAVAILABLE",
    "reason": "coverage_start_unknown",
    "reason_code": "TRUSTED_USAGE_STATE_DAMAGED",
    "message": "Exact session usage is unavailable because trusted accounting state is damaged."
  },
  "totals": null
}
```

Clients branch on `coverage.reason_code`, never message text. Messages may
change. Public failures contain no paths, SQLite errors, exception strings,
stack traces, epoch IDs, integrity material, or private billing URLs.

| Failure | HTTP | Stable `coverage.reason_code` |
|---|---:|---|
| Required trusted state damaged in an established epoch | 503 | `TRUSTED_USAGE_STATE_DAMAGED` |
| Accounting synchronization/flush fails | 503 | `ACCOUNTING_SYNCHRONIZATION_FAILED` |
| Store busy, unreadable, or otherwise unavailable | 503 | `USAGE_STATE_UNAVAILABLE` |

These service failures always carry null totals. Healthy requests remain 200.
Legitimate historical-window/lineage coverage limitations retain their existing
coverage behavior. Fresh initialization and canonical incomplete initialization
remain recoverable; the public route completes initialization even when an
incomplete schema shell already passes column probes.

Usage reads bypass writable schema healing whenever authoritative epoch or
trusted metadata evidence survives. Missing baseline tables remain missing during
the request. Integrity validation and classification run within the existing
accounting read snapshot. SQLite schema failures in an ACTIVE usage snapshot
map to the damaged family; lock/I/O failures retain the general unavailable
family. No message parsing is required by clients.

## Marker-only and complete-erasure reproduction

The fixture establishes a valid epoch with a baseline of 3 calls / 105 input
tokens, aggregate 5 / 115, and post-cutover detail 1 / 5. Before damage it reports
PARTIAL, correctly preserving the known accounting gap, over HTTP 200.

| Damage | Reviewed SHA | Remediation |
|---|---|---|
| Delete only trusted activation marker; retain other evidence | 200, UNAVAILABLE, null totals | 503, damaged reason code, null totals |
| Delete marker and integrity record and drop baseline table; retain authoritative epoch evidence | 503, unstructured detail; writable heal could recreate an empty baseline table | 503, same damaged reason code, null totals; no schema heal |

The new transport suite was run before changing production: **27 failed,
2 passed**. Marker-only failed on `200 != 503`; complete erasure failed the
structured-response assertion. After remediation all **29** transport controls
pass. Each damage case issues two public requests and compares the full SQLite
logical dump after each request against the dump immediately after deliberate
damage. This verifies no baseline recreation/capture, high-water movement,
replacement epoch, metadata changes, or other persisted state changes.

Receipts: [before](stage4a-final-transport-evidence/transport-before.txt),
[after](stage4a-final-transport-evidence/targeted-final.txt).

## Corruption transport matrix

Every row below returns 503 with `TRUSTED_USAGE_STATE_DAMAGED`, UNAVAILABLE
coverage, null totals, and an unchanged logical database dump on both requests.
The original 12-case and 8-case accounting matrices remain in place; their
mutation helpers are reused by HTTP tests.

| Group | Damage |
|---|---|
| Original | Marker only |
| Original | All baseline rows removed |
| Original | Marker high-water removed |
| Original | Integrity binding removed |
| Original | One baseline route removed |
| Original | Multiple baseline routes removed |
| Original | Marker removed and integrity high-water removed |
| Original | Marker and integrity binding removed |
| Original | Baseline rows and marker removed |
| Original | Baseline value altered |
| Original | High-water altered |
| Original | Integrity hash altered |
| Extended | Baseline table dropped |
| Extended | Epoch identity row removed |
| Extended | Epoch identity table removed |
| Extended | Epoch generation altered |
| Extended | Active schema shell with identity row, baseline rows, and metadata missing |
| Extended | Integrity generation inconsistent |
| Extended | Epoch table and all ordinary cutover artifacts erased; schema generation survives |
| Extended | Schema generation inconsistent |
| Additional | Complete ordinary-artifact erasure with epoch identity retained |
| Additional | Malformed marker JSON |
| Additional | Malformed baseline numeric value |
| Additional | Epoch identity schema corrupted |
| Additional | Detail table missing |
| Additional | Inconsistent cutover timestamp |

## Dashboard race: evidence and root cause

The unchanged reviewed code reproduced an HTTP 500 in
`TestAnalyticsDaysClamps.test_in_range_days_accepted` on attempt 2 of the first
comparison. The original file uses `raise_server_exceptions=False`, so its
first traceback is the actual `assert 500 == 200` at line 62. It is preserved
in [before-h-2.txt](stage4a-final-transport-evidence/before-h-2.txt).

A diagnostic copy changed only `raise_server_exceptions` to True. Its first
run exposed the underlying exception:

```text
_get_usage_analytics
  -> _open_session_db_for_profile
  -> _open_session_db_at_path (zero-byte bootstrap)
  -> SessionDB.__init__
  -> _init_schema
  -> _ensure_usage_detail_activation
RuntimeError: incomplete trusted session usage cutover baseline
```

The full first diagnostic traceback is retained in
[bootstrap-diagnostic-1.txt](stage4a-final-transport-evidence/bootstrap-diagnostic-1.txt).

Exact mechanism:

1. Dashboard lifespan starts eager reconciliation in a daemon thread.
2. SQLite connection creation exposes the profile database at zero bytes,
   before schema initialization has committed.
3. The request sees zero bytes and starts another writable bootstrap.
4. The old initializer guard depended on absence or a zeroed-file probe. The
   probe intentionally returns False for a live tracked connection to avoid
   unsafe raw reads of an open SQLite file. The second initializer therefore
   bypasses the guard even while initialization is in progress.
5. It can observe `baseline_table_preexisting=False` before another initializer
   creates the schema and commits the epoch. That pre-DDL boolean survives
   into the later `BEGIN IMMEDIATE` activation validation.
6. Stage 4A's required integrity check then sees a committed marker together
   with that stale False observation and raises the error above.

This is a Stage 4A regression exposing an older coordination gap, not an
unexplained flaky test. Retry success is not evidence of correctness.

Fix: every writable SessionDB initializer now participates in the existing
per-path cross-process initialization/quarantine lock through connection and
schema initialization. A visible file/header cannot bypass it. Lock timeout
uses the existing 20-second write patience and reports busy instead of
proceeding uncoordinated. Dashboard eager reconciliation and request-time
bootstrap/probe/healing share the existing process bootstrap lock; the profile
path is captured before starting the eager thread. Healthy read-only opens do
not acquire a SQLite write lock or request a checkpoint.

No sleeps or retries were added to the tests. The existing lock acquisition
implementation supplies coordination; no new sleep/retry workaround was added
to production. The event-controlled regression pauses an actual tracked
connection at zero bytes and requires the contender to enter coordination
before releasing the initializer. The reviewed code fails that invariant;
the remediation passes both direct SessionDB and dashboard variants.

## Controlled base comparison and stress

All runs use Windows CPython 3.11.9 from the same runtime virtual environment,
pytest 9.0.2, SQLite 3.45.1, Git Bash, and the unchanged canonical runner.
`-j 1` and `--file-retries 0` are explicit. The runner provides the same clean
environment, UTC locale settings, fresh per-file subprocess, and fresh
`hermes-pytest-tmproot-*` directory behavior on every revision.

The initial reproduction series recorded 6 failing files / 12 reviewed runs
and 0 / 12 base runs. A diagnostic run overlapped the tail of that exploratory
series; it is not used as the final controlled frequency comparison.

The final series runs reviewed (`r`), base (`b`), and remediation (`h`)
sequentially in that order for 12 cycles, followed by 8 more remediation runs.
No other test commands run concurrently with this series. Every attempt is
retained; no failed run is silently replaced or certified by a retry.

| Revision | Runs | Passed | Failed files | Failed attempts |
|---|---:|---:|---:|---|
| Reviewed SHA | 12 | 7 | 5 | 4, 7, 8, 10, 11 |
| Untouched forge/runtime | 12 | 12 | 0 | None |
| Remediation | 20 | 20 | 0 | None |

All 20 remediation runs passed all 9 cases (180 case executions). The controlled
comparison confirms a material Stage 4A regression and supports the coordination
fix. Finite stress runs are evidence, not a claim that timing failures are impossible.

Exact command in each worktree:

```powershell
$env:HERMES_PYTHON='C:/Users/khairul/Projects/hermes-agent-forge-runtime/.venv/Scripts/python.exe'
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh tests/hermes_cli/test_dashboard_param_clamps.py -j 1 --file-retries 0 -v --tb=long
```

See [every attempt](stage4a-final-transport-evidence/stress-summary.txt) and
`stress-{r,b,h}-{attempt}.txt` for individual outputs. The test file and runner
are unchanged across the three revisions.

The text receipts have only trailing whitespace normalized for Git. The
[raw receipt archive](stage4a-final-transport-evidence/raw-receipts.zip)
preserves every original log byte, including the first failure and full
first diagnostic traceback. Archive entries are also indexed by SHA-256 in
[receipt-hashes.txt](stage4a-final-transport-evidence/receipt-hashes.txt).

## Full verification

| Suite | Result |
|---|---|
| New transport and deterministic bootstrap | 31 passed |
| Focused established suite | 108 passed |
| State | 259 passed, 2 skipped |
| Expanded adversarial | 94 passed |
| Accounting/persistence | 59 passed |
| Schema/repair | 36 passed, 1 established baseline failure, 4 skipped |
| Dashboard/auth/profile | 285 passed, 1 established baseline failure, 5 skipped |
| Isolated deterministic cutover write lock | 1 passed |
| Production 20,000-row query plan, reviewed and remediation | 1 passed on each |
| Changed-file Ruff 0.16.6 | Passed |
| Repository-wide Ruff 0.16.6 | Passed; two existing invalid-noqa warnings |
| Changed Python compilation | Passed |
| `git diff --check` | Passed |

The exact file groups and repeatable commands are recorded in
[commands.ps1](stage4a-final-transport-evidence/commands.ps1). Full group logs
are `matrix-{focused,state,adversarial,accounting,schema,dashboard}.txt`.
Every group used the canonical runner with `-j 4 --file-retries 0 -q --tb=short`.

The query-plan test captures the SQL actually executed by
`get_session_usage_detail`, then applies EXPLAIN to that projection. Both
revisions produce:

```text
SEARCH session_usage_events USING INDEX idx_session_usage_events_session_time
(session_id=? AND recorded_at>? AND recorded_at<?)
```

The fixture asserts 20,000 ledger rows and correct target totals. See the
`query-plan-{reviewed,final}.xml` receipts for engine version and plan.

Both accepted environment failures were independently rerun on untouched
`forge/runtime` with the same interpreter and runner:

- `test_repair_rebuilds_stale_btree_indexes`: SQLite 3.45.1 says
  `row N missing from index`, while the assertion requires
  `wrong # of entries in index`.
- `TestSystemStatsEndpoint.test_stats_shape`: hermetic Windows omits
  `PROCESSOR_ARCHITECTURE`, leaving `arch` empty.

They match the remediation suite failures exactly. Baseline receipts are
`baseline-schema.txt` and `baseline-dashboard.txt`; neither is relabeled as a
passing check.

The preserved suites exercise S4A-01 through S4A-09, S4A-SRR-01, S4A-TIRR-01,
and S4A-TIRR-02: epoch identity and no rebaselining, crash recovery, corruption,
queue retry safety, snapshots, lineage, numeric validation, profile isolation,
authentication, event-row bounds, and indexed queries.

Final implementation status: **READY FOR FINAL INDEPENDENT RE-REVIEW**.
