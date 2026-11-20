"""库存时点跨表查询接口。

覆盖：门店/商品/调整/批次基础数据、带有效区间调整与盘点批次的幂等登记、
按门店日界的时点库存查询、数据集市聚合（保留来源区间）、末页商品位置回查。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.inventory_timeline import (
    VALID_VIEWS,
    VIEW_AS_AT,
    service,
)

router = APIRouter(prefix="/api/inventory_timeline", tags=["库存时点查询"])


def _validate_view(view: str) -> str:
    if view not in VALID_VIEWS:
        raise HTTPException(status_code=400, detail=f"视角仅支持 {VALID_VIEWS}")
    return view


@router.get("/stores")
def stores() -> dict[str, Any]:
    """门店清单（含时区与日切小时）。"""
    return {"items": service.list_stores(), "total": len(service.list_stores())}


@router.get("/products")
def products() -> dict[str, Any]:
    """商品清单。"""
    rows = service.list_products()
    return {"items": rows, "total": len(rows)}


@router.get("/store_products")
def store_products(store_id: str | None = None) -> dict[str, Any]:
    """门店商品清单（含期初库存），可按门店过滤。"""
    rows = service.list_store_products(store_id)
    return {"items": rows, "total": len(rows)}


@router.get("/adjustments")
def adjustments(store_id: str | None = None, sku: str | None = None) -> dict[str, Any]:
    """库存调整清单（含业务时点、系统收到时刻与有效区间）。"""
    rows = service.list_adjustments(store_id, sku)
    return {"items": rows, "total": len(rows)}


@router.get("/batches")
def batches() -> dict[str, Any]:
    """盘点批次清单（含重复提交去重状态）。"""
    rows = service.list_batches()
    return {"items": rows, "total": len(rows)}


@router.get("/intervals")
def intervals(store_id: str = Query(..., description="门店编号"),
              sku: str = Query(..., description="商品编号")) -> dict[str, Any]:
    """物化后的有效区间：跨午夜调整按门店营业日界拆成半开子区间。"""
    pieces = service.materialize_intervals(store_id, sku)
    return {"门店编号": store_id, "商品编号": sku, "items": pieces, "total": len(pieces)}


@router.get("/point")
def point_query(
    store_id: str | None = Query(default=None, description="门店编号，缺省查全部门店"),
    sku: str | None = Query(default=None, description="商品编号，缺省查门店全部商品"),
    point: str | None = Query(default=None, description="查询时点（门店本地时间）"),
    biz_day: str | None = Query(default=None, description="营业日 YYYY-MM-DD，取门店日界为时点"),
    view: str = Query(default=VIEW_AS_AT, description="as_at=复盘视角 / current=实际视角"),
) -> dict[str, Any]:
    """跨表时点查询：按门店日界定位每个查询时点的有效库存。"""
    _validate_view(view)
    if not point and not biz_day:
        raise HTTPException(status_code=400, detail="必须提供查询时点 point 或营业日 biz_day")
    try:
        return service.point_query(store_id=store_id, sku=sku, point=point,
                                   biz_day=biz_day, view=view)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mart")
def mart(
    store_id: str | None = Query(default=None, description="按门店筛选"),
    biz_day: str | None = Query(default=None, description="按营业日筛选"),
    point: str | None = Query(default=None, description="按查询时点筛选"),
    view: str = Query(default=VIEW_AS_AT),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """数据集市：按门店与商品汇总可用库存、缺记录商品数，每行保留来源区间。"""
    _validate_view(view)
    if not point and not biz_day:
        raise HTTPException(status_code=400, detail="必须提供查询时点 point 或营业日 biz_day")
    try:
        return service.mart(store_id=store_id, biz_day=biz_day, point=point,
                            view=view, page=page, size=size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/locate")
def locate(
    store_id: str = Query(..., description="目标门店编号"),
    sku: str = Query(..., description="目标商品编号"),
    biz_day: str | None = Query(default=None),
    point: str | None = Query(default=None),
    view: str = Query(default=VIEW_AS_AT),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """末页回查：定位指定商品在集市分页中的页号、页内位置与汇总贡献。"""
    _validate_view(view)
    if not point and not biz_day:
        raise HTTPException(status_code=400, detail="必须提供查询时点 point 或营业日 biz_day")
    try:
        return service.locate_product(store_id=store_id, sku=sku, biz_day=biz_day,
                                      point=point, view=view, size=size)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/count_batches", response_model=ActionResult)
def submit_count_batch(payload: EntryPayload) -> ActionResult:
    """盘点批次提交：同批次编号重复提交只产生一次库存调整（幂等去重）。"""
    entry, missing, message = service.submit_count_batch(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=message)
    duplicated = message.startswith("批次") and "重复提交" in message
    return ActionResult(ok=not duplicated, message=message, entry=entry)


@router.post("/adjustments", response_model=ActionResult)
def submit_adjustment(payload: EntryPayload) -> ActionResult:
    """登记一条带有效区间的库存调整；带批次编号时同样按批次幂等。"""
    entry, missing, message = service.submit_adjustment(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=message)
    duplicated = "重复提交已忽略" in message
    return ActionResult(ok=not duplicated, message=message, entry=entry)
