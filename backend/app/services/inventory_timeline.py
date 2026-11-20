"""库存时点跨表查询：按门店日界定位每个查询时点的有效库存。

口径要点（与需求逐条对应）：

1. 所有库存调整都带「有效区间」[有效起, 有效止)，采用半开区间，区间相邻但不重叠；
   跨午夜的调整在物化到营业日区间时被拆成两段，两段共用同一调整编号，
   因此同一条调整不会同时落在两个相邻有效区间里被重复计入。
2. 每条调整同时保留两个时刻：业务时点（业务实际发生）与系统收到时刻（补录到达），
   ``view=as_at`` 按系统收到时刻定位（复盘当时能看到什么），
   ``view=current`` 按业务时点定位（按业务事实计算实际库存），两条时间轴可分别检索。
3. 盘点批次重复提交按批次编号幂等去重，同一批次只产生一次库存调整，重复提交直接被忽略。
4. 门店营业日按各自时区 + 日切小时计算；同一份「日期筛选」在两个时区会落在不同的
   UTC 窗口上，跨时区门店各自按本地营业日定位。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.store import store

MODULE = "inventory_timeline"

# 库存调整相关的内存表（在 seed.py 中预置示例数据）
STORE_TABLE = "inv_store"
PRODUCT_TABLE = "inv_product"
STORE_PRODUCT_TABLE = "inv_store_product"
ADJUSTMENT_TABLE = "inv_adjustment"
BATCH_TABLE = "inv_count_batch"

VIEW_AS_AT = "as_at"          # 复盘视角：只看查询时点之前系统已经收到的调整
VIEW_CURRENT = "current"      # 实际视角：按业务时点纳入全部已知调整
VALID_VIEWS = (VIEW_AS_AT, VIEW_CURRENT)

# 开放区间的哨兵终点（9999 年），物化时不会逐日展开到世界末日
OPEN_SENTINEL_YEAR = 9999
# 哨兵时间在负偏移时区转 UTC 会越界，统一收敛到这个安全的 UTC 远期常量
OPEN_END_UTC = datetime(9999, 12, 30, 23, 59, 59, tzinfo=timezone.utc)
# 有限区间物化时最多展开的营业日段数，超长区间尾部合并为一段开放段
MAX_MATERIALIZED_SEGMENTS = 366

# 门店日界默认按本地 00:00；允许门店配置非零日切小时（如夜班门店）
DEFAULT_DAY_CUT_HOUR = 0


# --------------------------------------------------------------------------- #
# 时间与时区工具
# --------------------------------------------------------------------------- #
def _zone(tz_name: str) -> ZoneInfo | timezone:
    """把时区名转成 tzinfo；环境缺 tzdata 时退回固定偏移，保证服务仍可启动。"""
    try:
        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - 兜底分支
        fallback = {"Asia/Shanghai": 8, "America/Los_Angeles": -8}
        return timezone(timedelta(hours=fallback.get(tz_name, 0)))


def parse_local(text: str, tz_name: str) -> datetime:
    """把无时区的 ISO 字符串解释为门店本地时间，并挂上对应时区。"""
    text = str(text).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name))
    return dt


def to_utc(dt: datetime) -> datetime:
    """统一换算到 UTC，跨门店比较都在 UTC 轴上进行。"""
    return dt.astimezone(timezone.utc)


def parse_utc(text: str, tz_name: str) -> datetime:
    dt = parse_local(text, tz_name)
    if dt.year >= OPEN_SENTINEL_YEAR:
        # 负偏移时区里 9999 本地时间转 UTC 会溢出，开放终点统一用安全常量
        return OPEN_END_UTC
    return to_utc(dt)


def local_text(dt_utc: datetime, tz_name: str) -> str:
    """把 UTC 时刻格式化成门店本地时间字符串，便于界面核对。"""
    return dt_utc.astimezone(_zone(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def day_cut_hour(store_row: dict) -> int:
    try:
        return int(store_row.get("日切小时", DEFAULT_DAY_CUT_HOUR) or DEFAULT_DAY_CUT_HOUR)
    except (TypeError, ValueError):
        return DEFAULT_DAY_CUT_HOUR


def business_day(dt_local: datetime, cut_hour: int) -> str:
    """按门店日切小时确定营业日：日切前的凌晨时段仍归属前一个营业日。"""
    day = dt_local.date()
    if dt_local.hour < cut_hour:
        day = day - timedelta(days=1)
    return day.isoformat()


def business_day_window(biz_day: str, tz_name: str, cut_hour: int) -> tuple[datetime, datetime]:
    """营业日 -> 本地日界 [起, 止)，再换算为 UTC 半开窗口。"""
    day = datetime.fromisoformat(biz_day).date()
    start_local = datetime(day.year, day.month, day.day, cut_hour, tzinfo=_zone(tz_name))
    if day.year >= OPEN_SENTINEL_YEAR:
        # 哨兵年份不再加一天，用当年最后一刻作为兜底终点
        end_local = datetime(OPEN_SENTINEL_YEAR, 12, 31, 23, 59, 59, tzinfo=_zone(tz_name))
    else:
        end_local = start_local + timedelta(days=1)
    return to_utc(start_local), to_utc(end_local)


# --------------------------------------------------------------------------- #
# 主服务
# --------------------------------------------------------------------------- #
class InventoryTimelineService:
    # ---- 基础读取 ---------------------------------------------------------- #
    def list_stores(self) -> list[dict]:
        return store.rows(STORE_TABLE)

    def list_products(self) -> list[dict]:
        return store.rows(PRODUCT_TABLE)

    def list_store_products(self, store_id: str | None = None) -> list[dict]:
        rows = store.rows(STORE_PRODUCT_TABLE)
        if store_id:
            rows = [r for r in rows if r.get("门店编号") == store_id]
        return rows

    def get_store(self, store_id: str) -> dict | None:
        for row in store.rows(STORE_TABLE):
            if row.get("门店编号") == store_id:
                return row
        return None

    def get_product(self, sku: str) -> dict | None:
        for row in store.rows(PRODUCT_TABLE):
            if row.get("商品编号") == sku:
                return row
        return None

    def list_adjustments(self, store_id: str | None = None, sku: str | None = None) -> list[dict]:
        rows = store.rows(ADJUSTMENT_TABLE)
        if store_id:
            rows = [r for r in rows if r.get("门店编号") == store_id]
        if sku:
            rows = [r for r in rows if r.get("商品编号") == sku]
        return rows

    def list_batches(self) -> list[dict]:
        return store.rows(BATCH_TABLE)

    # ---- 盘点批次去重登记 -------------------------------------------------- #
    def submit_count_batch(self, values: dict) -> tuple[dict | None, list[str], str]:
        """登记盘点批次并落库存调整；批次编号重复时幂等拒绝，不产生第二条调整。

        返回 (批次或调整信息, 缺失字段, 说明消息)。
        """
        required = ["批次编号", "门店编号", "商品编号", "盘点数量", "业务时点", "系统收到时刻"]
        missing = [f for f in required if str(values.get(f) or "").strip() == ""]
        if missing:
            return None, missing, ""

        store_row = self.get_store(str(values["门店编号"]))
        if store_row is None:
            return None, [], f"门店 {values['门店编号']} 不存在"
        if self.get_product(str(values["商品编号"])) is None:
            return None, [], f"商品 {values['商品编号']} 不存在"

        batch_no = str(values["批次编号"]).strip()
        # 幂等：同一批次编号只允许成功提交一次
        for batch in store.rows(BATCH_TABLE):
            if batch.get("批次编号") == batch_no:
                return batch, [], f"批次 {batch_no} 已提交过，本次为重复提交，未再产生库存调整"

        tz_name = str(store_row.get("时区"))
        zone = _zone(tz_name)
        biz_time = parse_local(str(values["业务时点"]), tz_name)
        recv_time = parse_local(str(values["系统收到时刻"]), tz_name)
        counted_qty = self._to_int(values["盘点数量"])
        if counted_qty is None:
            return None, [], "盘点数量必须是整数"

        # 盘点调整 = 实盘数量 - 盘点时点的账面库存（按当前已知口径）
        book_qty = self.stock_at(
            str(values["门店编号"]), str(values["商品编号"]),
            point=values["业务时点"], view=VIEW_CURRENT,
        )["库存数量"]
        delta = counted_qty - book_qty

        batch_id = max((int(b.get("id", 0)) for b in store.rows(BATCH_TABLE)), default=0) + 1
        batch = {
            "id": batch_id,
            "批次编号": batch_no,
            "门店编号": str(values["门店编号"]),
            "商品编号": str(values["商品编号"]),
            "业务时点": values["业务时点"],
            "系统收到时刻": values["系统收到时刻"],
            "盘点数量": counted_qty,
            "账面数量": book_qty,
            "调整数量": delta,
            "迟到补录": recv_time.astimezone(zone).date() > biz_time.astimezone(zone).date(),
            "提交状态": "已入账",
        }
        store.rows(BATCH_TABLE).append(batch)

        adjustment = self._insert_adjustment({
            "门店编号": str(values["门店编号"]),
            "商品编号": str(values["商品编号"]),
            "调整类型": "盘点",
            "调整数量": delta,
            "业务时点": values["业务时点"],
            "系统收到时刻": values["系统收到时刻"],
            "有效起": values["业务时点"],
            "批次编号": batch_no,
        })
        adjustment["关联批次"] = batch_no
        return {"批次": batch, "调整": adjustment}, [], f"批次 {batch_no} 已入账，生成调整 {delta:+d}"

    def submit_adjustment(self, values: dict) -> tuple[dict | None, list[str], str]:
        """登记一条带有效区间的库存调整；携带批次编号时同样做批次幂等。"""
        required = ["门店编号", "商品编号", "调整类型", "调整数量", "有效起", "系统收到时刻"]
        missing = [f for f in required if str(values.get(f) or "").strip() == ""]
        if missing:
            return None, missing, ""
        store_row = self.get_store(str(values["门店编号"]))
        if store_row is None:
            return None, [], f"门店 {values['门店编号']} 不存在"

        batch_no = str(values.get("批次编号") or "").strip()
        if batch_no:
            for adj in store.rows(ADJUSTMENT_TABLE):
                if adj.get("批次编号") == batch_no:
                    return adj, [], f"批次 {batch_no} 的调整已存在，重复提交已忽略"
        try:
            int(values["调整数量"])
        except (TypeError, ValueError):
            return None, [], "调整数量必须是整数"
        adjustment = self._insert_adjustment(values)
        return adjustment, [], f"调整 {adjustment['调整编号']} 已登记"

    def _insert_adjustment(self, values: dict) -> dict:
        store_row = self.get_store(str(values["门店编号"]))
        tz_name = str(store_row.get("时区"))
        rows = store.rows(ADJUSTMENT_TABLE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        next_seq = max((int(r.get("调整序号", 0)) for r in rows), default=1000) + 1
        effective_start = str(values["有效起"])
        effective_end = str(values.get("有效止") or "9999-12-31 23:59:59")
        biz_time = str(values.get("业务时点") or effective_start)
        recv_time = str(values["系统收到时刻"])

        biz_local = parse_local(biz_time, tz_name)
        recv_utc = to_utc(parse_local(recv_time, tz_name))
        biz_day = business_day(biz_local, day_cut_hour(store_row))
        late = recv_utc.astimezone(_zone(tz_name)).date() > biz_local.date()

        adjustment = {
            "id": next_id,
            "调整编号": f"ADJ-{next_seq:04d}",
            "调整序号": next_seq,
            "门店编号": str(values["门店编号"]),
            "商品编号": str(values["商品编号"]),
            "调整类型": str(values["调整类型"]),
            "调整数量": int(values["调整数量"]),
            "业务时点": biz_time,
            "系统收到时刻": recv_time,
            "有效起": effective_start,
            "有效止": effective_end,
            "批次编号": str(values.get("批次编号") or "").strip(),
            "业务营业日": biz_day,
            "迟到补录": late,
        }
        rows.append(adjustment)
        return adjustment

    # ---- 有效区间物化（跨午夜拆分，半开不重叠） ----------------------------- #
    def materialize_intervals(self, store_id: str, sku: str) -> list[dict]:
        """把一条调整的有效区间按门店营业日界切成半开子区间。

        跨午夜的调整会得到两段 [起, 日界) 与 [日界, 止)，两段共享同一调整编号，
        各自归属不同营业日，因此同一条调整不会在两个相邻区间里重复出现。
        开放区间（有效止为 9999 哨兵）只物化首个营业日段，其余合并为一条开放段，
        避免逐日展开到世界末日；库存定位本身走 ``stock_at`` 的半开区间比较，
        不依赖这里的物化结果。
        """
        store_row = self.get_store(store_id)
        if store_row is None:
            return []
        tz_name = str(store_row.get("时区"))
        cut = day_cut_hour(store_row)
        pieces: list[dict] = []
        for adj in self.list_adjustments(store_id, sku):
            start = parse_utc(adj["有效起"], tz_name)
            end = parse_utc(adj["有效止"], tz_name)
            open_ended = end.year >= OPEN_SENTINEL_YEAR
            cursor = start
            produced = 0
            while cursor < end and produced < MAX_MATERIALIZED_SEGMENTS:
                cursor_local = cursor.astimezone(_zone(tz_name))
                bday = business_day(cursor_local, cut)
                win_start, win_end = business_day_window(bday, tz_name, cut)
                seg_start = max(cursor, win_start)
                seg_end = min(end, win_end)
                if seg_start < seg_end:
                    pieces.append(self._interval_piece(
                        adj, store_id, sku, tz_name, cut, seg_start, seg_end, open_ended=False))
                    produced += 1
                cursor = seg_end
                if open_ended and cursor < end:
                    # 开放尾部：一段标到原始终点，标记为开放区间，不再逐日展开
                    tail_day = business_day(cursor.astimezone(_zone(tz_name)), cut)
                    pieces.append(self._interval_piece(
                        adj, store_id, sku, tz_name, cut, cursor, end,
                        open_ended=True, biz_day_override=tail_day,
                        end_text=str(adj["有效止"])))
                    break
        pieces.sort(key=lambda p: (p["区间起UTC"], p["调整编号"]))
        return pieces

    def _interval_piece(self, adj: dict, store_id: str, sku: str, tz_name: str,
                        cut: int, seg_start, seg_end, *, open_ended: bool,
                        biz_day_override: str | None = None,
                        end_text: str | None = None) -> dict:
        if biz_day_override is None:
            biz_day_override = business_day(seg_start.astimezone(_zone(tz_name)), cut)
        return {
            "调整编号": adj["调整编号"],
            "商品编号": sku,
            "门店编号": store_id,
            "营业日": biz_day_override,
            "区间起": local_text(seg_start, tz_name),
            "区间止": end_text if (open_ended and end_text) else local_text(seg_end, tz_name),
            "区间起UTC": seg_start.isoformat(),
            "区间止UTC": seg_end.isoformat(),
            "开放区间": open_ended,
            "调整数量": adj["调整数量"],
            "调整类型": adj["调整类型"],
            "业务时点": adj["业务时点"],
            "系统收到时刻": adj["系统收到时刻"],
            "迟到补录": adj["迟到补录"],
            "批次编号": adj.get("批次编号", ""),
        }

    # ---- 单门店单商品时点库存 ---------------------------------------------- #
    def stock_at(self, store_id: str, sku: str, point: str, view: str = VIEW_AS_AT) -> dict:
        """定位某门店某商品在查询时点的有效库存，并回带命中的来源区间。"""
        if view not in VALID_VIEWS:
            raise ValueError(f"视角 view 仅支持 {VALID_VIEWS}")
        store_row = self.get_store(store_id)
        if store_row is None:
            raise ValueError(f"门店 {store_id} 不存在")
        tz_name = str(store_row.get("时区"))
        point_utc = parse_utc(point, tz_name)
        point_local = point_utc.astimezone(_zone(tz_name))
        bday = business_day(point_local, day_cut_hour(store_row))

        base = self._base_qty(store_id, sku)
        applied: list[dict] = []
        for adj in self.list_adjustments(store_id, sku):
            start = parse_utc(adj["有效起"], tz_name)
            end = parse_utc(adj["有效止"], tz_name)
            in_interval = start <= point_utc < end  # 半开区间，端点不重复计入
            if not in_interval:
                continue
            if view == VIEW_AS_AT:
                # 复盘视角：查询时点系统尚未收到的补录不可见
                if parse_utc(adj["系统收到时刻"], tz_name) > point_utc:
                    continue
            applied.append({
                "调整编号": adj["调整编号"],
                "调整类型": adj["调整类型"],
                "调整数量": adj["调整数量"],
                "业务时点": adj["业务时点"],
                "系统收到时刻": adj["系统收到时刻"],
                "业务营业日": adj["业务营业日"],
                "迟到补录": adj["迟到补录"],
                "批次编号": adj.get("批次编号", ""),
            })

        applied.sort(key=lambda a: a["调整编号"])
        stock = base + sum(int(a["调整数量"]) for a in applied)
        return {
            "门店编号": store_id,
            "门店名称": store_row.get("门店名称"),
            "时区": tz_name,
            "商品编号": sku,
            "商品名称": (self.get_product(sku) or {}).get("商品名称"),
            "查询时点": point,
            "查询时点本地": local_text(point_utc, tz_name),
            "营业日": bday,
            "视角": view,
            "期初库存": base,
            "库存数量": stock,
            "命中调整数": len(applied),
            "来源区间": applied,
        }

    def _base_qty(self, store_id: str, sku: str) -> int:
        for row in store.rows(STORE_PRODUCT_TABLE):
            if row.get("门店编号") == store_id and row.get("商品编号") == sku:
                return int(row.get("期初库存", 0))
        return 0

    @staticmethod
    def _to_int(value) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ---- 跨表时点查询（门店商品 × 时区 × 业务时点 × 带区间调整） ------------ #
    def point_query(
        self,
        *,
        store_id: str | None = None,
        sku: str | None = None,
        point: str | None = None,
        biz_day: str | None = None,
        view: str = VIEW_AS_AT,
    ) -> dict:
        """跨表查询入口：按门店日界给每个查询时点返回有效库存。

        - 给 ``point``：直接定位该时点；
        - 给 ``biz_day``：在营业日收口时刻（次日日界）评估，使当日调整全部生效，
          再按调整的业务营业日归属过滤；
        - 两个视角分别对应「复盘当时决策」与「计算实际库存」。
        """
        if view not in VALID_VIEWS:
            raise ValueError(f"视角 view 仅支持 {VALID_VIEWS}")

        store_rows = [s for s in self.list_stores() if not store_id or s["门店编号"] == store_id]
        if store_id and not store_rows:
            raise ValueError(f"门店 {store_id} 不存在")

        rows: list[dict] = []
        for store_row in store_rows:
            tz_name = str(store_row.get("时区"))
            cut = day_cut_hour(store_row)
            if point:
                query_point = point
            elif biz_day:
                # 营业日口径：在营业日收口时刻（次日日界）评估，使当日调整全部生效，
                # 再按调整的业务营业日归属过滤；复盘视角仍由系统收到时刻卡可见性。
                _, win_end = business_day_window(biz_day, tz_name, cut)
                query_point = local_text(win_end, tz_name)
            else:
                raise ValueError("必须提供查询时点 point 或营业日 biz_day")

            sp_rows = self.list_store_products(str(store_row["门店编号"]))
            for sp in sp_rows:
                product_sku = str(sp["商品编号"])
                if sku and product_sku != sku:
                    continue
                result = self.stock_at(str(store_row["门店编号"]), product_sku, query_point, view)
                if biz_day:
                    # 营业日筛选：只保留归属该营业日的来源区间，仍保留可追溯的区间信息
                    result["营业日"] = biz_day
                    result["来源区间"] = [a for a in result["来源区间"] if a["业务营业日"] == biz_day]
                    result["库存数量"] = result["期初库存"] + sum(
                        int(a["调整数量"]) for a in result["来源区间"])
                    result["命中调整数"] = len(result["来源区间"])
                rows.append(result)

        rows.sort(key=lambda r: (r["门店编号"], r["商品编号"]))
        return {
            "视角": view,
            "查询时点": point,
            "营业日": biz_day,
            "门店数": len(store_rows),
            "行数": len(rows),
            "items": rows,
        }

    # ---- 数据集市：按门店 × 商品聚合，保留来源区间，支持筛选与定位 ---------- #
    def mart(
        self,
        *,
        store_id: str | None = None,
        biz_day: str | None = None,
        point: str | None = None,
        view: str = VIEW_AS_AT,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """消费库存时点关联结果，按门店与商品汇总可用库存与缺记录商品数。

        输出每一行都带 ``来源区间`` 明细，因此按门店或营业时点筛选后仍能回查来源。
        """
        point_result = self.point_query(
            store_id=store_id, point=point, biz_day=biz_day, view=view)
        all_rows = point_result["items"]
        aggregated: list[dict] = []
        for row in all_rows:
            sources = row["来源区间"]
            # 缺记录：该门店商品在查询口径下没有任何有效调整来源
            missing = 1 if not sources else 0
            aggregated.append({
                "门店编号": row["门店编号"],
                "门店名称": row["门店名称"],
                "时区": row["时区"],
                "商品编号": row["商品编号"],
                "商品名称": row["商品名称"],
                "营业日": row["营业日"],
                "视角": view,
                "可用库存": row["库存数量"],
                "缺记录商品数": missing,
                "来源区间数": len(sources),
                "来源区间": sources,
            })

        total = len(aggregated)
        start = max(page - 1, 0) * size
        page_rows = aggregated[start:start + size]
        missing_total = sum(r["缺记录商品数"] for r in aggregated)
        return {
            "items": page_rows,
            "total": total,
            "page": page,
            "size": size,
            "视角": view,
            "营业日": biz_day,
            "查询时点": point,
            "汇总": {
                "门店商品行数": total,
                "可用库存合计": sum(r["可用库存"] for r in aggregated),
                "缺记录商品总数": missing_total,
            },
        }

    def locate_product(self, *, store_id: str, sku: str, biz_day: str | None = None,
                       point: str | None = None, view: str = VIEW_AS_AT,
                       size: int = 20) -> dict:
        """末页回查：定位指定商品在集市分页中的页号、页内位置与汇总贡献。"""
        point_result = self.point_query(
            store_id=None, point=point, biz_day=biz_day, view=view)
        ordered = sorted(point_result["items"], key=lambda r: (r["门店编号"], r["商品编号"]))
        index = next(
            (i for i, r in enumerate(ordered)
             if r["门店编号"] == store_id and r["商品编号"] == sku),
            None,
        )
        if index is None:
            raise ValueError(f"商品 {sku} 在门店 {store_id} 的查询口径下不存在")
        page_no = index // size + 1
        offset = index % size
        target = ordered[index]
        sources = target["来源区间"]
        return {
            "门店编号": store_id,
            "商品编号": sku,
            "全局序号": index + 1,
            "页号": page_no,
            "页内位置": offset + 1,
            "分页大小": size,
            "营业日": target["营业日"],
            "视角": view,
            "可用库存": target["库存数量"],
            "缺记录商品数": 0 if sources else 1,
            "来源区间数": len(sources),
            "来源区间": sources,
            "汇总贡献": {
                "可用库存增量": target["库存数量"],
                "缺记录增量": 0 if sources else 1,
            },
        }


service = InventoryTimelineService()
