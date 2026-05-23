# EntropyGuard Threat Model

**Version:** 1.0
**Date:** 2026-05-23
**Status:** Verified — Red Team v2: 10/10 BLOCKED
**Author:** Security review session

---

## 1. Assets

| Asset | Location | Sensitivity | Protection |
|-------|----------|------------|------------|
| LLM API keys | `/root/.env` | **High** | Docker non-mount + `env_file` injection + `chmod 600` |
| GEAR_PROMPTS | `/etc/entropyguard/gear_prompts.py` | **High** | SHA-256 + systemd ExecStartPre verification |
| Verifier | `/usr/local/bin/entropyguard-verify.py` | **High** | HMAC signature + wrapper script |
| Audit log | `/root/EntropyGuard/events.json` | **High** | `chattr +a` + backup every 5 min |
| HMAC key | `/etc/entropyguard/hmac.key` | **Critical** | `chmod 600` root-only |
| SSH access | Port 2222, Ed25519 key | **Critical** | Key-only auth, password disabled |
| Host filesystem | Entire server | **Critical** | Docker isolation + iptables + fail2ban |

## 2. Trust Boundaries

```
Internet
    |
    v
[iptables INPUT] -- only 2222/80/ICMP
    |
    +-- SSH (2222) -- key auth -- host shell
    |
    +-- HTTP (80) -- Nginx -- Gunicorn -- EntropyGuard API
                                            |
                                +-----------+-----------+
                                |           |           |
                              [Docker]  [Host proc]  [cron]
                              AutoGPT   gunicorn    backup
                              uid=1000  root        root
                              read-only
                                |
                                v
                        [DOCKER-USER iptables]
                        ipset llm-api whitelist
                                |
                                v
                        [LLM APIs only]
```

### Trust Levels

| Component | Trust Level | Rationale |
|-----------|-------------|-----------|
| Host root | Full trust | Administrator |
| EntropyGuard API (gunicorn) | High trust | Runs as root, all operations audited |
| Docker container (AutoGPT) | **Low trust** | non-root + read-only fs + network isolation |
| LLM models (DeepSeek/Qwen/Kimi) | **No trust** | Susceptible to prompt injection |
| External network | No trust | Potential attacker presence |

## 3. Adversary Model

| Adversary | Capability | Goal | Current Defense |
|-----------|-----------|------|----------------|
| External brute-force | Network scanning | SSH login | fail2ban + port 2222 + key-only auth |
| LLM prompt injection | Craft malicious prompts | Bypass AutoGPT restrictions | Docker isolation + read-only fs |
| AutoGPT autonomous action | `execute_shell`, Python exec | Modify system / exfiltrate data | non-root + DOCKER-USER + ipset |
| Container escape | Exploit Docker vulnerability | Gain host root | no-new-privileges + non-root user |
| Compromised host root | Root access | Tamper audit / backdoor | HMAC + SHA-256 + watchdog |

## 4. Assumptions

| Assumption | Risk if Wrong | Mitigation |
|------------|---------------|------------|
| Docker daemon is trustworthy | Container escape | Keep Docker updated |
| Linux kernel has no 0day | Container escape via kernel | Regular security patches |
| LLM API providers are trustworthy | API key leakage | Key rotation + minimal permissions |
| Administrator won't act maliciously | Insider threat | HMAC + SHA-256 detection |
| Alibaba Cloud infrastructure is trustworthy | Side-channel attacks | Cloud provider security measures |

## 5. Non-Goals

The following are **explicitly out of scope** for EntropyGuard:

- **Linux kernel 0day / container escape 0day** — requires kernel-level defense
- **Physical access attacks** — out of scope for software-only defense
- **Supply chain attacks** (pip package poisoning) — requires reproducible builds
- **LLM API provider-side data leakage** — outside our control
- **DDoS attacks** — requires CDN/infrastructure-level defense
- **Social engineering** — requires organizational security policies

