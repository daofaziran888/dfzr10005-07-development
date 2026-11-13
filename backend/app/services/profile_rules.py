"""分区热度画像规则引擎（纯函数，便于单测）。

口径分两个规则版本，保证历史批次可按原口径保留、新画像按新版本重算：

* ``v1``（历史口径）：只看总查询次数，所有访问来源权重相同；
* ``v2``（现行口径）：按访问来源加权计算分区热度，并引入持续高访问 / 短时峰值 /
  长期无访问三类判定。

采样窗口以「采样批次」为单位：一个批次对应一个固定采样窗口（通常为一个自然月），
窗口内同一分区可被多个采样任务、多个访问来源重复采样，收口时冻结成画像快照。
"""
from __future__ import annotations

from typing import Any

# 访问来源权重：v2 区分在线/离线/对外服务的成本与实时性差异
SOURCE_WEIGHTS: dict[str, dict[str, float]] = {
    "v1": {"在线查询": 1.0, "离线任务": 1.0, "对外服务": 1.0},
    "v2": {"在线查询": 1.0, "离线任务": 0.6, "对外服务": 1.2},
}
DEFAULT_SOURCE_WEIGHT = 1.0

# 单窗口加权热度达到该值即视为高访问分区
HIGH_HEAT_THRESHOLD = 60.0
# 短时峰值：当前窗口热度绝对值门槛
PEAK_HEAT_THRESHOLD = 100.0
# 短时峰值：相对历史窗口均值的倍数门槛
PEAK_RATIO = 2.0
# 长期无访问：最近若干个已收口窗口热度均低于该值
IDLE_HEAT_THRESHOLD = 5.0
IDLE_HISTORY_BATCHES = 3
# 持续高访问：最近连续若干个已收口窗口均达到高访问门槛
HOT_CONSECUTIVE_WINDOWS = 2

CATEGORY_HOT = "持续高访问"
CATEGORY_SPIKE = "短时峰值"
CATEGORY_IDLE = "长期无访问"
CATEGORY_NORMAL = "平稳波动"
PROFILE_CATEGORIES = [CATEGORY_HOT, CATEGORY_SPIKE, CATEGORY_IDLE, CATEGORY_NORMAL]

# 分类 -> 可执行的分层调整建议
TIER_ACTIONS: dict[str, dict[str, str]] = {
    CATEGORY_HOT: {
        "建议动作": "上调至热数据层，启用SSD/内存加速",
        "建议目标分层": "热数据层",
    },
    CATEGORY_SPIKE: {
        "建议动作": "保留当前分层，配置峰值弹性缓存",
        "建议目标分层": "温数据层（弹性缓存）",
    },
    CATEGORY_IDLE: {
        "建议动作": "下调至冷存储并纳入归档压缩范围",
        "建议目标分层": "冷数据归档层",
    },
    CATEGORY_NORMAL: {
        "建议动作": "维持当前分层，按常规周期复评",
        "建议目标分层": "温数据层",
    },
}


def weighted_heat(source_counts: dict[str, int], rule_version: str) -> float:
    """按来源权重把各访问来源的查询次数折算成加权分区热度。"""
    weights = SOURCE_WEIGHTS.get(rule_version, SOURCE_WEIGHTS["v2"])
    return round(
        sum(
            float(count) * weights.get(source, DEFAULT_SOURCE_WEIGHT)
            for source, count in source_counts.items()
        ),
        2,
    )


def aggregate_partition(observations: list[dict[str, Any]], rule_version: str) -> dict[str, Any]:
    """把一个批次内同一分区的全部采样点聚合成一条画像输入。

    同一分区在同一批次内只会有一条被接受的采样记录（重复上报在服务层拦截），
    但一条记录里可能包含多个来源、多个采样时刻，这里负责汇总。
    """
    source_counts: dict[str, int] = {}
    sample_times: list[str] = []
    total_queries = 0
    peak_point = ""
    peak_count = -1
    for observation in observations:
        source = str(observation.get("访问来源") or "未知来源")
        count = int(observation.get("查询次数") or 0)
        source_counts[source] = source_counts.get(source, 0) + count
        total_queries += count
        sample_time = str(observation.get("采样时间") or "")
        if sample_time:
            sample_times.append(sample_time)
        if count > peak_count:
            peak_count = count
            peak_point = f"{sample_time} {source} 查询{count}次"
    return {
        "总查询次数": total_queries,
        "分区热度": weighted_heat(source_counts, rule_version),
        "来源明细": dict(sorted(source_counts.items())),
        "采样时刻": sorted(sample_times),
        "峰值采样点": peak_point,
    }


