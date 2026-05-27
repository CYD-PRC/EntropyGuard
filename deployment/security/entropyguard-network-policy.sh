#!/bin/bash
# =============================================================================
# EntropyGuard Network Policy — 幂等重建 ipset + iptables DOCKER-USER 链
# 在以下场景自动调用：
#   - Docker 服务重启后
#   - 系统重启后
#   - deploy_verify.sh 中
# =============================================================================

set -euo pipefail

IPSET_NAME="llm-api"

# LLM API 域名（动态解析）
LLM_DOMAINS="api.deepseek.com dashscope.aliyuncs.com api.moonshot.cn"

# 阿里云内部服务
ALIYUN_INTERNAL="100.100.30.25 100.100.100.200"

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# 1. 重建 ipset
log "Rebuilding ipset $IPSET_NAME..."
ipset create $IPSET_NAME hash:ip -exist 2>/dev/null || true
ipset flush $IPSET_NAME

# 添加 LLM API IP（动态解析，仅 IPv4）
for domain in $LLM_DOMAINS; do
    for ip in $(getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'); do
        ipset add $IPSET_NAME "$ip" -exist 2>/dev/null || true
        log "  + $domain -> $ip"
    done
done

# 添加阿里云内部服务
for ip in $ALIYUN_INTERNAL; do
    ipset add $IPSET_NAME "$ip" -exist 2>/dev/null || true
    log "  + aliyun-internal -> $ip"
done

TOTAL=$(ipset list $IPSET_NAME 2>/dev/null | grep -cE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || echo 0)
log "ipset $IPSET_NAME: $TOTAL entries"

# 2. 配置 DOCKER-USER 链（幂等）
log "Configuring DOCKER-USER chain..."
iptables -F DOCKER-USER 2>/dev/null || true

# 顺序：ESTABLISHED -> DNS -> ipset -> DROP
iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# UDP 53: drop non-whitelist DNS queries (fixes ATK-13 DNS leak)
iptables -A DOCKER-USER -p udp --dport 53 -m set ! --match-set $IPSET_NAME dst -j DROP
iptables -A DOCKER-USER -p udp --dport 53 -j RETURN
iptables -A DOCKER-USER -p tcp --dport 53 -j RETURN
iptables -A DOCKER-USER -m set --match-set $IPSET_NAME dst -j RETURN
iptables -A DOCKER-USER -j DROP

DU_RULES=$(iptables -L DOCKER-USER -n 2>/dev/null | grep -c -v '^Chain\|^target\|^$' || echo 0)
log "DOCKER-USER: $DU_RULES rules"

# 3. 确保宿主机 OUTPUT 链有 ipset 白名单（幂等，不破坏已有规则）
log "Checking OUTPUT chain..."
# 如果已经有 ipset match 规则，则跳过
if ! iptables -L OUTPUT -n 2>/dev/null | grep -q "match-set $IPSET_NAME"; then
    # 找到 LOG 或 DROP 规则的行号，在其之前插入 ipset 白名单
    INSERT_BEFORE=$(iptables -L OUTPUT -n --line-numbers 2>/dev/null | grep -E 'LOG |DROP ' | head -1 | awk '{print $1}')
    if [ -n "$INSERT_BEFORE" ]; then
        iptables -I OUTPUT "$INSERT_BEFORE" -m set --match-set $IPSET_NAME dst -j ACCEPT
        log "OUTPUT: inserted ipset whitelist at rule $INSERT_BEFORE"
    else
        iptables -A OUTPUT -m set --match-set $IPSET_NAME dst -j ACCEPT
        log "OUTPUT: appended ipset whitelist"
    fi
else
    log "OUTPUT: ipset whitelist already present, skipping"
fi

log "Network policy applied successfully."

# ========== INPUT 链：保护 SSH 和 Web 入站 ==========
log "Configuring INPUT chain..."
# 只在 INPUT 为空时才重建（避免重复添加）
CURRENT_INPUT_RULES=$(iptables -L INPUT -n | grep -c -v '^Chain\|^target\|^$')
if [ "$CURRENT_INPUT_RULES" -le 1 ]; then
    iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A INPUT -p tcp --dport 2222 -j ACCEPT
    iptables -A INPUT -p tcp --dport 80 -j ACCEPT
    iptables -A INPUT -p tcp --dport 443 -j ACCEPT
    iptables -A INPUT -p icmp -j ACCEPT
    iptables -P INPUT DROP
    log "INPUT chain rebuilt: SSH + HTTP + HTTPS + ICMP"
else
    log "INPUT chain already has rules, skipping"
fi
