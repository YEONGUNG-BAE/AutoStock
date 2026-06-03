#!/usr/bin/env bash
# AutoStock Ops Foundation — acceptance regression gate
# macOS 기본 bash 3.2 호환. set -e 미사용: 한 check 실패 후에도 나머지를 계속 실행한다.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "[PASS] $1"
}

warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    echo "[WARN] $1"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "[FAIL] $1"
}

# Check 1 — pytest
check_pytest() {
    local output
    local exit_code
    output="$(uv run pytest tests/ -v 2>&1)"
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        fail "pytest: command failed (exit $exit_code)"
        return
    fi
        if echo "$output" | grep -q "2591 passed"; then
        pass "pytest: 2591 passed"
    else
        warn "pytest: exit 0 but baseline '2591 passed' not found"
    fi
}

# Check 2 — config smoke
check_config_smoke() {
    local output
    local exit_code
    output="$(PYTHONPATH=src uv run python -c "from config.settings import load_settings; print(load_settings('config/config.toml.example').trading.mode)" 2>&1)"
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        fail "config smoke: command failed (exit $exit_code)"
        return
    fi
    if echo "$output" | grep -q "paper"; then
        pass "config smoke: paper"
    else
        fail "config smoke: expected 'paper' in output"
    fi
}

# Check 3 — AccountRole smoke
check_account_role() {
    local output
    local exit_code
    local role
    local bad
    output="$(PYTHONPATH=src uv run python -c "from domain import AccountRole; print([r.value for r in AccountRole])" 2>&1)"
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        fail "AccountRole smoke: command failed (exit $exit_code)"
        return
    fi
    for role in KR_TAX_ADVANTAGED US_REGULAR CASH_BUFFER PAPER; do
        if ! echo "$output" | grep -q "'$role'"; then
            fail "AccountRole smoke: missing required role '$role'"
            return
        fi
    done
    for bad in ISA GENERAL CMA; do
        if echo "$output" | grep -q "'$bad'"; then
            fail "AccountRole smoke: forbidden legacy role '$bad' present"
            return
        fi
    done
    pass "AccountRole smoke"
}

# Check 4 — ExecutionMode smoke
check_execution_mode() {
    local output
    local exit_code
    local mode
    output="$(PYTHONPATH=src uv run python -c "from config.settings import ExecutionMode; print([e.value for e in ExecutionMode])" 2>&1)"
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        fail "ExecutionMode smoke: command failed (exit $exit_code)"
        return
    fi
    for mode in normal rebalancing emergency_trigger mdd_killswitch manual; do
        if ! echo "$output" | grep -q "'$mode'"; then
            fail "ExecutionMode smoke: missing required mode '$mode'"
            return
        fi
    done
    pass "ExecutionMode smoke"
}

# Check 5 — tracked artifacts
check_tracked_artifacts() {
    local output
    output="$(git ls-files | grep -E '__pycache__|\.pyc$|\.db$|\.sqlite$|\.sqlite3$|\.DS_Store$' || true)"
    if [ -z "$output" ]; then
        pass "tracked artifacts: none"
    else
        fail "tracked artifacts: forbidden paths tracked in git"
    fi
}

# Check 6 — legacy AccountRole references
check_legacy_account_role() {
    local output
    output="$(grep -R "AccountRole\.ISA\|AccountRole\.GENERAL\|AccountRole\.CMA" src tests --include="*.py" || true)"
    if [ -z "$output" ]; then
        pass "legacy AccountRole references: none"
    else
        fail "legacy AccountRole references: found in src/tests"
    fi
}

# Check 7 — KIS mock adapter reintroduction
check_kis_mock_adapter() {
    local output
    output="$(grep -RE "from .*kis_mock|import .*kis_mock|class .*KisMock|KisMockAdapter|kis_mock_adapter" src tests --include="*.py" || true)"
    if [ -z "$output" ]; then
        pass "KIS mock adapter reintroduction: none"
    else
        fail "KIS mock adapter reintroduction: found mock adapter import/class"
    fi
}

# Check 8 — tiny-live submit function
check_tiny_live_submit() {
    local output
    output="$(grep -R "submit_tiny_live_order\|place_tiny_live_order" src --include="*.py" || true)"
    if [ -z "$output" ]; then
        pass "tiny-live submit function: absent"
    else
        fail "tiny-live submit function: found in src/"
    fi
}

# Check 9 — auto_apply true
check_auto_apply_true() {
    local output
    output="$(grep -R "auto_apply=True\|auto_apply = True" src --include="*.py" || true)"
    if [ -z "$output" ]; then
        pass "auto_apply=True: absent"
    else
        fail "auto_apply=True: found in src/"
    fi
}

# Check 10 — committed KIS account number pattern
check_kis_account_pattern() {
    local output
    output="$(grep -R "KIS_.*ACCOUNT.*[0-9][0-9][0-9]" src config docs --include="*.py" --include="*.md" --include="*.example" || true)"
    if [ -z "$output" ]; then
        pass "KIS account number pattern: absent"
    else
        fail "KIS account number pattern: found in src/config/docs"
    fi
}

check_pytest
check_config_smoke
check_account_role
check_execution_mode
check_tracked_artifacts
check_legacy_account_role
check_kis_mock_adapter
check_tiny_live_submit
check_auto_apply_true
check_kis_account_pattern

# Check 11 — runtime generated artifacts tracked in git
check_runtime_generated_artifacts() {
    local output
    output="$(git ls-files | grep -E '^runtime/synthetic/|^runtime/research/' || true)"
    if [ -z "$output" ]; then
        pass "runtime generated artifacts: none"
    else
        fail "runtime generated artifacts: generated files tracked in git"
    fi
}

check_runtime_generated_artifacts

echo ""
echo "Summary: $PASS_COUNT PASS, $WARN_COUNT WARN, $FAIL_COUNT FAIL"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
