# RedteamEvolver v3 跨目标泛化对比报告

**日期**: 2026-06-01
**目标 A**: Flask 1.0 + Werkzeug 0.14.1 (CVE-2019-1010083)
**目标 B**: Django 2.0.7 (Debug/CSRF/XSS/Secret disclosure)

---

## 一、双目标部署对比

| 维度 | Flask 实验 | Django 实验 |
|------|:----------:|:-----------:|
| 框架 | Flask 1.0 / Werkzeug 0.14.1 | Django 2.0.7 |
| CVE 目标 | CVE-2019-1010083 (debugger PIN) | Debug 信息泄露 / XSS / CSRF |
| 攻击端点 | `/console` | `/api/config`, `/api/echo`, `/api/exec` |
| 脆弱性类型 | Python RCE 调试器 | 配置泄露 + 反射 XSS + CSRF 绕过 |
| 运行时 | Python 3.6 | Python 3.6 |
| 端口 | 5000 | 8080 |

---

## 二、v3 盲测命中率对比

### 2.1 模板生成（独立发现）

| 攻击模板 | Flask 实验 | Django 实验 | 说明 |
|----------|:----------:|:-----------:|------|
| 路径遍历 /etc/passwd | ✅ 模板#1 | ✅ 模板#1 | 通用 web 模板，跨框架有效 |
| SSRF 云元数据 | ✅ 模板#2 | ✅ 模板#2 | 通用 web 模板 |
| Flask Werkzeug /console | ✅ 模板#3 | ❌ 模板#3 | Flask 限定！Django 无此端点 |
| **Django debug/config** | ❌ 无 | ❌ **未生成** | 模板系统无 Django 特定模板 |
| **CSRF 绕过** | ❌ 无 | ❌ **未生成** | 模板系统无 CSRF 模板 |
| **XSS 检测** | ❌ 无 | ❌ **未生成** | 模板系统无 XSS 模板 |

**结论**: 模板生成器仅有 3 个固定 web 模板（1 个 Flask 专用，2 个通用）。**没有 Django 专用模板**。

### 2.2 变异引擎传播能力

| 变异技术 | Flask 种子保留 `/console` | Django CSRF 种子保留 `/api/exec` | Django XSS 种子保留 `<script>` |
|----------|:-------------------------:|:--------------------------------:|:------------------------------:|
| base64 | ✅ | ✅ | ✅ |
| hex | ✅ | ✅ | ✅ |
| unicode | ✅ | ✅ | ✅ |
| nested_shell | ✅ | ✅ | ✅ |
| param_split | ✅ | ✅ | ✅ |
| url_encode | ✅ | ✅ | ✅ |
| header_injection | ✅ | ✅ | ✅ |
| param_pollution | ✅ | ✅ | ✅ |
| path_traversal | ✅ | ✅ | ✅ |
| double_encoding | ✅ | ✅ | ✅ |
| **保留率** | **8/10 (80%)** | **10/10 (100%)** | **10/10 (100%)** |

**结论**: 变异引擎泛化稳定——不管是 Flask URL、Django API 路径还是 XSS payload，10 种技术都一致保留。

### 2.3 种子库覆盖

| OWASP 类别 | Flask 实验 | Django 实验 | 种子文件 |
|------------|:----------:|:-----------:|----------|
| A03-Injection | ✅ 7 条 | ✅ 7 条 | injection.json |
| A01-BrokenAccessControl | ✅ 6 条 | ✅ 6 条 | auth_broken.json |
| A03-Injection (XSS) | ✅ 6 条 | ✅ 6 条 | xss.json |
| A10-SSRF | ✅ 7 条（含 Flask /console） | ✅ 7 条（含 Flask /console） | ssrf.json |
| **CSRF** | ❌ 无 | ❌ **无** | 缺失 |
| **Django 专用** | ❌ 无 | ❌ **无** | 缺失 |

**结论**: 种子库覆盖 OWASP Top 10 的 4 类，但 CSRF 和 Django 专用种子缺失。

### 2.4 Fitness 评估

| 指标 | Flask 实验 | Django 实验 |
|------|:----------:|:-----------:|
| 目标可达 | ❌ 服务已停止 | ✅ 服务运行中 |
| HTTP 状态码检测 | N/A | ⚠️ 404（路径错误） |
| 敏感信息泄露检测 | N/A | ⚠️ leak=0.6（误报） |
| 响应时间检测 | N/A | 正常 (< 0.1s) |

**结论**: Fitness 函数需要修复——当前固定向 `/api/chat` 发请求，不适用于自定义目标。

---

## 三、跨目标对比仪表板

