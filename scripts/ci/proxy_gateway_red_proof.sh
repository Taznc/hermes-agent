#!/usr/bin/env bash
# RED-proof for tests/hermes_cli/test_proxy_gateway.py.
#
# These tests were written against an implementation that already existed, so a
# green run proves nothing on its own. Each mutation below breaks exactly one
# load-bearing behaviour the acceptance criteria name; the suite MUST fail on
# every one and pass on the unmutated tree.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

GATEWAY=hermes_cli/proxy/gateway.py
ROUTING=hermes_cli/proxy/routing.py
LEGS=hermes_cli/proxy/legs.py
BACKUP=/tmp/red-proof-$$

mkdir -p "$BACKUP"
cp "$GATEWAY" "$BACKUP/gateway.py"
cp "$ROUTING" "$BACKUP/routing.py"
cp "$LEGS" "$BACKUP/legs.py"

restore() {
  cp "$BACKUP/gateway.py" "$GATEWAY"
  cp "$BACKUP/routing.py" "$ROUTING"
  cp "$BACKUP/legs.py" "$LEGS"
}
trap restore EXIT

run_suite() {
  # Both suites: the gateway integration contract AND the routing unit
  # contract. The attempt bound is a RouteContext invariant that the visited
  # set already makes unreachable at the gateway level, so it can only be
  # RED-proven at the unit level — running one suite alone would score that
  # mutation as vacuous when it is in fact covered.
  scripts/run_tests.sh tests/hermes_cli/test_proxy_gateway.py \
    tests/hermes_cli/test_proxy_failover.py 2>&1 | grep -E "^=== Summary" | tail -1
}

check_mutation() {
  local label="$1"
  local summary
  summary="$(run_suite)"
  if echo "$summary" | grep -qE ", 0 tests passed, 0 failed"; then
    # The mutation did not produce importable code, so it proves nothing about
    # the assertions. Treat it as a broken mutation, not as RED evidence.
    echo "INVALID  $label -> $summary"
    FAILURES=$((FAILURES + 1))
  elif echo "$summary" | grep -qE ", 0 failed"; then
    echo "VACUOUS  $label -> $summary"
    FAILURES=$((FAILURES + 1))
  else
    echo "RED  OK  $label -> $summary"
  fi
  restore
}

FAILURES=0

echo "--- baseline (must be green) ---"
BASE="$(run_suite)"
echo "baseline: $BASE"
echo "$BASE" | grep -qE ", 0 failed" || { echo "BASELINE NOT GREEN"; exit 1; }

echo
echo "--- mutations (each must turn the suite RED) ---"

# M1: every failure becomes failover-eligible -> forbidden 4xx get replayed.
sed -i 's/^    return int(status) in FAILOVER_STATUS_CODES$/    return True/' "$ROUTING"
check_mutation "M1 all statuses failover-eligible (forbidden-4xx replay)"

# M2: no failure is failover-eligible -> Claude->Codex failover never happens.
sed -i 's/^    return int(status) in FAILOVER_STATUS_CODES$/    return False/' "$ROUTING"
check_mutation "M2 no status failover-eligible (failover never fires)"

# M3: visited set ignored -> a backend can be attempted twice.
sed -i 's/^        return backend not in self.visited$/        return True/' "$ROUTING"
check_mutation "M3 visited-set ignored (backend attempted twice)"

# M4: attempt bound removed -> attempts are unbounded.
sed -i 's/^        if self.attempts >= self.max_attempts:$/        if False:/' "$ROUTING"
check_mutation "M4 attempt bound removed"

# M5: circuit always admits -> open breaker still storms the dead backend.
sed -i 's/^            if now < state.opened_until:$/            if False:/' "$ROUTING"
check_mutation "M5 circuit admits during cooldown (probe storm)"

# M6: upstream Retry-After ignored -> cooldown never honours the reset deadline.
sed -i 's/^                cooldown = max(cooldown, max(0.0, float(retry_after_seconds)))$/                cooldown = cooldown/' "$ROUTING"
check_mutation "M6 Retry-After ignored"

# M7: route headers echo the client's spoofed values instead of minted ones.
sed -i 's/^        REQUEST_ID_HEADER: context.request_id,$/        REQUEST_ID_HEADER: "attacker-supplied",/' "$GATEWAY"
check_mutation "M7 request id not gateway-minted"

# M8: streaming chat never terminates the stream.
sed -i 's/^                await response.write(b"data: \[DONE\]\\n\\n")$/                pass/' "$GATEWAY"
check_mutation "M8 chat stream missing [DONE]"

# M9: Responses client gets the raw canonical chat object (protocol mismatch).
sed -i 's/^    from hermes_cli.proxy.responses_translate import chat_response_to_responses$/    return chat/' "$GATEWAY"
check_mutation "M9 Responses client served a chat object"

# M10: credential failure aborts the whole request instead of skipping to the
# next backend. Patch the `continue` two lines below its unique marker.
CRED_LINE=$(grep -n 'reason="credential_error"' "$GATEWAY" | cut -d: -f1)
sed -i "$((CRED_LINE + 2))s/continue/return _json_error(500, \"forced\", \"forced\")/" "$GATEWAY"
check_mutation "M10 credential failure not skipped"

# M11: tools are dropped on the Codex leg.
TRANSLATE=hermes_cli/proxy/responses_translate.py
cp "$TRANSLATE" "$BACKUP/responses_translate.py"
sed -i 's/^            payload\["tools"\] = converted$/            pass/' "$TRANSLATE"
summary="$(run_suite)"
cp "$BACKUP/responses_translate.py" "$TRANSLATE"
if echo "$summary" | grep -qE ", 0 tests passed, 0 failed"; then
  echo "INVALID  M11 tools dropped on Codex leg -> $summary"; FAILURES=$((FAILURES + 1))
elif echo "$summary" | grep -qE ", 0 failed"; then
  echo "VACUOUS  M11 tools dropped on Codex leg -> $summary"; FAILURES=$((FAILURES + 1))
else
  echo "RED  OK  M11 tools dropped on Codex leg -> $summary"
fi
restore

# M12: an abandoned half-open attempt keeps the only probe slot forever.
sed -i 's/^                if now - state\.probe_started_at < self\._probe_timeout:$/                if True:/' "$ROUTING"
check_mutation "M12 abandoned half-open probe lease never expires"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL MUTATIONS CAUGHT — the gateway suite is RED-capable."
else
  echo "$FAILURES MUTATION(S) NOT CAUGHT — those assertions are vacuous."
  exit 1
fi
