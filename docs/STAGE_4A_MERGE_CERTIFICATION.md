# Stage 4A Merge Certification

Status: **STAGE 4A MERGED AND CERTIFIED**

Certification date: 2026-09-05 (Asia/Kuala_Lumpur)

## Repository and provenance

- Merge worktree: `C:\Users\khairul\Projects\hermes-agent-stage4a-merge`
- Repository origin: `https://github.com/kairul-dev/hermes-agent.git`
- Repository upstream: `https://github.com/NousResearch/hermes-agent.git`
- Target branch: `forge/runtime`
- Approved branch: `origin/forge/stage-4a-final-transport-hardening`
- Approved implementation SHA: `2593bc7acb00e8b52c5aa522001861e7bffcf013`
- Pre-merge runtime SHA: `4c4b37a7d792924f6e060baac2bed069fc85ddb8`
- Resulting runtime implementation SHA: `2593bc7acb00e8b52c5aa522001861e7bffcf013`
- Merge method: fast-forward of the exact approved SHA; no squash, rebase, amendment, conflict resolution, or force-push

After `git fetch origin --prune`, both remote refs resolved to the required commits. The approved local branch and its remote had `0 0` parity. The historical approved-branch worktree contained an unrelated untracked certification document; it was not modified or used for the merge. The immutable approved commit and remote ref were used as the provenance authority. The dedicated runtime merge worktree was clean before the merge.

## Ancestry and branch state

`git merge-base --is-ancestor 2593bc7acb00e8b52c5aa522001861e7bffcf013 HEAD` completed successfully. The implementation merge initially left `forge/runtime` 21 commits ahead and 0 behind `origin/forge/runtime`. After the documentation-only certification commit and final normal push, local/remote parity was verified as `0 0`, and the worktree was clean.

The local and remote `main` refs remained at `63279301bcbdc185c1b07b98a9312eb0c862f26d` throughout the operation.

## Post-merge verification

All test invocations used Windows CPython 3.11.9, pytest 9.0.2, SQLite 3.45.1, the canonical `scripts/run_tests.sh` runner, and `--file-retries 0`.

| Verification | Result |
|---|---|
| Damaged-state transport and deterministic bootstrap | 31 passed, 0 failed |
| Focused usage/API suite | 108 passed, 0 failed |
| Accounting/persistence suite | 59 passed, 0 failed |
| Full Hermes state suite | 259 passed, 2 skipped, 0 failed |
| Expanded adversarial suite | 94 passed, 0 failed |
| Schema/repair suite | 36 passed, 4 skipped, 1 accepted baseline failure |
| Dashboard/auth/profile suite | 285 passed, 5 skipped, 1 accepted baseline failure |
| Deterministic cutover concurrency | 1 passed, 0 failed |
| 20,000-row production SQLite query plan | 1 passed, 0 failed |
| Ruff 0.16.6, 15 changed Python files | Passed |
| Ruff 0.16.6, repository-wide | Passed; two existing invalid-`noqa` warnings |
| Python compilation, 15 changed Python files | Passed |
| `git diff --check` for `4c4b37a...HEAD` | Passed |
| `git diff --check` for the working tree | Passed |

### Accepted baseline failures

The two failures matched the committed untouched-runtime receipts in `docs/stage4a-final-transport-evidence/`:

1. `tests/test_state_db_malformed_repair.py::test_repair_rebuilds_stale_btree_indexes` expected SQLite text containing `wrong # of entries in index idx_messages_session`; SQLite 3.45.1 returned the same established `row 1 missing ...; row 2 missing ...; row 3 missing ...` text. This was the only schema/repair failure.
2. `tests/hermes_cli/test_dashboard_admin_endpoints.py::TestSystemStatsEndpoint::test_stats_shape` observed the established Windows environment value `arch == ""`. This was the only dashboard/auth/profile failure.

No new failures were observed.

### Bootstrap-race regression

`tests/hermes_cli/test_stage4a_bootstrap.py` passed both variants of `test_visible_zero_byte_bootstrap_waits_for_initializer` with one worker and retries disabled. Together with the damaged-state transport file, the isolated group completed with 31 passed and no failures.

### Deterministic concurrency

`test_cutover_write_lock_excludes_concurrent_usage_writer` passed in an isolated one-worker invocation with retries disabled.

### SQLite query plan

`test_production_usage_projection_uses_session_time_index` passed with 20,000 `session_usage_events` rows. It verified correct target totals, a `SEARCH session_usage_events` plan using `idx_session_usage_events_session_time` with both time bounds, and no full table scan.

## Scope controls

- Hermes Forge remained untouched at `C:\Users\khairul\Projects\hermes-forge`, branch `feat/stage-4-session-usage-contract`, SHA `1c4039e85d9cd81d5401ec6fe8c2ae2cf4cb5ca8`.
- The Hermes Agent fork `main` branch was not modified or pushed.
- Historical Stage 4A review/remediation worktrees were not modified.
- No artifact was recreated, substituted, cherry-picked, squashed, rebased, or amended.
- Stage 4B was not started.
- Stage 5 was not started.
