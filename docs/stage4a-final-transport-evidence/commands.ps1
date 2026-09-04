# Run from the remediation checkout. Each invocation uses per-file subprocess
# isolation. Outputs and exit codes must be retained, including baseline failures.
$env:HERMES_PYTHON='C:/Users/khairul/Projects/hermes-agent-forge-runtime/.venv/Scripts/python.exe'
$bash='C:\Program Files\Git\bin\bash.exe'

# New transport/initialization/bootstrap controls.
& $bash scripts/run_tests.sh tests/hermes_cli/test_stage4a_transport.py tests/hermes_cli/test_stage4a_bootstrap.py -j 1 --file-retries 0 -q --tb=short

# Established focused group.
& $bash scripts/run_tests.sh tests/state/test_stage4a_s4a06_cutover.py tests/state/test_stage4a_remediation.py tests/state/test_session_usage_detail.py tests/hermes_cli/test_session_usage_api.py -j 4 --file-retries 0 -q --tb=short

# State.
& $bash scripts/run_tests.sh tests/test_hermes_state.py -j 4 --file-retries 0 -q --tb=short

# Expanded adversarial.
& $bash scripts/run_tests.sh tests/state/test_stage4a_remediation.py tests/state/test_compression_lineage_guard.py tests/state/test_stage4a_s4a06_cutover.py -j 4 --file-retries 0 -q --tb=short

# Accounting/persistence.
& $bash scripts/run_tests.sh tests/agent/test_async_token_accounting.py tests/agent/test_background_review_usage.py tests/hermes_state/test_aux_usage_accounting.py tests/run_agent/test_token_persistence_non_cli.py tests/state/test_session_model_usage_pk_heal.py tests/state/test_session_usage_detail.py -j 4 --file-retries 0 -q --tb=short

# Schema/repair: one established assertion failure on this interpreter.
& $bash scripts/run_tests.sh tests/test_schema_read_probe.py tests/test_zeroed_state_db.py tests/test_state_db_malformed_repair.py tests/state/test_session_model_usage_pk_heal.py -j 4 --file-retries 0 -q --tb=short

# Dashboard/auth/profile: one established empty-arch failure.
& $bash scripts/run_tests.sh tests/hermes_cli/test_session_usage_api.py tests/hermes_cli/test_web_server.py tests/hermes_cli/test_dashboard_admin_endpoints.py tests/hermes_cli/test_dashboard_param_clamps.py tests/hermes_cli/test_profiles_sidebar_scope.py tests/hermes_cli/test_dashboard_auth_gate.py -j 4 --file-retries 0 -q --tb=short

# Isolated deterministic cutover concurrency.
& $bash scripts/run_tests.sh tests/state/test_stage4a_s4a06_cutover.py -k test_cutover_write_lock_excludes_concurrent_usage_writer -j 1 --file-retries 0 -q --tb=short

# Query plan on actual production SQL (also run from reviewed SHA with this new test copied in).
& $bash scripts/run_tests.sh tests/state/test_stage4a_query_plan.py -j 1 --file-retries 0 -q --tb=short --junitxml=../evidence/query-plan-final.xml

# Run these exact two commands from untouched forge/runtime for base receipts:
# & $bash scripts/run_tests.sh tests/test_state_db_malformed_repair.py -k test_repair_rebuilds_stale_btree_indexes -j 1 --file-retries 0 -q --tb=short
# & $bash scripts/run_tests.sh tests/hermes_cli/test_dashboard_admin_endpoints.py -k test_stats_shape -j 1 --file-retries 0 -q --tb=short

# Initializer regression on reviewed SHA (new test copied into isolated reviewed worktree):
# & $bash scripts/run_tests.sh tests/hermes_cli/test_stage4a_bootstrap.py -k False -j 1 --file-retries 0 -q --tb=short

# Stress: from a parent containing r (reviewed), b (runtime), h (remediation),
# with an existing evidence directory, run the following sequential loop.
# No other test processes ran concurrently with the final stress series.
# foreach ($attempt in 1..20) {
#   $variants = if ($attempt -le 12) { @('r','b','h') } else { @('h') }
#   foreach ($variant in $variants) {
#     Push-Location $variant
#     & $bash scripts/run_tests.sh tests/hermes_cli/test_dashboard_param_clamps.py -j 1 --file-retries 0 -v --tb=long *> "../evidence/stress-$variant-$attempt.log"
#     $result = $LASTEXITCODE
#     Pop-Location
#     "attempt=$attempt variant=$variant exit=$result" | Tee-Object -FilePath evidence/stress-summary.txt -Append
#   }
# }

# Static checks used Ruff 0.16.6, available locally at:
# C:\Users\khairul\AppData\Local\uv\cache\archive-v0\2jIdslWWe_fuDOoh\ruff-0.16.6.data\scripts\ruff.exe
# ruff check <changed Python files>
# ruff check .
# <same Python> -m py_compile <changed Python files>
# git diff --check 08d055291c5929bc63bbde717a0f4c14258051fc
