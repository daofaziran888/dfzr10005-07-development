"""分区热度画像领域规则测试。

只用标准库（unittest + threading），覆盖：
1. 同批次同分区重复上报只计一次；
2. 两个采样任务并发写同一分区仅首个成功；
3. 历史批次按原口径冻结保留，新画像按新版本重算；
4. 批次交叠冲突按最近收口 / 热度较高裁决；
5. 历史批次、当前画像、目标分层建议三者相互可追溯；
6. 建议提交给数据分层定义后不可重复提交。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import profile_rules as rules
from app.services.profile import (
    BATCH_MODULE,
    CURRENT_MODULE,
    OBSERVATION_MODULE,
    SNAPSHOT_MODULE,
    SUGGESTION_MODULE,
    PartitionProfileService,
)
from app.store import store


class ProfileServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从种子数据重新核算，避免模块级单例串状态
        self.service = PartitionProfileService()
        self.service.reset()

    def _open_batch_id(self) -> int:
        return next(
            int(row["id"])
            for row in store.rows(BATCH_MODULE)
            if row["状态"] == "采样中"
        )

    def _report(self, batch_id: int, table: str, partition: str, task: str, count: int = 10) -> tuple:
        return self.service.report_observation(
            {
                "采样批次id": batch_id,
                "表名称": table,
                "分区名称": partition,
                "访问来源": "在线查询",
                "查询次数": count,
                "采样任务": task,
                "采样时间": "2026-09-15 10:00:00",
            }
        )


class ReportDedupTests(ProfileServiceTestBase):
    def test_duplicate_report_in_same_batch_only_first_succeeds(self) -> None:
        batch_id = self._open_batch_id()
        first, msg1, conflict1 = self._report(batch_id, "dim_region", "dt=2026-09-15", "TASK-A")
        second, msg2, conflict2 = self._report(batch_id, "dim_region", "dt=2026-09-15", "TASK-B")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertTrue(conflict2)
        self.assertIn("重复上报只计一次", msg2)

        accepted = [
            row
            for row in store.rows(OBSERVATION_MODULE)
            if row["采样批次id"] == batch_id
            and row["表名称"] == "dim_region"
            and row["分区名称"] == "dt=2026-09-15"
            and row["状态"] == "已接受"
        ]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["采样任务"], "TASK-A")

    def test_same_partition_in_different_batches_both_accepted(self) -> None:
        # 不同批次对同一分区上报互不影响（批次是采样窗口边界）
        first_batch = self._open_batch_id()
        one, *_ = self._report(first_batch, "dim_region", "dt=2026-09-15", "TASK-A")
        self.assertIsNotNone(one)

        new_batch, problems = self.service.create_batch(
            {
                "批次编号": "PB-202610",
                "采样窗口开始": "2026-10-01",
                "采样窗口结束": "2026-10-31",
                "规则版本": "v2",
            }
        )
        self.assertEqual(problems, [])
        two, *_ = self._report(int(new_batch["id"]), "dim_region", "dt=2026-09-15", "TASK-B")
        self.assertIsNotNone(two)

    def test_closed_batch_rejects_new_report(self) -> None:
        batch_id = self._open_batch_id()
        observation, *_ = self._report(batch_id, "dim_region", "dt=2026-09-15", "TASK-A")
        self.assertIsNotNone(observation)
        self.service.close_batch(batch_id)
        again, message, conflict = self._report(batch_id, "dim_region", "dt=2026-09-16", "TASK-B")
        self.assertIsNone(again)
        self.assertFalse(conflict)
        self.assertIn("已收口", message)


class ConcurrentReportTests(ProfileServiceTestBase):
    def test_concurrent_tasks_only_one_wins_partition(self) -> None:
        batch_id = self._open_batch_id()
        barrier = threading.Barrier(8)
        results: list[tuple] = []
        result_lock = threading.Lock()

        def worker(task: str) -> None:
            barrier.wait()
            outcome = self._report(batch_id, "dim_region", "dt=2026-09-15", task)
            with result_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(f"TASK-{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [item for item in results if item[0] is not None]
        rejected = [item for item in results if item[0] is None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(rejected), 7)
        self.assertTrue(all(item[2] for item in rejected))

        accepted = [
            row
            for row in store.rows(OBSERVATION_MODULE)
            if row["采样批次id"] == batch_id
            and row["分区名称"] == "dt=2026-09-15"
            and row["状态"] == "已接受"
        ]
        self.assertEqual(len(accepted), 1)

    def test_different_partitions_can_be_reported_concurrently(self) -> None:
        batch_id = self._open_batch_id()
        barrier = threading.Barrier(4)
        results: list[tuple] = []
        result_lock = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait()
            outcome = self._report(
                batch_id, "dim_region", f"dt=2026-09-{10 + index:02d}", f"TASK-{index}"
            )
            with result_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for item in results if item[0] is not None), 4)


class BatchPreservationTests(ProfileServiceTestBase):
    def test_historical_batch_keeps_original_rule_version(self) -> None:
        snapshots = store.rows(SNAPSHOT_MODULE)
        v1_snapshots = [row for row in snapshots if row["规则版本"] == "v1"]
        v2_snapshots = [row for row in snapshots if row["规则版本"] == "v2"]
        self.assertTrue(v1_snapshots)
        self.assertTrue(v2_snapshots)

        # v1 来源权重均为 1：热度等于原始查询次数之和
        june = [row for row in v1_snapshots if row["批次编号"] == "PB-202606"]
        order_hot = next(
            row
            for row in june
            if row["表名称"] == "dwd_order_detail" and row["分区名称"] == "dt=2026-08-20"
        )
        self.assertEqual(order_hot["分区热度"], 100.0)  # 80 + 20，未做来源加权

        # v2 对外服务 1.2 权重：110*1 + 20*1.2 = 134
        august = [row for row in v2_snapshots if row["批次编号"] == "PB-202608"]
        order_v2 = next(
            row
            for row in august
            if row["表名称"] == "dwd_order_detail" and row["分区名称"] == "dt=2026-08-20"
        )
        self.assertEqual(order_v2["分区热度"], 134.0)

    def test_closed_batch_cannot_be_recomputed(self) -> None:
        before = len(store.rows(SNAPSHOT_MODULE))
        batch, message = self.service.close_batch(1)
        self.assertIsNone(batch)
        self.assertIn("已收口", message)
        self.assertEqual(len(store.rows(SNAPSHOT_MODULE)), before)

    def test_bootstrap_is_idempotent(self) -> None:
        self.service.bootstrap()
        self.service.bootstrap()
        snapshots = store.rows(SNAPSHOT_MODULE)
        self.assertEqual(len(snapshots), len({(row["批次id"], row["表名称"], row["分区名称"]) for row in snapshots}))


class ConflictResolutionTests(ProfileServiceTestBase):
    def _new_batch(self, code: str, start: str, end: str, version: str = "v2") -> int:
        batch, problems = self.service.create_batch(
            {
                "批次编号": code,
                "采样窗口开始": start,
                "采样窗口结束": end,
                "规则版本": version,
            }
        )
        self.assertEqual(problems, [])
        return int(batch["id"])

    def test_later_closed_batch_overwrites_current_profile(self) -> None:
        batch_id = self._new_batch("PB-202610", "2026-10-01", "2026-10-31")
        self._report(batch_id, "dim_region", "dt=2026-08-20", "TASK-X", count=5)
        self.service.close_batch(batch_id)

        current = next(
            row
            for row in store.rows(CURRENT_MODULE)
            if row["表名称"] == "dim_region" and row["分区名称"] == "dt=2026-08-20"
        )
        self.assertEqual(current["批次编号"], "PB-202610")
        self.assertIn("更晚", current["冲突裁决"])
        # 旧批次快照仍完整保留，可追溯
        old_snapshots = [
            row
            for row in store.rows(SNAPSHOT_MODULE)
            if row["表名称"] == "dim_region"
            and row["分区名称"] == "dt=2026-08-20"
            and row["批次编号"] != "PB-202610"
        ]
        self.assertTrue(old_snapshots)

    def test_earlier_batch_cannot_overwrite_newer_profile(self) -> None:
        # 构造一个收口时间早于当前画像来源批次的批次
        batch_id = self._new_batch("PB-LATE", "2026-05-01", "2026-05-31", version="v1")
        self._report(batch_id, "dim_region", "dt=2026-08-20", "TASK-X", count=200)
        self.service.close_batch(batch_id, closed_at="2026-06-01 08:00:00")

        current = next(
            row
            for row in store.rows(CURRENT_MODULE)
            if row["表名称"] == "dim_region" and row["分区名称"] == "dt=2026-08-20"
        )
        self.assertEqual(current["批次编号"], "PB-202608")

    def test_same_close_time_higher_heat_wins(self) -> None:
        existing = {"收口时间": "2026-09-01 08:00:00", "分区热度": 80.0}
        hotter = {"收口时间": "2026-09-01 08:00:00", "分区热度": 120.0}
        winner, _ = rules.resolve_conflict(hotter, existing)
        self.assertEqual(winner, "candidate")

        cooler = {"收口时间": "2026-09-01 08:00:00", "分区热度": 30.0}
        winner, _ = rules.resolve_conflict(cooler, existing)
        self.assertEqual(winner, "existing")

        tied = {"收口时间": "2026-09-01 08:00:00", "分区热度": 80.0}
        winner, note = rules.resolve_conflict(tied, existing)
        self.assertEqual(winner, "existing")
        self.assertIn("首个", note)


class TraceabilityAndSuggestionTests(ProfileServiceTestBase):
    def test_profile_links_batch_and_suggestion_links_tier(self) -> None:
        profile = next(
            row
            for row in store.rows(CURRENT_MODULE)
            if row["表名称"] == "dwd_order_detail"
        )
        detail = self.service.partition_detail(profile["表名称"], profile["分区名称"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["当前画像"]["批次id"], profile["批次id"])
        self.assertTrue(detail["历史快照"])
        self.assertTrue(detail["采样记录"])
        self.assertTrue(detail["分层建议"])

        # 每条建议都能回溯到具体采样批次
        for suggestion in detail["分层建议"]:
            self.assertTrue(suggestion["来源批次编号"])
            self.assertTrue(
                any(
                    batch["批次编号"] == suggestion["来源批次编号"]
                    for batch in self.service.list_batches()
                )
            )

    def test_submit_suggestion_creates_tier_definition_once(self) -> None:
        suggestion = next(
            row for row in self.service.list_suggestions(status="待处理")
        )
        before = len(store.rows("tier"))
        tier_entry, message = self.service.submit_suggestion_to_tier(int(suggestion["id"]))
        self.assertIsNotNone(tier_entry)
        self.assertIn(suggestion["来源批次编号"], tier_entry["分层状态"])
        self.assertEqual(tier_entry["建议来源批次"], suggestion["来源批次编号"])
        self.assertEqual(len(store.rows("tier")), before + 1)

        # 重复提交被拒绝，台账不会多出定义
        again, repeat_message = self.service.submit_suggestion_to_tier(int(suggestion["id"]))
        self.assertIsNone(again)
        self.assertIn("不可重复提交", repeat_message)
        self.assertEqual(len(store.rows("tier")), before + 1)

    def test_new_batch_marks_old_suggestion_superseded(self) -> None:
        target = next(
            row
            for row in store.rows(SUGGESTION_MODULE)
            if row["表名称"] == "dim_region"
            and row["分区名称"] == "dt=2026-08-20"
            and row["状态"] == "待处理"
        )
        batch, _ = self.service.create_batch(
            {
                "批次编号": "PB-202610",
                "采样窗口开始": "2026-10-01",
                "采样窗口结束": "2026-10-31",
                "规则版本": "v2",
            }
        )
        self._report(int(batch["id"]), "dim_region", "dt=2026-08-20", "TASK-X", count=5)
        self.service.close_batch(int(batch["id"]))

        refreshed = store.find(SUGGESTION_MODULE, int(target["id"]))
        self.assertEqual(refreshed["状态"], "已被新批次取代")


class RulesEngineTests(unittest.TestCase):
    def test_weighted_heat_uses_source_weights(self) -> None:
        self.assertEqual(
            rules.weighted_heat({"在线查询": 10, "对外服务": 10}, "v2"),
            22.0,
        )
        self.assertEqual(
            rules.weighted_heat({"在线查询": 10, "对外服务": 10}, "v1"),
            20.0,
        )

    def test_classify_priority_order(self) -> None:
        hot_history = [
            {"分区热度": 70.0},
            {"分区热度": 80.0},
        ]
        category, _ = rules.classify_partition(hot_history)
        self.assertEqual(category, rules.CATEGORY_HOT)

        spike_history = [{"分区热度": 20.0}, {"分区热度": 120.0}]
        category, _ = rules.classify_partition(spike_history)
        self.assertEqual(category, rules.CATEGORY_SPIKE)

        idle_history = [{"分区热度": 0.0}, {"分区热度": 1.0}, {"分区热度": 2.0}]
        category, _ = rules.classify_partition(idle_history)
        self.assertEqual(category, rules.CATEGORY_IDLE)

        normal_history = [{"分区热度": 30.0}, {"分区热度": 33.0}]
        category, _ = rules.classify_partition(normal_history)
        self.assertEqual(category, rules.CATEGORY_NORMAL)

    def test_aggregate_dedupes_source_counts(self) -> None:
        aggregate = rules.aggregate_partition(
            [
                {"访问来源": "在线查询", "查询次数": 5, "采样时间": "2026-09-01 09:00:00"},
                {"访问来源": "在线查询", "查询次数": 7, "采样时间": "2026-09-02 09:00:00"},
                {"访问来源": "离线任务", "查询次数": 10, "采样时间": "2026-09-02 10:00:00"},
            ],
            "v2",
        )
        self.assertEqual(aggregate["总查询次数"], 22)
        self.assertEqual(aggregate["来源明细"], {"在线查询": 12, "离线任务": 10})
        self.assertEqual(aggregate["分区热度"], round(12 * 1.0 + 10 * 0.6, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