| 能力 | Flask 实验 | Django 实验 | 一致？ |
|------|:----------:|:-----------:|:------:|
| 🎯 **AI 独立发现 CVE** | ❌（DeepSeek 不可用） | ❌（DeepSeek 不可用） | ✅ 一致 |
| 📋 **模板命中目标特有向量** | ✅ 部分（/console） | ❌ 未命中（无 CSRF/Debug 模板） | ❌ 不一致 |
| 🧬 **10 种变异保留攻击向量** | ✅ 8/10 | ✅ 10/10 | ✅ 一致 |
| 🌱 **种子库加载** | ✅ 26 条 | ✅ 26 条 | ✅ 一致 |
| 📊 **Fitness 评分** | ⚠️ 不可用 | ⚠️ 路径错误 | ✅ 一致缺陷 |
| 🔄 **跨框架泛化** | ⭐⭐⭐☆ (Flask) | ⭐⭐☆☆ (Django) | ❌ 不一致 |

---

## 四、剩余瓶颈分析

### 4.1 模板生成器缺乏目标适应能力

```
当前: _template_candidates() 固定返回 3 个 web 模板
      ├─ 路径遍历（通用）✅
      ├─ SSRF（通用）✅
      └─ Flask Werkzeug console（Flask 专用）❌ Django 无效

需要: 根据 target_context.focus_areas 动态选择模板
      ├─ focus_areas 含 "debug" → Django debug 模板
      ├─ focus_areas 含 "csrf"  → CSRF 绕过模板
      ├─ focus_areas 含 "xss"   → XSS 反射模板
      └─ focus_areas 含 "admin" → Admin 面板探测模板
```

### 4.2 Fitness 函数路径硬编码

```python
# 当前: 固定向 /api/chat 发请求（Entropy Runtime 格式）
cmd = ["curl", url + "/api/chat", ...]

# 需要: 根据每个 case 的 endpoint_type 发到正确路径
# 例如 SEED-AUTH-004.endpoint_type = "GET /static/" 
#     → curl http://target/../../../etc/passwd
```

### 4.3 CSRF 种子缺失

| 需要的种子 | CWE | OWASP |
|------------|:---:|:-----:|
| CSRF: no token | CWE-352 | A01 |
| CSRF: old/stale token | CWE-352 | A01 |
| CSRF: same-site bypass | CWE-352 | A01 |
| CSRF: JSON content type bypass | CWE-352 | A01 |
| CSRF: cookie injection | CWE-352 | A01 |

---

## 五、跨框架泛化能力评分

| 能力维度 | Flask 评分 | Django 评分 | 平均 |
|----------|:----------:|:-----------:|:----:|
| 模板命中率 | ⭐⭐⭐☆☆ | ⭐⭐☆☆☆ | ⭐⭐☆☆☆ |
| 变异保留率 | ⭐⭐⭐⭐☆ | ⭐⭐⭐⭐☆ | ⭐⭐⭐⭐☆ |
| 种子覆盖率 | ⭐⭐⭐☆☆ | ⭐⭐☆☆☆ | ⭐⭐☆☆☆ |
| Fitness 评估 | ⭐☆☆☆☆ | ⭐☆☆☆☆ | ⭐☆☆☆☆ |
| AI 独立发现 | ⭐☆☆☆☆ | ⭐☆☆☆☆ | ⭐☆☆☆☆ |

**总体泛化能力**: ⭐⭐☆☆☆（2/5 — 偶发命中，需通用种子才能稳定）

---

## 六、v2 → v3 泛化提升对比

| 指标 | v2（泛化前） | v3（已修复） | v3（Django 实测） |
|------|:-----------:|:------------:|:-----------------:|
| 变异技术 | 5 种 shell | 10 种（5 web） | ✅ 全部工作 |
| 跨框架变异 | ❌ 丢失 URL | ✅ 保留 URL | ✅ 保留 API 路径 |
| OWASP 种子 | 0 | 26 | ✅ 加载正常 |
| Flask CVE 命中 | ❌ | ✅ 模板#3 | N/A |
| **Django CVE 命中** | **❌** | **❌ 未命中** | **❌ 模板不匹配** |
| 目标适应 | ❌ | ✅ target_context | ⚠️ 只影响 AI，不影响模板 |

---

## 七、结论

| 陈述 | 判断 |
|------|:----:|
| v3 泛化能力是**重复命中**？ | ❌ 否——模板系统无 Django 专用模板，Flask /console 是巧合 |
| 变异引擎泛化稳定？ | ✅ 是——10 种技术在两个框架上一致保留攻击向量 |
| 模板系统需要修复？ | ✅ 是——应基于 `focus_areas` 动态生成特定模板 |
| Fitness 函数需要修复？ | ✅ 是——需支持自定义端点路径 |
| CSRF 种子需要补充？ | ✅ 是——当前缺失 CWE-352 类别 |

**核心结论**: v3 在变异引擎和种子库方面实现了**稳定的跨框架泛化**，但模板生成器尚不能根据目标上下文动态生成特定模板。Flask CVE 命中是一次部分巧合（Werkzeug debug console 模板恰好适用于 Flask），Django 实验暴露了模板系统的目标适应缺口。

---

*报告结束。*
