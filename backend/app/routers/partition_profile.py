"""分区热度画像接口。

覆盖采样批次管理、采样上报（含并发/重复冲突）、批次收口、按时间分组的画像视图、
分区明细、分层调整建议提交，以及同步给成本分析报告的画像摘要。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas import ActionResult, EntryPayload
from app.services.profile import profile_service

router = APIRouter(prefix="/api/partition-profile", tags=["分区热度画像"])


@router.get("/batches")
def list_batches(status: str | None = None) -> dict:
    """采样批次台账：历史批次按原口径保留，可逐批追溯。"""
    return {"items": profile_service.list_batches(status=status), "total": len(profile_service.list_batches(status=status))}


@router.post("/batches", response_model=ActionResult)
def create_batch(payload: EntryPayload) -> ActionResult:
    """新建采样批次（一个批次即一个采样窗口）。"""
    batch, problem = profile_service.create_batch(payload.values)
    if batch is None:
        return ActionResult(ok=False, message="；".join(problem))
    return ActionResult(ok=True, message=f"采样批次 {batch['批次编号']} 已创建，进入采样中", entry=batch)


@router.post("/batches/{batch_id}/close", response_model=ActionResult)
def close_batch(batch_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """收口批次：冻结分区快照、重算当前画像并生成分层调整建议。"""
    closed_at = ""
    if payload is not None:
        closed_at = str(payload.values.get("收口时间") or "").strip()
    batch, message = profile_service.close_batch(batch_id, closed_at or None)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/observations", response_model=ActionResult)
def report_observation(payload: EntryPayload) -> ActionResult:
    """采样任务上报分区访问记录。

    同一分区在同一批次重复上报、或两个采样任务并发申请同一分区时，仅首个结果
    成功；冲突时返回 ok=False 并说明占用方，不写入重复数据。
    """
    observation, message, conflict = profile_service.report_observation(payload.values)
    if observation is None:
        result = ActionResult(ok=False, message=message)
        # 冲突语义通过 entry 标记，前端可据此提示并发/重复原因
        result.entry = {"conflict": conflict}
        return result
    return ActionResult(ok=True, message=message, entry=observation)


@router.get("/profiles")
def current_profiles(category: str | None = None, keyword: str | None = None) -> dict:
    """当前分区热度画像（每行标注来源批次，可与历史快照互相追溯）。"""
    items = profile_service.current_profiles(category=category, table_keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/timeline")
def profile_timeline() -> dict:
    """按采样时间（批次窗口）分组的画像视图，含三类分区标注。"""
    items = profile_service.timeline()
    return {"items": items, "total": len(items)}


@router.get("/partitions/{table_name}/{partition_name}")
def partition_detail(table_name: str, partition_name: str) -> dict:
    """分区明细：当前画像、历史批次快照、采样记录与分层建议全链路。"""
    detail = profile_service.partition_detail(table_name, partition_name)
    if detail is None:
        return {"ok": False, "message": f"分区 {table_name}/{partition_name} 暂无画像"}
    return {"ok": True, "detail": detail}


@router.get("/suggestions")
def list_suggestions(status: str | None = None) -> dict:
    """分层调整建议清单（分层定义页展示建议来源批次）。"""
    items = profile_service.list_suggestions(status=status)
    return {"items": items, "total": len(items)}


@router.post("/suggestions/{suggestion_id}/submit", response_model=ActionResult)
def submit_suggestion(suggestion_id: int) -> ActionResult:
    """把可执行的分层调整建议提交给数据分层定义。"""
    tier_entry, message = profile_service.submit_suggestion_to_tier(suggestion_id)
    if tier_entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=tier_entry)


@router.get("/summary")
def profile_summary() -> dict:
    """供成本分析报告清单同步显示的画像摘要。"""
    return profile_service.profile_summary()