def classify_partition(history: list[dict[str, Any]]) -> tuple[str, str]:
    """依据同一分区按收口时间排序的历史快照（末位为当前窗口）判定画像分类。

    判定优先级：持续高访问 > 短时峰值 > 长期无访问 > 平稳波动。
    """
    if not history:
        return CATEGORY_NORMAL, "无历史采样，按平稳波动兜底"
    current = history[-1]
    current_heat = float(current.get("分区热度") or 0.0)
    prior_heats = [float(item.get("分区热度") or 0.0) for item in history[:-1]]

    recent = history[-HOT_CONSECUTIVE_WINDOWS:]
    if len(recent) >= HOT_CONSECUTIVE_WINDOWS and all(
        float(item.get("分区热度") or 0.0) >= HIGH_HEAT_THRESHOLD for item in recent
    ):
        return (
            CATEGORY_HOT,
            f"最近{HOT_CONSECUTIVE_WINDOWS}个收口窗口加权热度均≥{HIGH_HEAT_THRESHOLD:g}",
        )

    if current_heat >= PEAK_HEAT_THRESHOLD:
        if prior_heats:
            baseline = sum(prior_heats) / len(prior_heats)
            if baseline <= 0 or current_heat >= PEAK_RATIO * baseline:
                return (
                    CATEGORY_SPIKE,
                    f"当前热度{current_heat:g}≥{PEAK_HEAT_THRESHOLD:g}且达到历史均值"
                    f"{baseline:.2f}的{PEAK_RATIO:g}倍",
                )
        else:
            return CATEGORY_SPIKE, f"当前热度{current_heat:g}≥{PEAK_HEAT_THRESHOLD:g}且无历史基线"

    idle_window = history[-IDLE_HISTORY_BATCHES:]
    if len(idle_window) >= min(IDLE_HISTORY_BATCHES, len(history)) and all(
        float(item.get("分区热度") or 0.0) < IDLE_HEAT_THRESHOLD for item in idle_window
    ):
        return (
            CATEGORY_IDLE,
            f"最近{len(idle_window)}个收口窗口加权热度均<{IDLE_HEAT_THRESHOLD:g}",
        )

    return CATEGORY_NORMAL, f"当前热度{current_heat:g}，未触发高访问/峰值/无访问门槛"


def resolve_conflict(
    candidate: dict[str, Any], existing: dict[str, Any]
) -> tuple[str, str]:
    """两个已收口批次对同一分区画像冲突时的裁决规则。

    先比收口时间：最近一次已收口采样为准；收口时间相同时，分区热度较高者为准；
    热度也相同则保留先入库者（首个结果成功）。

    返回 ``(胜出方, 裁决说明)``，胜出方取值 ``candidate`` / ``existing``。
    """
    candidate_closed = str(candidate.get("收口时间") or "")
    existing_closed = str(existing.get("收口时间") or "")
    if candidate_closed > existing_closed:
        return "candidate", f"候选批次收口时间{candidate_closed}更晚，覆盖旧画像"
    if candidate_closed < existing_closed:
        return "existing", f"已保留批次收口时间{existing_closed}更晚，旧画像继续生效"
    candidate_heat = float(candidate.get("分区热度") or 0.0)
    existing_heat = float(existing.get("分区热度") or 0.0)
    if candidate_heat > existing_heat:
        return "candidate", "收口时间相同，候选批次分区热度更高"
    if candidate_heat < existing_heat:
        return "existing", "收口时间相同，已保留批次分区热度更高"
    return "existing", "收口时间与热度均相同，保留首个已收口结果"
