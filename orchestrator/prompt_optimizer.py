"""Entropy Runtime · Prompt 自优化引擎
v7.2 Phase 4.2: 根据执行效果自动调整 prompt。

核心流程:
  1. PromptMetrics — 记录每次 prompt 调用结果
  2. PromptEvaluator — 评估不同版本效果
  3. PromptTuner — 用 LLM 生成改进版本
  4. A/B Test — 分流对比

作用于三个关键 prompt:
  - decompose: 目标拆解 prompt
  - route: Agent 路由 prompt
  - system: 系统级 prompt（cosmic_lense + config）
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("entropyruntime.prompt_optimizer")

# ========== 常量 ==========

OPTIMIZER_DB = "/var/lib/entropyguard/prompt_metrics.json"
MIN_SAMPLES_FOR_EVAL = 5           # 最低样本数才评估
MIN_SAMPLES_FOR_AB = 10            # A/B 测试最低样本
POOR_SUCCESS_RATE = 0.5            # 成功率低于 50% 触发优化
AB_WIN_MARGIN = 0.1                # A/B 测试胜出需要 10% 优势


# ========== 数据模型 ==========

@dataclass
class PromptCall:
    """单次 prompt 调用的结果记录"""
    prompt_key: str                  # "decompose" / "route" / "system"
    version: str                     # "v1.0" / "v1.1" / "ab-a" / "ab-b"
    success: bool
    duration: float = 0.0
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class EvalResult:
    """prompt 版本评估结果"""
    prompt_key: str
    version: str
    success_rate: float
    avg_duration: float
    total_calls: int
    failure_count: int
    failure_examples: list[dict] = field(default_factory=list)
    needs_optimization: bool = False
    reason: str = ""


# ========== Prompt 指标存储 ==========

class PromptMetrics:
    """持久化记录 prompt 调用效果指标。

    使用 JSON 文件存储，格式:
    {
      "decompose": {
        "v1.0": [PromptCall, ...],
        "v1.1": [...],
      },
      "route": { ... },
      "system": { ... },
    }
    """

    def __init__(self, db_path: str = OPTIMIZER_DB):
        self.db_path = db_path
        self._data: dict[str, dict[str, list[dict]]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        for key in ("decompose", "route", "system"):
            self._data.setdefault(key, {})

    def _save(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with open(self.db_path, "w") as f:
            json.dump(self._data, f, indent=2)

    def record_call(self, call: PromptCall):
        """记录一次 prompt 调用结果。"""
        versions = self._data.setdefault(call.prompt_key, {})
        calls = versions.setdefault(call.version, [])
        calls.append({
            "success": call.success,
            "duration": call.duration,
            "error": call.error,
            "timestamp": call.timestamp,
            **call.metadata,
        })
        # 只保留最近 200 条记录
        if len(calls) > 200:
            calls[:] = calls[-200:]
        self._save()
        logger.debug(
            "[PromptMetrics] %s@%s: %s (%.1fs)",
            call.prompt_key, call.version,
            "✅" if call.success else "❌", call.duration,
        )

    def record(
        self, prompt_key: str, version: str,
        success: bool, duration: float = 0.0,
        error: str = "", **metadata,
    ):
        """便捷方法记录一次调用。"""
        self.record_call(PromptCall(
            prompt_key=prompt_key, version=version,
            success=success, duration=duration,
            error=error, metadata=metadata,
        ))

    def get_calls(
        self, prompt_key: str, version: Optional[str] = None,
        limit: int = 100,
    ) -> list[PromptCall]:
        """获取指定 prompt 的调用记录。"""
        versions = self._data.get(prompt_key, {})
        if version:
            raw = versions.get(version, [])
        else:
            raw = []
            for v in versions.values():
                raw.extend(v)
        raw.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return [
            PromptCall(
                prompt_key=prompt_key,
                version=v.get("version", version or "?"),
                success=v.get("success", False),
                duration=v.get("duration", 0.0),
                error=v.get("error", ""),
                timestamp=v.get("timestamp", 0),
                metadata={k: v for k, v in v.items()
                         if k not in ("success", "duration", "error", "timestamp", "version")},
            )
            for v in raw[:limit]
        ]

    def get_versions(self, prompt_key: str) -> list[str]:
        """获取所有已知版本号。"""
        return list(self._data.get(prompt_key, {}).keys())

    def evaluate(self, prompt_key: str, version: str) -> EvalResult:
        """评估指定版本的 prompt 效果。"""
        calls = self.get_calls(prompt_key, version)
        if not calls:
            return EvalResult(
                prompt_key=prompt_key, version=version,
                success_rate=0.0, avg_duration=0.0,
                total_calls=0, failure_count=0,
                needs_optimization=False,
                reason="暂无数据",
            )

        successes = sum(1 for c in calls if c.success)
        total = len(calls)
        rate = successes / max(total, 1)
        avg_dur = sum(c.duration for c in calls) / total
        failures = [c for c in calls if not c.success]

        # 判断是否需要优化
        needs_opt = False
        reason = ""
        if total >= MIN_SAMPLES_FOR_EVAL and rate < POOR_SUCCESS_RATE:
            needs_opt = True
            reason = (
                f"成功率 {rate:.0%} < 阈值 {POOR_SUCCESS_RATE:.0%}"
                f" ({successes}/{total})"
            )
        elif avg_dur > 30 and total >= MIN_SAMPLES_FOR_EVAL:
            needs_opt = True
            reason = f"平均耗时 {avg_dur:.1f}s 过高"

        return EvalResult(
            prompt_key=prompt_key, version=version,
            success_rate=rate, avg_duration=avg_dur,
            total_calls=total, failure_count=total - successes,
            failure_examples=[
                {"error": f.error, "duration": f.duration}
                for f in failures[:5]
            ],
            needs_optimization=needs_opt,
            reason=reason,
        )

    def get_stats(self) -> dict:
        """获取统计概览。"""
        stats = {}
        for key in ("decompose", "route", "system"):
            versions = self._data.get(key, {})
            stats[key] = {
                "total_calls": sum(len(v) for v in versions.values()),
                "versions": list(versions.keys()),
            }
            best = self.get_best_version(key)
            if best:
                stats[key]["best_version"] = best.version
                stats[key]["best_rate"] = best.success_rate
        return stats

    def get_best_version(self, prompt_key: str) -> Optional[EvalResult]:
        """返回当前最优版本的评估结果。"""
        versions = self.get_versions(prompt_key)
        if not versions:
            return None
        results = [self.evaluate(prompt_key, v) for v in versions]
        valid = [r for r in results if r.total_calls >= MIN_SAMPLES_FOR_EVAL]
        if not valid:
            return max(results, key=lambda r: r.success_rate) if results else None
        return max(valid, key=lambda r: r.success_rate)

    def needs_optimization(self, prompt_key: str) -> bool:
        """检查是否需要优化。"""
        best = self.get_best_version(prompt_key)
        if not best:
            return False
        return best.needs_optimization

    def get_failure_examples(self, prompt_key: str, limit: int = 5) -> list[PromptCall]:
        """获取最近的失败案例。"""
        all_calls = self.get_calls(prompt_key, limit=100)
        return [c for c in all_calls if not c.success][:limit]


# ========== Prompt 调优器 ==========

class PromptTuner:
    """根据失败案例用 LLM 优化 prompt。"""

    def __init__(self, metrics: Optional[PromptMetrics] = None):
        self.metrics = metrics or PromptMetrics()

    def tune(
        self, prompt_key: str, current_prompt: str,
        failure_cases: Optional[list[PromptCall]] = None,
    ) -> Optional[str]:
        """优化 prompt。

        Args:
            prompt_key: prompt 类型 ("decompose"/"route"/"system")
            current_prompt: 当前 prompt 文本
            failure_cases: 失败案例（自动从 metrics 获取 if None）

        Returns:
            优化后的 prompt，或 None（失败时返回当前 version）
        """
        if failure_cases is None:
            failure_cases = self.metrics.get_failure_examples(prompt_key)

        if not failure_cases:
            logger.info("[PromptTuner] %s: 无失败案例，跳过优化", prompt_key)
            return None

        fail_summary = "\n".join(
            f"  - 错误: {c.error or '未知'} | 耗时: {c.duration:.1f}s"
            for c in failure_cases[:5]
        )

        system_prompt = (
            "你是一个 Prompt 工程专家。分析以下 prompt 的失败案例，生成改进版本。\n"
            "输出格式：只输出优化后的 prompt 文本，不要任何解释。"
        )
        user_prompt = (
            f"Prompt 类型: {prompt_key}\n"
            f"当前 prompt:\n---\n{current_prompt}\n---\n\n"
            f"失败案例（最近 {len(failure_cases)} 个）:\n"
            f"{fail_summary}\n\n"
            "请分析失败模式并改进 prompt。要求：\n"
            "1. 保持原有功能不变\n"
            "2. 增加对失败模式的针对性约束\n"
            "3. 输出纯文本 prompt，不要代码块"
        )

        try:
            new_prompt = self._call_llm(system_prompt, user_prompt)
            if new_prompt and len(new_prompt) > 50:
                logger.info(
                    "[PromptTuner] %s: 优化成功 (%d→%d chars)",
                    prompt_key, len(current_prompt), len(new_prompt),
                )
                return new_prompt
        except Exception as e:
            logger.warning("[PromptTuner] %s: LLM 调用失败: %s", prompt_key, e)

        return None

    def _call_llm(self, system: str, user: str) -> Optional[str]:
        """调用 DeepSeek API。"""
        import urllib.request
        api_key = self._get_deepseek_key()
        if not api_key:
            return None
        payload = {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            content = re.sub(r"```(?:text|prompt)?\s*", "", content)
            content = re.sub(r"\s*```", "", content)
            return content.strip()

    def _get_deepseek_key(self) -> str:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            return key
        try:
            with open("/root/.env") as f:
                for line in f:
                    ls = line.strip()
                    if ls.startswith("DEEPSEEK_API_KEY") and "=" in ls:
                        return ls.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            pass
        return ""

    # ---- A/B 测试 ----

    def ab_test(
        self, prompt_key: str, version_a: str, version_b: str,
        prompt_a: str, prompt_b: str, sample_size: int = MIN_SAMPLES_FOR_AB,
    ) -> Optional[str]:
        """A/B 分流测试两个 prompt 版本。

        Returns:
            胜出版本号 (v1/v2)，如果打平返回 None
        """
        eval_a = self.metrics.evaluate(prompt_key, version_a)
        eval_b = self.metrics.evaluate(prompt_key, version_b)

        logger.info(
            "[ABTest] %s: A(%s)=%.0f%%(%d次) vs B(%s)=%.0f%%(%d次)",
            prompt_key, version_a, eval_a.success_rate * 100, eval_a.total_calls,
            version_b, eval_b.success_rate * 100, eval_b.total_calls,
        )

        if eval_a.total_calls < sample_size or eval_b.total_calls < sample_size:
            logger.info(
                "[ABTest] %s: 样本不足(%d/%d)，继续收集",
                prompt_key, min(eval_a.total_calls, eval_b.total_calls), sample_size,
            )
            return None

        margin = eval_a.success_rate - eval_b.success_rate
        if abs(margin) < AB_WIN_MARGIN:
            logger.info("[ABTest] %s: 打平(差%.1f%%)", prompt_key, margin * 100)
            return None

        winner = version_a if margin > 0 else version_b
        logger.info(
            "[ABTest] %s: %s 胜出 (%.0f%% vs %.0f%%)",
            prompt_key, winner,
            max(eval_a.success_rate, eval_b.success_rate) * 100,
            min(eval_a.success_rate, eval_b.success_rate) * 100,
        )
        return winner


# ========== 便捷集成的全局实例 ==========

_metrics: Optional[PromptMetrics] = None
_tuner: Optional[PromptTuner] = None


def get_metrics() -> PromptMetrics:
    global _metrics
    if _metrics is None:
        _metrics = PromptMetrics(db_path=OPTIMIZER_DB)
    return _metrics


def record_call(prompt_key: str, version: str, success: bool,
                duration: float = 0.0, error: str = "", **metadata):
    """全局便捷方法：记录 prompt 调用。"""
    get_metrics().record(prompt_key, version, success, duration, error, **metadata)


def should_optimize(prompt_key: str) -> bool:
    """检查是否需要优化特定 prompt。"""
    return get_metrics().needs_optimization(prompt_key)


def run_optimization_cycle(prompt_key: str, current_prompt: str) -> Optional[str]:
    """运行完整优化周期：检查 → 调优 → A/B 部署。

    Returns:
        新 prompt（如果需要更新），或 None
    """
    metrics = get_metrics()
    best = metrics.get_best_version(prompt_key)

    if best and not best.needs_optimization:
        logger.info("[PromptOpt] %s: 当前效果良好 (%.0f%%)，跳过优化",
                     prompt_key, best.success_rate * 100)
        return None

    tuner = PromptTuner(metrics)
    new_prompt = tuner.tune(prompt_key, current_prompt)
    return new_prompt
