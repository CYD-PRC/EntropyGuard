#!/bin/bash
# =============================================================================
# EntropyGuard Deploy Verification
# Run after every deployment to verify security posture
# Usage: ./deploy_verify.sh
# =============================================================================

set -e

echo "=== Deploy Verification ==="
echo "Timestamp: $(date)"
echo ""

# 1. Rebuild Docker image if Dockerfile changed
cd /root/AutoGPT
if git -C /root/AutoGPT/source diff --name-only HEAD~1 2>/dev/null | grep -q "Dockerfile\|requirements"; then
    echo "[INFO] Dockerfile or requirements changed, rebuilding..."
    docker compose build 2>&1 | tail -5
else
    echo "[INFO] No Docker changes detected, skipping rebuild"
fi

# 2. Run red team regression
echo ""
echo "[INFO] Running red team regression..."
/root/AutoGPT/scripts/run_redteam_suite.sh --json --quiet
RT_RC=$?

if [ $RT_RC -ne 0 ]; then
    echo ""
    echo "========================================="
    echo "DEPLOY BLOCKED: Red team regression failed"
    echo "========================================="
    exit 1
fi

# 3. Check host integrity
echo ""
echo "[INFO] Checking host integrity..."

/usr/local/bin/entropyguard-verify.py >/dev/null 2>&1 || {
    echo "FAIL: SHA-256 verification failed"
    exit 1
}
echo "PASS: SHA-256 verify"

systemctl is-active entropyguard >/dev/null 2>&1 || {
    echo "FAIL: EntropyGuard is down"
    exit 1
}
echo "PASS: EntropyGuard running"

systemctl is-active fail2ban >/dev/null 2>&1 || {
    echo "FAIL: fail2ban is down"
    exit 1
}
echo "PASS: fail2ban running"

# 4. Check iptables
DU_RULES=$(iptables -L DOCKER-USER -n 2>/dev/null | grep -c "DROP" || echo 0)
if [ "$DU_RULES" -lt 1 ]; then
    echo "WARN: DOCKER-USER chain missing DROP rule"
fi

IPSET_EXISTS=$(ipset list llm-api 2>/dev/null | head -1 || echo "")
if [ -z "$IPSET_EXISTS" ]; then
    echo "WARN: ipset llm-api not found"
fi

echo ""
echo "========================================="
echo "Deploy verification PASSED"
echo "========================================="