## 6. Security Properties

| Property | Mechanism | Verification |
|----------|-----------|-------------|
| **Confidentiality** | `.env` excluded from image + `chmod 600` + non-root container | ATK-01a/b |
| **Integrity** | SHA-256 chain + HMAC + `chattr +a` | ATK-02/03/07 |
| **Availability** | Watchdog + fallback routing + health probe | Manual test |
| **Isolation** | Docker non-root + read-only fs + DOCKER-USER | ATK-04/05/06/09 |
| **Auditability** | `events.json` + audit chain + `/api/health` | API query |
| **Least Privilege** | ipset whitelist + read-only mounts | ATK-08 |

## 7. Attack Vector Coverage

| # | Attack | Target Property | Defense Layer | Result |
|---|--------|----------------|---------------|--------|
| 1 | Read API key | Confidentiality | Docker non-mount + `RUN rm` | BLOCKED |
| 2 | Modify GEAR_PROMPTS | Integrity | Docker read-only fs | BLOCKED |
| 3 | Modify verifier | Integrity | Docker read-only fs + HMAC | BLOCKED |
| 4 | Modify systemd | Isolation | No systemd in container | BLOCKED |
| 5 | Modify crontab | Isolation | No crontab + read-only fs | BLOCKED |
| 6 | Python code bypass | Isolation | non-root + read-only fs | BLOCKED |
| 7 | Tamper audit log | Integrity | Docker read-only fs + `chattr` | BLOCKED |
| 8 | Data exfiltration | Least Privilege | DOCKER-USER + ipset whitelist | BLOCKED |
| 9 | Kill defense processes | Availability | No host PID namespace | BLOCKED |
| 10 | Combined attack | All | All layers combined | BLOCKED |

### Automated Regression

```bash
# One-command verification after every deployment
./scripts/run_redteam_suite.sh --json

# CI hook
./scripts/deploy_verify.sh
```

## 8. Known Limitations

| Limitation | Impact | Acceptance Reason |
|------------|--------|-------------------|
| Host root can bypass `chattr` | Host-side persistence | AutoGPT runs in container, can't reach host |
| Docker daemon exploitable via 0day | Full system compromise | Kernel-level, not application defense |
| iptables/ipset lost on Docker restart | Outbound rules temporarily absent | DOCKER-USER chain + rebuild script |
| HMAC key stored on filesystem | Root can read | Future: migrate to KMS |
| ipset may not contain all LLM API IPs | Some API calls may fail | Dynamic ipset update capability |
| ATK-08b: Container LLM API access depends on ipset accuracy | Intermittent connectivity | Monitoring + manual ipset refresh |

## 9. Security Architecture Decisions

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Docker containerization | VM, kata-containers, firecracker | Docker is lightweight, fast to deploy, sufficient isolation for threat model |
| ipset whitelist (not port-based) | Allow all 443/80 outbound | Least privilege: only known LLM API IPs allowed |
| VERDICT-based test assertions | Exit code + grep patterns | Explicit markers eliminate false positives/negatives from ambiguous output |
| Non-root container user (uid=1000) | Root container with capabilities | Defense in depth: even if container is compromised, attacker has no root |
| Read-only container filesystem | Writable with overlays | Prevents any persistent modification inside container |
| `env_file` instead of `.env` mount | Volume mount `.env:ro` | `env_file` doesn't expose file inside container; `RUN rm` removes build-time copy |
| HMAC wrapper for verifier | Signed ELF binary | Simpler, sufficient for detecting file tampering by non-root |
| 2GB swap file | No swap (OOM) | Server has only 1.8GB RAM; Docker build needs more |
| Minimal requirements (18 packages) | Full requirements (75+) | Faster build, smaller attack surface, no spacy/selenium |

## 10. Change Log

| Date | Version | Change |
|------|---------|--------|
| 2026-05-23 | 1.0 | Initial threat model — red team v2 verified 10/10 BLOCKED |
