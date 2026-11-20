"""库存时点跨表查询验收脚本：覆盖需求中的全部口径与场景。

用 python -I 运行（隔离环境），只依赖标准库。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api/inventory_timeline"
PASS = 0
FAIL = 0


def call(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path: str) -> dict:
    return call("GET", path)


def post(path: str, values: dict) -> dict:
    return call("POST", path, {"values": values})


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def q(path: str = "", **kwargs) -> str:
    query = urllib.parse.urlencode({k: v for k, v in kwargs.items() if v is not None})
    return f"{path}?{query}" if query else path


print("== 1. 跨午夜调整：半开区间拆分，同一调整不重复落在相邻区间 ==")
pieces = get(q(path="/intervals", store_id="ST-SH01", sku="SKU-1001"))["items"]
night = [p for p in pieces if p["调整编号"] == "ADJ-1002"]
check("夜间出库被拆成 2 段", len(night) == 2, str(len(night)))
check("两段归属相邻营业日 11-17 / 11-18",
      {p["营业日"] for p in night} == {"2026-11-17", "2026-11-18"})
check("前一段在午夜前半开止、后一段从午夜起（端点不重叠）",
      night[0]["区间止"] == night[1]["区间起"] == "2026-11-18 00:00:00")
check("两段调整编号相同、各只出现一次",
      len({p["调整编号"] for p in night}) == 1 and len(night) == 2)
# 半开端点验证：午夜整点查询不应同时命中两段语义（stock_at 用 start <= t < end）
at_midnight = get(q(path="/point", store_id="ST-SH01", sku="SKU-1001",
                    point="2026-11-18 00:00:00", view="current"))["items"][0]
before_midnight = get(q(path="/point", store_id="ST-SH01", sku="SKU-1001",
                        point="2026-11-17 23:59:59", view="current"))["items"][0]
hits_mid = {a["调整编号"] for a in at_midnight["来源区间"]}
hits_before = {a["调整编号"] for a in before_midnight["来源区间"]}
check("23:59:59 命中夜间出库、00:00:00 也命中（半开：起点含、终点不含）",
      "ADJ-1002" in hits_mid and "ADJ-1002" in hits_before)
after_close = get(q(path="/point", store_id="ST-SH01", sku="SKU-1001",
                    point="2026-11-18 02:00:00", view="current"))["items"][0]
check("02:00:00（区间终点）不再命中夜间出库",
      "ADJ-1002" not in {a["调整编号"] for a in after_close["来源区间"]})

print()
print("== 2. 双时间轴：业务时点与系统收到时刻分别定位（复盘 vs 实际） ==")
# 迟到盘点：业务时点 11-17 23:30，系统 11-18 08:30 才收到
review = get(q(path="/point", store_id="ST-SH01", sku="SKU-1001",
               point="2026-11-18 07:00:00", view="as_at"))["items"][0]
actual = get(q(path="/point", store_id="ST-SH01", sku="SKU-1001",
               point="2026-11-18 07:00:00", view="current"))["items"][0]
check("复盘视角 07:00 看不到 08:30 才收到的迟到补录",
      "ADJ-1003" not in {a["调整编号"] for a in review["来源区间"]})
check("实际视角按业务时点纳入迟到补录",
      "ADJ-1003" in {a["调整编号"] for a in actual["来源区间"]})
check("两视角库存不同（复盘 90 vs 实际 88）",
      review["库存数量"] == 90 and actual["库存数量"] == 88,
      f"{review['库存数量']} / {actual['库存数量']}")
# 08:30 之后复盘视角也能看到了
review_after = get(q(path="/point", store_id="ST-SH01", sku="SKU-1001",
                     point="2026-11-18 09:00:00", view="as_at"))["items"][0]
check("收到时刻之后复盘视角补录可见",
      "ADJ-1003" in {a["调整编号"] for a in review_after["来源区间"]}
      and review_after["库存数量"] == 88)

print()
print("== 3. 盘点批次重复提交幂等：只产生一次调整 ==")
before_adj = get("/adjustments")["total"]
dup = post("/count_batches", {
    "批次编号": "BATCH-C01", "门店编号": "ST-SH01", "商品编号": "SKU-1001",
    "盘点数量": 70, "业务时点": "2026-11-17 23:30:00",
    "系统收到时刻": "2026-11-18 09:00:00"})
after_adj = get("/adjustments")["total"]
check("重复提交返回 ok=false 且提示重复", dup["ok"] is False and "重复提交" in dup["message"])
check("重复提交没有新增调整", before_adj == after_adj, f"{before_adj} -> {after_adj}")
# 新批次正常入账，且再次提交被幂等拦截
first = post("/count_batches", {
    "批次编号": "BATCH-C02", "门店编号": "ST-SH01", "商品编号": "SKU-1002",
    "盘点数量": 95, "业务时点": "2026-11-18 10:00:00",
    "系统收到时刻": "2026-11-18 10:10:00"})
check("新批次提交成功并生成调整", first["ok"] is True and "调整" in first["entry"])
mid_adj = get("/adjustments")["total"]
second = post("/count_batches", {
    "批次编号": "BATCH-C02", "门店编号": "ST-SH01", "商品编号": "SKU-1002",
    "盘点数量": 10, "业务时点": "2026-11-18 10:00:00",
    "系统收到时刻": "2026-11-18 10:20:00"})
check("同批次第二次提交被幂等拦截", second["ok"] is False and get("/adjustments")["total"] == mid_adj)
# 走通用调整接口带批次编号同样幂等
adj_dup = post("/adjustments", {
    "批次编号": "BATCH-C02", "门店编号": "ST-SH01", "商品编号": "SKU-1002",
    "调整类型": "损耗", "调整数量": -99, "有效起": "2026-11-18 10:00:00",
    "系统收到时刻": "2026-11-18 10:30:00"})
check("通用调整接口对已存在批次同样去重", adj_dup["ok"] is False and get("/adjustments")["total"] == mid_adj)

print()
print("== 4. 两个时区门店 + 跨界补单：相同日期筛选落在不同 UTC 窗口 ==")
sh = get(q(path="/point", store_id="ST-SH01", biz_day="2026-11-17", view="current"))["items"][0]
la = get(q(path="/point", store_id="ST-LA01", biz_day="2026-11-17", view="current"))["items"][0]
check("上海店营业日按 Asia/Shanghai 日界定位", sh["时区"] == "Asia/Shanghai" and sh["营业日"] == "2026-11-17")
check("洛杉矶店营业日按 America/Los_Angeles 日界定位",
      la["时区"] == "America/Los_Angeles" and la["营业日"] == "2026-11-17")
check("同一日期筛选两店各自命中本地调整（上海 -10/-5/-2，洛杉矶 -8）",
      sh["库存数量"] == 83 and la["库存数量"] == 52,
      f"{sh['库存数量']} / {la['库存数量']}")
# 洛杉矶跨午夜促销调整同样拆两段
la_pieces = get(q(path="/intervals", store_id="ST-LA01", sku="SKU-1002"))["items"]
la_night = [p for p in la_pieces if p["调整编号"] == "ADJ-1006"]
check("洛杉矶店跨日界调整拆成两段且营业日相邻",
      len(la_night) == 2 and {p["营业日"] for p in la_night} == {"2026-11-17", "2026-11-18"})
# 迟到补单的业务时点在上个营业日：按营业日复盘/实际两套口径
sh_day_review = get(q(path="/point", store_id="ST-SH01", biz_day="2026-11-17", view="as_at"))["items"][0]
sh_day_actual = get(q(path="/point", store_id="ST-SH01", biz_day="2026-11-17", view="current"))["items"][0]
check("同一营业日复盘口径不含迟到补单",
      "ADJ-1003" not in {a["调整编号"] for a in sh_day_review["来源区间"]}
      and sh_day_review["库存数量"] == 85)
check("同一营业日实际口径含迟到补单",
      "ADJ-1003" in {a["调整编号"] for a in sh_day_actual["来源区间"]}
      and sh_day_actual["库存数量"] == 83)

print()
print("== 5. 数据集市：按门店商品汇总可用库存与缺记录数，筛选后仍可回查来源区间 ==")
mart = get(q(path="/mart", biz_day="2026-11-17", view="current", size=5))
check("集市返回分页结构与汇总", {"items", "total", "page", "size", "汇总"} <= set(mart))
check("汇总含可用库存合计与缺记录商品总数",
      "可用库存合计" in mart["汇总"] and "缺记录商品总数" in mart["汇总"])
# 上海 SKU-1004 全无调整、SKU-1002 当日无调整（到货在 11-18）-> 当日均缺记录（取全量分页核对）
all_mart = get(q(path="/mart", biz_day="2026-11-17", view="current", size=200))
missing_rows = [r for r in all_mart["items"] if r["缺记录商品数"] == 1]
missing_keys = {(r["门店编号"], r["商品编号"]) for r in missing_rows}
check("当日无调整来源的门店商品计入缺记录（牛奶当日无变动 + 气泡水无记录）",
      missing_keys == {("ST-SH01", "SKU-1002"), ("ST-SH01", "SKU-1004")},
      json.dumps(sorted(missing_keys), ensure_ascii=False))
check("缺记录商品总数进入汇总", all_mart["汇总"]["缺记录商品总数"] == 2)
# 次日牛奶到货后即不再缺记录，说明缺记录是按营业时点口径计算的
next_mart = get(q(path="/mart", biz_day="2026-11-18", view="current", size=200))
next_missing = {(r["门店编号"], r["商品编号"]) for r in next_mart["items"] if r["缺记录商品数"] == 1}
milk_next = next(r for r in next_mart["items"]
                 if r["门店编号"] == "ST-SH01" and r["商品编号"] == "SKU-1002")
check("次日到货后牛奶脱离缺记录且来源可追溯",
      ("ST-SH01", "SKU-1002") not in next_missing and milk_next["来源区间数"] >= 1
      and any(a["调整类型"] == "到货" for a in milk_next["来源区间"])
      and milk_next["可用库存"] == 80 + 20 - 5)  # 期初 80 + 到货 20，含测试中登记的盘点批次 -5
check("每行都带来源区间明细", all("来源区间" in r for r in mart["items"]))
# 按门店筛选后仍能检索来源区间
sh_mart = get(q(path="/mart", store_id="ST-SH01", biz_day="2026-11-17", view="current"))
check("按门店筛选后来源区间仍可检索",
      all(r["门店编号"] == "ST-SH01" for r in sh_mart["items"])
      and any(r["来源区间"] for r in sh_mart["items"]))
# 按时点筛选
point_mart = get(q(path="/mart", point="2026-11-18 07:00:00", view="as_at"))
check("按查询时点筛选正常返回", point_mart["total"] >= 1 and all(r["视角"] == "as_at" for r in point_mart["items"]))

print()
print("== 6. 末页回查：两条指定商品的位置与汇总贡献 ==")
# 全部门店商品共 7 行（ST-LA01 3 + ST-SH01 4），size=4 -> 末页为第 2 页，
# 末页两条为 ST-SH01/SKU-1003（有来源）与 ST-SH01/SKU-1004（缺记录）
loc1 = get(q(path="/locate", store_id="ST-SH01", sku="SKU-1003",
             biz_day="2026-11-17", view="current", size=4))
loc2 = get(q(path="/locate", store_id="ST-SH01", sku="SKU-1004",
             biz_day="2026-11-17", view="current", size=4))
check("坚果商品定位到末页（第 2 页）并带页内位置",
      loc1["页号"] == 2 and 1 <= loc1["页内位置"] <= 4, json.dumps(loc1, ensure_ascii=False))
check("回查结果带可用库存与汇总贡献（50-3 报损=47）",
      loc1["可用库存"] == 47 and loc1["汇总贡献"]["可用库存增量"] == 47
      and loc1["来源区间数"] == 1)
check("缺记录商品的回查贡献为缺记录增量 1",
      loc2["缺记录商品数"] == 1 and loc2["汇总贡献"]["缺记录增量"] == 1
      and loc2["可用库存"] == 0 and loc2["页号"] == 2)
check("回查结果保留来源区间字段", "来源区间" in loc1 and "来源区间" in loc2)
# 直接取末页验证位置与 locate 一致
last_page = get(q(path="/mart", biz_day="2026-11-17", view="current", page=loc1["页号"], size=4))
idx1 = loc1["页内位置"] - 1
idx2 = loc2["页内位置"] - 1
check("末页同位置就是被回查的两条商品",
      (last_page["items"][idx1]["门店编号"] == "ST-SH01"
       and last_page["items"][idx1]["商品编号"] == "SKU-1003")
      and (last_page["items"][idx2]["商品编号"] == "SKU-1004"))
check("末页恰好 3 条（7 行 / size=4 的分页边界正确）", len(last_page["items"]) == 3)
# 不存在的商品给 404
try:
    get(q(path="/locate", store_id="ST-SH01", sku="SKU-9999", biz_day="2026-11-17"))
    check("不存在商品返回 404", False)
except urllib.error.HTTPError as e:
    check("不存在商品返回 404", e.code == 404)

print()
print("== 7. 入参校验 ==")
try:
    get("/point?store_id=ST-SH01")
    check("缺时点/营业日返回 400", False)
except urllib.error.HTTPError as e:
    check("缺时点/营业日返回 400", e.code == 400)
try:
    get(q(path="/point", store_id="ST-SH01", biz_day="2026-11-17", view="bogus"))
    check("非法视角返回 400", False)
except urllib.error.HTTPError as e:
    check("非法视角返回 400", e.code == 400)
bad = post("/count_batches", {"批次编号": "X"})
check("盘点批次缺字段返回 ok=false 并列出缺失项",
      bad["ok"] is False and "缺少必填字段" in bad["message"])

print()
print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(1 if FAIL else 0)
