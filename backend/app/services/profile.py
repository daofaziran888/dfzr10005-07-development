"""分区热度画像服务。

承担三类关键约束：

1. 批次内去重：同一分区在一个采样批次中重复上报只计一次，重复上报返回失败；
2. 并发互斥：两个采样任务并发写同一分区时，全局锁 + 分区占用登记保证只有首个
   结果成功，后到的请求拿到 409 语义的失败结果；
3. 批次裁决与可追溯：已收口批次冻结快照且永不重算（旧批次按原口径保留），批次
   交叠产生的同分区冲突按「最近收口优先，热度较高者次之」裁决，当前画像的每一行
   都带出来源批次，与历史快照、目标分层建议互相可追溯。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services import profile_rules as rules
from app.store import store

BATCH_MODULE = "profile_batch"
OBSERVATION_MODULE = "profile_observation"
SNAPSHOT_MODULE = "profile_snapshot"
CURRENT_MODULE = "profile_current"
SUGGESTION_MODULE = "tier_suggestion"

PROFILE_TABLES = [
    BATCH_MODULE,
    OBSERVATION_MODULE,
    SNAPSHOT_MODULE,
    CURRENT_MODULE,
    SUGGESTION_MODULE,
]

REQUIRED_OBSERVATION_FIELDS = ["采样批次id", "表名称", "分区名称", "访问来源", "查询次数"]


class PartitionProfileService:
    """画像领域服务。所有写操作共用一把可重入锁，保证判定-写入的原子性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 采样中批次的分区占用登记：(批次id, 表名称, 分区名称) -> 采样任务
        self._partition_claims: dict[tuple[int, str, str], str] = {}
        self._bootstrapped = False

    # ------------------------------------------------------------------ 基础工具
    def _next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1

    def _find_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    @staticmethod
    def _partition_key(observation: dict[str, Any]) -> tuple[int, str, str]:
        return (
            int(observation["采样批次id"]),
            str(observation["表名称"]),
            str(observation["分区名称"]),
        )

    # ------------------------------------------------------------------ 启动核算
    def bootstrap(self, *, force: bool = False) -> None:
        """按种子批次补齐快照、当前画像与分层建议。

        幂等：已存在快照的批次直接跳过，保证重启服务不会重复生成画像；
        ``force=True`` 时清空衍生表重新核算（测试使用）。
        """
        with self._lock:
            if self._bootstrapped and not force:
                return
            if force:
                for module in (SNAPSHOT_MODULE, CURRENT_MODULE, SUGGESTION_MODULE):
                    store.rows(module).clear()
            closed_batches = sorted(
                (row for row in store.rows(BATCH_MODULE) if row.get("状态") == "已收口"),
                key=lambda row: str(row.get("收口时间") or ""),
            )
            existing_snapshot_batches = {
                int(row["批次id"]) for row in store.rows(SNAPSHOT_MODULE)
            }
            for batch in closed_batches:
                if int(batch["id"]) not in existing_snapshot_batches:
                    self._close_batch_locked(batch)
            self._bootstrapped = True

    # ------------------------------------------------------------------ 批次查询
    def list_batches(self, status: str | None = None) -> list[dict[str, Any]]:
        self.bootstrap()
        rows = store.rows(BATCH_MODULE)
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return sorted(rows, key=lambda row: int(row["id"]))

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        self.bootstrap()
        return self._find_batch(batch_id)

    def create_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["批次编号", "采样窗口开始", "采样窗口结束", "规则版本"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(BATCH_MODULE)
            code = str(values["批次编号"]).strip()
            if any(str(row.get("批次编号")) == code for row in rows):
                return None, [f"批次编号 {code} 已存在"]
            rule_version = str(values["规则版本"]).strip()
            if rule_version not in rules.SOURCE_WEIGHTS:
                return None, [f"规则版本仅支持 {sorted(rules.SOURCE_WEIGHTS)}"]
            batch = {
                "id": self._next_id(BATCH_MODULE),
                "批次编号": code,
                "采样窗口开始": str(values["采样窗口开始"]).strip(),
                "采样窗口结束": str(values["采样窗口结束"]).strip(),
                "规则版本": rule_version,
                "状态": "采样中",
                "创建时间": str(values.get("创建时间") or values["采样窗口开始"]).strip(),
                "收口时间": "",
            }
            rows.append(batch)
            return dict(batch), []

    # ------------------------------------------------------------------ 采样上报
    def report_observation(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """登记一条分区采样点。

        返回 ``(记录或None, 说明, 是否因并发/重复被拒)``；被拒时 ``ok=False``，
        调用方按冲突语义响应，且不写入任何数据（重复上报只生效一次）。
        """
        self.bootstrap()
        missing = [
            field
            for field in REQUIRED_OBSERVATION_FIELDS
            if field not in values or str(values.get(field) or "").strip() == ""
        ]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}", False
        try:
            batch_id = int(values["采样批次id"])
            query_count = int(values["查询次数"])
        except (TypeError, ValueError):
            return None, "采样批次id与查询次数必须为整数", False
        if query_count < 0:
            return None, "查询次数不能为负数", False

        with self._lock:
            batch = self._find_batch(batch_id)
            if batch is None:
                return None, f"采样批次 {batch_id} 不存在", False
            if batch.get("状态") != "采样中":
                return None, f"批次 {batch['批次编号']} 已收口，不再接收采样上报", False

            key = (
                batch_id,
                str(values["表名称"]).strip(),
                str(values["分区名称"]).strip(),
            )
            # 同批次同分区重复上报：含已接受记录或占用登记，均只允许首个成功
            accepted = [
                row
                for row in store.rows(OBSERVATION_MODULE)
                if self._partition_key(row) == key and row.get("状态") == "已接受"
            ]
            task_name = str(values.get("采样任务") or "未命名采样任务").strip()
            if accepted:
                return (
                    None,
                    f"分区 {key[1]}/{key[2]} 在批次 {batch['批次编号']} 已由采样任务"
                    f"「{accepted[0].get('采样任务')}」上报，重复上报只计一次",
                    True,
                )
            if key in self._partition_claims:
                return (
                    None,
                    f"分区 {key[1]}/{key[2]} 正被采样任务「{self._partition_claims[key]}」"
                    "占用，并发申请仅首个结果成功",
                    True,
                )

            # 首个申请者登记占用并落库
            self._partition_claims[key] = task_name
            observation = {
                "id": self._next_id(OBSERVATION_MODULE),
                "采样批次id": batch_id,
                "批次编号": batch["批次编号"],
                "表名称": key[1],
                "分区名称": key[2],
                "访问来源": str(values["访问来源"]).strip(),
                "查询次数": query_count,
                "分区热度": "",
                "采样时间": str(values.get("采样时间") or batch["采样窗口开始"]).strip(),
                "采样任务": task_name,
                "状态": "已接受",
            }
            store.rows(OBSERVATION_MODULE).append(observation)
            return dict(observation), "采样上报成功，分区画像占位已锁定", False

    def release_claim(self, batch_id: int, table_name: str, partition_name: str) -> bool:
        """释放采样中的分区占用（异常补偿用）；当前流程上报即落库，默认不释放。"""
        with self._lock:
            return self._partition_claims.pop((batch_id, table_name, partition_name), None) is not None

    # ------------------------------------------------------------------ 批次收口
    def close_batch(
        self, batch_id: int, closed_at: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        self.bootstrap()
        with self._lock:
            batch = self._find_batch(batch_id)
            if batch is None:
                return None, f"采样批次 {batch_id} 不存在"
            if batch.get("状态") == "已收口":
                return None, f"批次 {batch['批次编号']} 已收口，历史批次按原口径保留不可重算"
            return self._close_batch_locked(batch, closed_at)

    def _close_batch_locked(
        self, batch: dict[str, Any], closed_at: str | None = None
    ) -> tuple[dict[str, Any], str]:
        batch_id = int(batch["id"])
        batch["状态"] = "已收口"
        batch["收口时间"] = closed_at or f"{batch['采样窗口结束']} 08:00:00"
        rule_version = str(batch["规则版本"])

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for observation in store.rows(OBSERVATION_MODULE):
            if int(observation.get("采样批次id", 0)) != batch_id:
                continue
            if observation.get("状态") != "已接受":
                continue
            key = (str(observation["表名称"]), str(observation["分区名称"]))
            grouped.setdefault(key, []).append(observation)

        snapshot_rows = store.rows(SNAPSHOT_MODULE)
        current_rows = store.rows(CURRENT_MODULE)
        suggestion_rows = store.rows(SUGGESTION_MODULE)
        conflicts: list[dict[str, Any]] = []

        for (table_name, partition_name), observations in sorted(grouped.items()):
            aggregate = rules.aggregate_partition(observations, rule_version)
            for observation in observations:
                observation["分区热度"] = aggregate["分区热度"]
            # 历史序列取此前已收口快照，并把当前批次的聚合值作为末位一起参与判定
            history = self._partition_history(table_name, partition_name)
            history.append({"分区热度": aggregate["分区热度"], "收口时间": batch["收口时间"]})
            category, reason = rules.classify_partition(history)
            tier_action = rules.TIER_ACTIONS[category]
            snapshot = {
                "id": self._next_id(SNAPSHOT_MODULE),
                "批次id": batch_id,
                "批次编号": batch["批次编号"],
                "规则版本": rule_version,
                "表名称": table_name,
                "分区名称": partition_name,
                "总查询次数": aggregate["总查询次数"],
                "分区热度": aggregate["分区热度"],
                "来源明细": aggregate["来源明细"],
                "采样时刻": aggregate["采样时刻"],
                "峰值采样点": aggregate["峰值采样点"],
                "画像分类": category,
                "分类依据": reason,
                "收口时间": batch["收口时间"],
            }
            snapshot_rows.append(snapshot)

            # 批次交叠：与当前画像比较，按最近收口/热度较高裁决
            current = next(
                (
                    row
                    for row in current_rows
                    if row.get("表名称") == table_name and row.get("分区名称") == partition_name
                ),
                None,
            )
            conflict_note = "首次建立画像"
            if current is not None:
                winner, note = rules.resolve_conflict(snapshot, current)
                conflict_note = note
                conflicts.append(
                    {
                        "表名称": table_name,
                        "分区名称": partition_name,
                        "新批次": batch["批次编号"],
                        "保留批次": batch["批次编号"] if winner == "candidate" else current["批次编号"],
                        "裁决说明": note,
                    }
                )
                if winner == "existing":
                    continue
            profile_row = {
                "id": current["id"] if current else self._next_id(CURRENT_MODULE),
                "表名称": table_name,
                "分区名称": partition_name,
                "批次id": batch_id,
                "批次编号": batch["批次编号"],
                "规则版本": rule_version,
                "总查询次数": aggregate["总查询次数"],
                "分区热度": aggregate["分区热度"],
                "画像分类": category,
                "分类依据": reason,
                "来源明细": aggregate["来源明细"],
                "峰值采样点": aggregate["峰值采样点"],
                "收口时间": batch["收口时间"],
                "冲突裁决": conflict_note,
            }
            if current is None:
                current_rows.append(profile_row)
            else:
                current_rows[current_rows.index(current)] = profile_row

            self._upsert_suggestion(suggestion_rows, profile_row, tier_action)

        # 释放本批次的全部分区占用
        for key in [key for key in self._partition_claims if key[0] == batch_id]:
            self._partition_claims.pop(key, None)

        message = f"批次 {batch['批次编号']} 已收口，冻结 {len(grouped)} 个分区画像"
        if conflicts:
            message += f"，处理 {len(conflicts)} 处批次交叠冲突"
        return dict(batch), message

    def _partition_history(
        self, table_name: str, partition_name: str
    ) -> list[dict[str, Any]]:
        """取同一分区在当前批次之前（含当前批次已聚合值）的收口快照序列。"""
        history = [
            row
            for row in store.rows(SNAPSHOT_MODULE)
            if row.get("表名称") == table_name and row.get("分区名称") == partition_name
        ]
        return sorted(history, key=lambda row: str(row.get("收口时间") or ""))

    def _upsert_suggestion(
        self,
        suggestion_rows: list[dict[str, Any]],
        profile_row: dict[str, Any],
        tier_action: dict[str, str],
    ) -> None:
        # 同分区旧的待处理建议标记为被取代，保留完整来源链路
        for old in suggestion_rows:
            if (
                old.get("表名称") == profile_row["表名称"]
                and old.get("分区名称") == profile_row["分区名称"]
                and old.get("状态") == "待处理"
            ):
                old["状态"] = "已被新批次取代"
        suggestion_rows.append(
            {
                "id": self._next_id(SUGGESTION_MODULE),
                "建议编号": f"TAS-{int(profile_row['批次id']):04d}-{profile_row['分区名称'].replace('=', '').replace('/', '')}",
                "表名称": profile_row["表名称"],
                "分区名称": profile_row["分区名称"],
                "画像分类": profile_row["画像分类"],
                "当前热度": profile_row["分区热度"],
                "建议动作": tier_action["建议动作"],
                "建议目标分层": tier_action["建议目标分层"],
                "来源批次id": profile_row["批次id"],
                "来源批次编号": profile_row["批次编号"],
                "状态": "待处理",
                "分层定义id": "",
                "生成时间": profile_row["收口时间"],
            }
        )

    # ------------------------------------------------------------------ 画像视图
    def current_profiles(
        self, category: str | None = None, table_keyword: str | None = None
    ) -> list[dict[str, Any]]:
        self.bootstrap()
        rows = store.rows(CURRENT_MODULE)
        if category:
            rows = [row for row in rows if row.get("画像分类") == category]
        if table_keyword:
            rows = [
                row
                for row in rows
                if table_keyword in str(row.get("表名称", ""))
                or table_keyword in str(row.get("分区名称", ""))
            ]
        order = {name: index for index, name in enumerate(rules.PROFILE_CATEGORIES)}
        return sorted(
            rows,
            key=lambda row: (order.get(str(row.get("画像分类")), 99), -float(row.get("分区热度") or 0)),
        )

    def timeline(self) -> list[dict[str, Any]]:
        """按采样窗口（批次）分组的画像视图。"""
        self.bootstrap()
        result: list[dict[str, Any]] = []
        for batch in self.list_batches():
            snapshots = [
                row
                for row in store.rows(SNAPSHOT_MODULE)
                if int(row.get("批次id", 0)) == int(batch["id"])
            ]
            category_count = {name: 0 for name in rules.PROFILE_CATEGORIES}
            for snapshot in snapshots:
                category_count[str(snapshot["画像分类"])] = (
                    category_count.get(str(snapshot["画像分类"]), 0) + 1
                )
            result.append(
                {
                    "批次id": batch["id"],
                    "批次编号": batch["批次编号"],
                    "采样窗口开始": batch["采样窗口开始"],
                    "采样窗口结束": batch["采样窗口结束"],
                    "规则版本": batch["规则版本"],
                    "状态": batch["状态"],
                    "收口时间": batch["收口时间"],
                    "分区数": len(snapshots),
                    "分类统计": category_count,
                    "快照": sorted(snapshots, key=lambda row: str(row.get("表名称", ""))),
                }
            )
        return result

    def partition_detail(
        self, table_name: str, partition_name: str
    ) -> dict[str, Any] | None:
        """单分区画像明细：当前画像 + 历史批次快照 + 采样点 + 分层建议。"""
        self.bootstrap()
        current = next(
            (
                row
                for row in store.rows(CURRENT_MODULE)
                if row.get("表名称") == table_name and row.get("分区名称") == partition_name
            ),
            None,
        )
        if current is None:
            return None
        snapshots = self._partition_history(table_name, partition_name)
        observations = [
            row
            for row in store.rows(OBSERVATION_MODULE)
            if row.get("表名称") == table_name and row.get("分区名称") == partition_name
        ]
        suggestions = [
            row
            for row in store.rows(SUGGESTION_MODULE)
            if row.get("表名称") == table_name and row.get("分区名称") == partition_name
        ]
        return {
            "当前画像": current,
            "历史快照": snapshots,
            "采样记录": sorted(observations, key=lambda row: (int(row["采样批次id"]), str(row["采样时间"]))),
            "分层建议": sorted(suggestions, key=lambda row: -int(row["id"])),
        }

    # ------------------------------------------------------------------ 分层建议
    def list_suggestions(self, status: str | None = None) -> list[dict[str, Any]]:
        self.bootstrap()
        rows = store.rows(SUGGESTION_MODULE)
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return sorted(rows, key=lambda row: -int(row["id"]))

    def submit_suggestion_to_tier(
        self, suggestion_id: int
    ) -> tuple[dict[str, Any] | None, str]:
        """把可执行的分层调整建议提交给数据分层定义台账。"""
        self.bootstrap()
        with self._lock:
            suggestion = store.find(SUGGESTION_MODULE, suggestion_id)
            if suggestion is None:
                return None, f"分层建议 {suggestion_id} 不存在"
            if suggestion.get("状态") != "待处理":
                return None, f"建议 {suggestion['建议编号']} 状态为{suggestion['状态']}，不可重复提交"
            tier_rows = store.rows("tier")
            tier_entry = {
                "id": max((int(row.get("id", 0)) for row in tier_rows), default=0) + 1,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "分层编号": f"TIER-P{suggestion_id:04d}",
                "分层名称": f"{suggestion['表名称']}/{suggestion['分区名称']} 画像驱动分层调整",
                "层级别名": suggestion["建议目标分层"],
                "数据粒度": "分区级",
                "存储格式": "按画像建议选型",
                "压缩策略": suggestion["建议动作"],
                "访问模式": suggestion["画像分类"],
                "分层状态": "待评审（来源批次：%s）" % suggestion["来源批次编号"],
                "建议来源批次": suggestion["来源批次编号"],
                "来源建议编号": suggestion["建议编号"],
            }
            tier_rows.append(tier_entry)
            suggestion["状态"] = "已提交分层定义"
            suggestion["分层定义id"] = tier_entry["id"]
            return dict(tier_entry), f"建议已提交分层定义 {tier_entry['分层编号']}，来源批次 {suggestion['来源批次编号']}"

    # ------------------------------------------------------------------ 成本摘要
    def profile_summary(self) -> dict[str, Any]:
        """成本分析报告清单同步展示的画像摘要。"""
        self.bootstrap()
        profiles = store.rows(CURRENT_MODULE)
        category_count = {name: 0 for name in rules.PROFILE_CATEGORIES}
        for row in profiles:
            category_count[str(row["画像分类"])] = category_count.get(str(row["画像分类"]), 0) + 1
        batches = store.rows(BATCH_MODULE)
        pending_suggestions = sum(
            1 for row in store.rows(SUGGESTION_MODULE) if row.get("状态") == "待处理"
        )
        return {
            "最新批次编号": next(
                (row["批次编号"] for row in sorted(batches, key=lambda item: -int(item["id"]))),
                "",
            ),
            "已收口批次数": sum(1 for row in batches if row.get("状态") == "已收口"),
            "采样中批次数": sum(1 for row in batches if row.get("状态") == "采样中"),
            "画像分区总数": len(profiles),
            "分类统计": category_count,
            "待处理分层建议": pending_suggestions,
        }

    def reset(self) -> None:
        """测试辅助：清空画像全部台账并释放占用，再按种子重新核算。"""
        with self._lock:
            for module in PROFILE_TABLES:
                store.rows(module).clear()
            self._partition_claims.clear()
            self._bootstrapped = False
            # 重新载入种子中的批次与采样点
            from app.seed import _build_profile_seed

            seed = _build_profile_seed()
            store.rows(BATCH_MODULE).extend(dict(row) for row in seed[BATCH_MODULE])
            store.rows(OBSERVATION_MODULE).extend(dict(row) for row in seed[OBSERVATION_MODULE])
            self.bootstrap(force=True)


profile_service = PartitionProfileService()
