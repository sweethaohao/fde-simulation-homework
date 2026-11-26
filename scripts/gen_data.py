"""生成 8 门店 × 50 SKU × 42 天合成数据（固定随机种子，可复现）。

全部为合成数据，不含任何真实业务数据；输出到 data/seed/*.csv。
刻意植入：缺货率当日 12% / 时段加权 21%、账实差异 8%（1/3 编码伪差异）、
延迟归因、以及 f44 五类兜底的确定性触发样例。
"""

from __future__ import annotations  # 延迟注解

import argparse  # 命令行参数
import csv  # 写 CSV
import json  # 事件 payload
import random  # 固定种子随机
from datetime import date, datetime, timedelta, timezone  # 日期与时间
from pathlib import Path  # 路径

SEED = 20261119  # 固定种子，保证可复现
DAYS = 42  # 6 周
START = date(2026, 9, 1)  # 试点起始日
TZ = timezone(timedelta(hours=8))  # 东八区
STORE_IDS = [f"S{i:02d}" for i in range(1, 9)]  # S01–S08
STORE_NAMES = ["静安店", "徐汇店", "浦东店", "长宁店", "普陀店", "虹口店", "杨浦店", "闵行店"]  # 合成门店名
STORE_TIERS = ["high", "high", "high", "medium", "medium", "medium", "low", "low"]  # 高/中/低缺货三档 3/3/2
TIER_FACTOR = {"high": 1.25, "medium": 1.0, "low": 0.80}  # 门店需求系数

CATEGORIES = ["饮料", "零食", "粮油", "纸品", "日化"]  # 常温品大类
SKU_NAMES = [  # 50 个合成商品名（不引用真实品牌）
    "拾光气泡水", "拾光苏打水", "青柠味茶饮", "无糖乌龙茶", "电解质水",
    "椰香植物饮", "低糖酸奶饮", "谷物早餐奶", "冷萃咖啡液", "蜂蜜柚子茶",
    "海盐苏打饼", "原味薯片", "烘焙坚果", "黑巧克力块", "果干混合装",
    "低糖曲奇", "全麦吐司", "即食燕麦片", "玉米脆片", "米香锅巴",
    "小包装面粉", "东北长粒米", "食用调和油", "挂面组合装", "五谷杂粮包",
    "厨房纸巾", "抽取式面巾", "卷筒卫生纸", "湿巾补充装", "一次性抹布",
    "洗衣凝珠", "餐具洗洁精", "衣物柔顺剂", "浴室清洁剂", "厨房去油剂",
    "垃圾袋中号", "保鲜袋组合", "铝箔锡纸", "密封夹套装", "清洁海绵",
    "薄荷糖", "无糖口香糖", "即食海苔", "卤味豆干", "小袋花生",
    "瓶装纯净水", "便携湿巾", "旅行装洗发水", "棉柔巾", "厨房收纳袋",
]

# f44 五类兜底的确定性样例（DEMO.md 会引用）
DEMO_PROMO_STORE, DEMO_PROMO_SKU = "S01", "K001"  # 临时促销
DEMO_TRUNC_STORE, DEMO_TRUNC_SKU = "S02", "K002"  # 缺货截断
DEMO_DIFF_STORE, DEMO_DIFF_SKU = "S03", "K003"  # 可售与账面不一致
DEMO_MISSING_SKU = "K049"  # 整箱规格缺失
DEMO_MISSING_STORE = "S08"  # 配送日历版本缺失
DEMO_DOWN_STORE = "S07"  # 关键系统不可用（快照缺 3 天）


def iso_day(d: date) -> str:
    """日期转 ISO 字符串。

    入参：d 日期。
    出参：`YYYY-MM-DD`。
    失败时的行为：不抛异常。
    """

    return d.isoformat()  # 仅日期


def iso_ts(d: date, hour: int, minute: int = 0) -> str:
    """日期 + 时刻转带时区 ISO 8601。

    入参：d 日期；hour 小时；minute 分钟。
    出参：`YYYY-MM-DDTHH:MM:00+08:00`。
    失败时的行为：不抛异常。
    """

    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ).isoformat()  # 显式带时区


def classify(index: int) -> str:
    """按序号给 SKU 分档。

    入参：index 0 基序号。
    出参：hot / regular / long_tail。
    失败时的行为：不抛异常。
    """

    if index < 10:  # 前 10 个爆款
        return "hot"
    if index < 35:  # 中间 25 个常规
        return "regular"
    return "long_tail"  # 后 15 个长尾


def base_daily(rng: random.Random, cls: str) -> float:
    """生成基准日销。

    入参：rng 随机源；cls 商品档位。
    出参：日均销量（浮点）。
    失败时的行为：不抛异常。
    """

    if cls == "hot":  # 爆款
        return rng.uniform(9, 18)  # 每天 9–18 件
    if cls == "regular":  # 常规
        return rng.uniform(3, 8)  # 每天 3–8 件
    return rng.uniform(0.4, 1.6)  # 长尾低频


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    """写 CSV（UTF-8，无 BOM）。

    入参：path 文件；header 表头；rows 行列表。
    出参：无。
    失败时的行为：IO 异常向上抛出。
    """

    path.parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在
    with path.open("w", encoding="utf-8", newline="") as fh:  # 覆盖写，保证幂等
        writer = csv.writer(fh)  # CSV 写入器
        writer.writerow(header)  # 表头
        writer.writerows(rows)  # 数据行


def generate(out_dir: Path) -> dict:
    """生成全部合成数据。

    入参：out_dir 输出目录。
    出参：各表行数统计。
    失败时的行为：IO 异常向上抛出；参数错误抛 ValueError。
    """

    rng = random.Random(SEED)  # 单一种子顺序消费，保证可复现
    days = [START + timedelta(days=i) for i in range(DAYS)]  # 42 个自然日

    # ---------- 门店 ----------
    stores: list[list] = []
    for idx, sid in enumerate(STORE_IDS):  # 逐个门店
        version = None if sid == DEMO_MISSING_STORE else "DC-2026-09-v1"  # S08 缺配送日历版本
        stores.append([sid, f"拾光·{STORE_NAMES[idx]}", "华东", STORE_TIERS[idx], version, 1])

    # ---------- SKU ----------
    skus: list[list] = []
    sku_meta: dict[str, dict] = {}  # 计算用元数据
    for idx in range(50):  # 50 个商品
        sid = f"K{idx + 1:03d}"  # K001–K050
        cls = classify(idx)  # 档位
        case_size = rng.choice([6, 12, 12, 24])  # 整箱规格
        if idx == 9:  # 爆款给更大箱规
            case_size = 24
        if sid == DEMO_MISSING_SKU:  # K049 整箱规格缺失，用于「依据缺失」兜底
            case_size = None
        moq = rng.choice([1, 1, 2, 3])  # 最小起订箱数
        cost = rng.randrange(180, 4600)  # 单位成本（分）
        cap = 360 if cls == "hot" else 240  # 仓容上限（件）
        skus.append([sid, SKU_NAMES[idx], CATEGORIES[idx % len(CATEGORIES)], cls, case_size, moq, cost, cap, 1])
        sku_meta[sid] = {"cls": cls, "case_size": case_size, "moq": moq, "cost": cost, "cap": cap}

    # ---------- 促销 ----------
    promotions: list[list] = [
        ["P01", DEMO_PROMO_STORE, DEMO_PROMO_SKU, "P2026-10-temp-v1", iso_day(days[40]), iso_day(days[41]), "temporary"],
        ["P02", None, "K004", "P2026-09-v1", iso_day(days[7]), iso_day(days[10]), "published"],
        ["P03", "S04", "K005", "P2026-09-v2", iso_day(days[19]), iso_day(days[23]), "published"],
        ["P04", "S02", "K001", "P2026-09-v2", iso_day(days[27]), iso_day(days[31]), "published"],
        ["P05", "S01", "K007", "P2026-09-v1", iso_day(days[14]), iso_day(days[18]), "published"],
        ["P06", "S05", "K012", "P2026-10-v1", iso_day(days[31]), iso_day(days[35]), "temporary"],
    ]
    promo_rows: list[list] = []
    for p in promotions:  # 补 is_synthetic 列
        promo_rows.append(p + [1])

    promo_map: dict[tuple[str, str, str], str] = {}  # (store,sku,date)->version，用于打 is_promo
    for pid, pstore, psku, version, start, end, status in promotions:  # 展开促销窗口
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)  # 窗口起止
        for d in days:  # 逐日判断
            if d0 <= d <= d1:
                for sid in STORE_IDS:  # 全国促销则覆盖所有门店
                    if pstore is None or pstore == sid:
                        promo_map[(sid, psku, iso_day(d))] = version

    # ---------- 日快照 ----------
    snapshots: list[list] = []
    base_map: dict[tuple[str, str], float] = {}  # (store,sku)->基准日销
    for sid in STORE_IDS:  # 门店
        factor = TIER_FACTOR[STORE_TIERS[STORE_IDS.index(sid)]]  # 门店系数
        for sku_id, meta in sku_meta.items():  # 商品
            base_map[(sid, sku_id)] = base_daily(rng, meta["cls"]) * factor  # 基准日销

    stockout_pairs: dict[str, list[tuple[str, str]]] = {}  # 每天缺货组合
    all_pairs = [(s, k) for s in STORE_IDS for k in sku_meta]  # 400 个门店-SKU
    pair_weights = [1.6 if sku_meta[k]["cls"] == "hot" else 1.0 if sku_meta[k]["cls"] == "regular" else 0.6 for _, k in all_pairs]  # 爆款更易缺
    for day_idx, d in enumerate(days):  # 逐日抽缺货组合
        rate = 0.12 if day_idx == DAYS - 1 else 0.21 + rng.uniform(-0.01, 0.01)  # 末日 12%，其余约 21%
        target = int(round(400 * rate))  # 当日缺货 SKU 数
        picked: list[tuple[str, str]] = []  # 已选组合
        seen: set[tuple[str, str]] = set()  # 去重集合
        guard = 0  # 防死循环
        while len(picked) < target and guard < target * 40:  # 加权抽样
            guard += 1  # 计数
            candidate = rng.choices(all_pairs, weights=pair_weights, k=1)[0]  # 按权重抽一个
            if candidate in seen:  # 已选过
                continue
            seen.add(candidate)  # 记录
            picked.append(candidate)  # 加入
        stockout_pairs[iso_day(d)] = picked  # 保存当日缺货组合

    for d in days:  # 逐日生成快照
        day_key = iso_day(d)  # 日期键
        weekend = d.weekday() >= 4  # 周五至周日客流更高
        for sid in STORE_IDS:  # 门店
            if sid == DEMO_DOWN_STORE and d >= days[39]:  # S07 最后 3 天快照缺失，演示「关键系统不可用」
                continue
            for sku_id, meta in sku_meta.items():  # 商品
                base = base_map[(sid, sku_id)]  # 基准日销
                promo_version = promo_map.get((sid, sku_id, day_key))  # 促销版本
                demand = base * (1.15 if weekend else 1.0) * (1.8 if promo_version else 1.0)  # 促销/周末抬升
                demand *= rng.uniform(0.85, 1.15)  # 日噪声
                is_stockout = (sid, sku_id) in stockout_pairs[day_key]  # 当日是否缺货
                if is_stockout:  # 货架为 0
                    if rng.random() < 0.55:  # 有货不可售：后仓有货但没上架
                        backroom = max(4, int(demand * rng.uniform(3, 10)))  # 后仓有货
                        shelf = 0  # 货架空
                        book = backroom  # 账面等于后仓
                        sales = 0  # 有货但不可售，销量为 0
                    else:  # 真正无货
                        backroom = 0  # 后仓也空
                        shelf = 0  # 货架空
                        book = 0  # 账面为 0
                        sales = 0  # 无货可卖
                    in_transit = int(demand * rng.uniform(1, 6)) if rng.random() < 0.4 else 0  # 部分在途
                else:  # 正常日
                    shelf = max(1, int(demand * rng.uniform(3, 6)))  # 货架 3–6 天
                    backroom = max(1, int(demand * rng.uniform(36, 46)))  # 后仓约 40 天，制造高周转天数
                    book = shelf + backroom  # 账面=货架+后仓
                    sales = max(0, int(round(demand)))  # 销量
                    in_transit = max(0, int(demand * rng.uniform(4, 8)))  # 在途约 6 天
                unit_cost = meta["cost"]  # 成本（分）
                snapshots.append([
                    day_key, sid, sku_id, book, shelf, backroom, in_transit, int(sales),
                    1 if promo_version else 0, 0, None, unit_cost, iso_ts(d, 6, 0), 1,
                ])

    # ---------- 确定性兜底样例（覆盖生成结果） ----------
    index = {(r[0], r[1], r[2]): i for i, r in enumerate(snapshots)}  # 快速定位行
    for offset, d in enumerate(days[-7:]):  # S02/K002 最近 7 天：有货不可售且销量为 0 → 缺货截断
        key = (iso_day(d), DEMO_TRUNC_STORE, DEMO_TRUNC_SKU)
        if key in index:
            i = index[key]
            snapshots[i][3] = 60  # 账面 60
            snapshots[i][4] = 0  # 货架 0
            snapshots[i][5] = 60  # 后仓 60
            snapshots[i][7] = 0  # 销量 0
    key = (iso_day(days[-1]), DEMO_DIFF_STORE, DEMO_DIFF_SKU)  # S03/K003 末日账实不一致
    if key in index:
        i = index[key]
        snapshots[i][3] = snapshots[i][4] + snapshots[i][5] + 80  # 账面比可售+后仓多 80
        snapshots[i][9] = 1  # 标记账实差异
        snapshots[i][10] = "true"  # 真差异
    key = (iso_day(days[-1]), DEMO_DIFF_STORE, DEMO_DIFF_SKU)  # 说明：同一行只保留一次
    for day_idx, d in enumerate(days):  # 促销最后两天确保货架有货
        if day_idx >= days.__len__() - 2:
            k2 = (iso_day(d), DEMO_PROMO_STORE, DEMO_PROMO_SKU)
            if k2 in index:
                i = index[k2]
                if snapshots[i][4] == 0:  # 若被抽中缺货，补回货架
                    snapshots[i][4] = 24
                    snapshots[i][3] = max(snapshots[i][3], 24)
                snapshots[i][8] = 1  # is_promo
    # 账实差异总体约 8%，其中 1/3 为编码伪差异
    total_rows = len(snapshots)  # 总行数
    diff_target = int(round(total_rows * 0.08))  # 目标差异行数
    existing_diff = sum(1 for r in snapshots if r[9] == 1)  # 已有差异行
    candidates = [i for i, r in enumerate(snapshots) if r[9] == 0]  # 可注入的行
    rng.shuffle(candidates)  # 打散
    for i in candidates[: max(0, diff_target - existing_diff)]:  # 注入差异
        delta = rng.choice([-6, -4, -3, 3, 4, 6, 8])  # 账面偏移
        snapshots[i][3] = max(0, snapshots[i][3] + delta)  # 改账面
        snapshots[i][9] = 1  # 标记差异
        snapshots[i][10] = "code_pseudo" if rng.random() < 0.34 else "true"  # 约 1/3 编码伪差异
    # 编码伪差异行数量补足到 1/3：把部分 true 改为 code_pseudo
    diff_indices = [i for i, r in enumerate(snapshots) if r[9] == 1]  # 全部差异行
    pseudo_need = len(diff_indices) // 3  # 目标伪差异数
    pseudo_have = sum(1 for i in diff_indices if snapshots[i][10] == "code_pseudo")  # 当前伪差异
    for i in diff_indices:  # 依次补足
        if pseudo_have >= pseudo_need:
            break
        if snapshots[i][10] == "true":
            snapshots[i][10] = "code_pseudo"
            pseudo_have += 1

    # ---------- 补货单 ----------
    orders: list[list] = []  # 补货单行
    order_ids: list[str] = []  # 供事件流使用
    parallel_days = days[21:]  # 后 3 周并行运行
    combos = [(s, k) for s in STORE_IDS if s != DEMO_DOWN_STORE for k in sku_meta]  # 可用组合
    chosen: set[tuple[str, str, str]] = set()  # 去重
    while len(orders) < 600:  # 约 600 张
        sid, sku_id = rng.choice(combos)  # 随机门店-SKU
        d = rng.choice(parallel_days)  # 并行窗口内
        key = (sid, sku_id, iso_day(d))  # 唯一键
        if key in chosen:  # 重复则重抽
            continue
        chosen.add(key)  # 记录
        seq = len(orders) + 1  # 序号
        order_id = f"ORD-2026-{seq:04d}"  # 单号
        biz_no = f"SG-{iso_day(d).replace('-', '')}-{seq:04d}"  # 业务编号
        status_roll = rng.random()  # 状态抽样
        if status_roll < 0.08:
            status = "STOPPED"  # 兜底停止
        elif status_roll < 0.15:
            status = "REJECTED"  # 驳回
        elif status_roll < 0.22:
            status = "PENDING_SIGN"  # 待签核
        elif status_roll < 0.30:
            status = "DRAFT"  # 草稿
        else:
            status = "SIGNED"  # 已签核
        base = base_map[(sid, sku_id)]  # 日销
        suggestion = max(0, int(base * rng.uniform(4, 9))) if status != "STOPPED" else None  # 建议量
        before_round = suggestion  # 取整前
        case_size = sku_meta[sku_id]["case_size"] or 12  # 缺箱规时演示用 12 但会触发兜底
        rounded = None if suggestion is None else ((suggestion + case_size - 1) // case_size) * case_size  # 向上取整
        cap = sku_meta[sku_id]["cap"]  # 仓容上限
        capped = None if rounded is None else (rounded - min(rounded, cap))  # 压回量
        final = None if rounded is None else min(rounded, cap)  # 最终量
        reason = None  # 兜底/驳回原因
        owner_role, owner_name, manual_at = None, None, None  # 兜底接手人
        if status == "STOPPED":  # 兜底样本
            reason = rng.choice(["临时促销生效中", "缺货截断，销量不可信", "可售与账面不一致", "依据缺失", "关键系统不可用"])
            owner_role, owner_name = "loss_manager", "傅景"  # 兜底接手人固定为商品与损耗经理
            manual_at = iso_ts(d, 18, 0)  # 人工确认时间
        reject_reason = "整箱规格与门店陈列不一致，退回核实" if status == "REJECTED" else None  # 驳回原因
        signed_by = "gu.minglan" if status == "SIGNED" else None  # 签核人
        signed_at = iso_ts(d, 17, 30) if status == "SIGNED" else None  # 签核时间
        created_by = "tang.yingqiu"  # 计划员创建
        orders.append([
            order_id, biz_no, sid, sku_id, iso_day(d), status, suggestion, before_round, rounded,
            capped, final, None, None, None, None, 0, reason, owner_role, owner_name, manual_at,
            created_by, iso_ts(d, 9, 0), iso_ts(d, 9, 20) if status != "DRAFT" else None,
            signed_by, signed_at, reject_reason, 0, 1,
        ])
        order_ids.append(order_id)
    # 保留一条「故意失败」的兜底快照作为验收证据
    orders[-1][0] = "ORD-DEMO-0001"
    orders[-1][1] = "SG-DEMO-0001"
    orders[-1][5] = "STOPPED"
    orders[-1][16] = "临时促销生效中（演示样例，保留不清理）"
    orders[-1][17] = "loss_manager"
    orders[-1][18] = "傅景"
    orders[-1][25] = 1  # is_demo
    order_ids[-1] = "ORD-DEMO-0001"

    # ---------- 事件流 ----------
    events: list[list] = []  # 事件行
    event_seq = 0  # 事件序号

    def add_event(order_id, sid, sku_id, etype, channel, when, reliable, payload) -> None:
        """追加一条事件。

        入参：order_id 关联单号；sid 门店；sku_id 商品；etype 类型；channel 渠道；
              when ISO 时间或 None；reliable 是否有可信时间戳；payload 结构化负载。
        出参：无。
        失败时的行为：不抛异常。
        """

        nonlocal event_seq  # 需要递增外层计数
        event_seq += 1  # 递增
        events.append([
            f"EV{event_seq:06d}", order_id, sid, sku_id, etype, channel, when, 1 if reliable else 0,
            json.dumps(payload, ensure_ascii=False), 1,
        ])

    for idx, o in enumerate(orders):  # 每张单据生成建议/取整/压回

        order_id, sid, sku_id, biz_date, status = o[0], o[2], o[3], o[4], o[5]
        d = date.fromisoformat(biz_date)  # 业务日期
        if status != "STOPPED":  # 有建议才有后续事件
            add_event(order_id, sid, sku_id, "suggestion_generated", "system", iso_ts(d, 9, 5), True,
                      {"suggestion_qty": o[6], "rule_version": "RULE-2026-09-v1"})
            if o[8] is not None:  # 取整事件
                add_event(order_id, sid, sku_id, "rounded_to_case", "system", iso_ts(d, 9, 6), True,
                          {"before": o[7], "after": o[8], "case_size": sku_meta[sku_id]["case_size"]})
            if o[9]:  # 仓容压回事件
                add_event(order_id, sid, sku_id, "capacity_capped", "system", iso_ts(d, 9, 7), True,
                          {"capped_units": o[9], "cap": sku_meta[sku_id]["cap"]})
        if status in {"SIGNED", "PENDING_SIGN", "REJECTED"}:  # 截单事件
            add_event(order_id, sid, sku_id, "cutoff", "system", iso_ts(d, 11, 0), True,
                      {"cutoff_time": "11:00", "note": "截单时点为配置项"})
        if status == "SIGNED":  # 出库与签收
            add_event(order_id, sid, sku_id, "outbound", "wms", iso_ts(d + timedelta(days=1), 7, 20), True,
                      {"qty": o[10], "wms_realtime": True})
            add_event(order_id, sid, sku_id, "delivered", "logistics", iso_ts(d + timedelta(days=2), 8, 40), True,
                      {"received_qty": o[10], "unmet_qty": 0})
        if idx % 40 == 0:  # 每 40 单抽一条跨店调拨审批（区域经理未受访）
            add_event(order_id, sid, sku_id, "transfer_approval", "system",
                      iso_ts(d, 10, 0) if rng.random() < 0.6 else None, rng.random() < 0.6,
                      {"approver": "区域经理", "unverified": True, "note": "该角色未受访"})
        if idx % 37 == 0:  # 要件不全退回
            add_event(order_id, sid, sku_id, "returned_for_missing_info", "complaint",
                      iso_ts(d + timedelta(days=1), 10, 0) if rng.random() < 0.3 else None, rng.random() < 0.3,
                      {"missing": ["原始业务编号", "资料版本"], "accepted_by": "叶书"})
        if idx % 25 == 0:  # 上架记录：全部无时间戳
            add_event(order_id, sid, sku_id, "shelved", "none", None, False,
                      {"note": "无系统记录，货架状态无法复核"})

    stockout_rows = [(r[0], r[1], r[2]) for r in snapshots if r[4] == 0]  # 全部缺货行
    rng.shuffle(stockout_rows)  # 打散
    for day_key, sid, sku_id in stockout_rows[:1500]:  # 发现缺货：30% 无时间戳
        has_ts = rng.random() >= 0.30  # 70% 有
        d = date.fromisoformat(day_key)  # 日期
        add_event(None, sid, sku_id, "stockout_detected", "manual",
                  iso_ts(d, rng.randint(8, 17), rng.randint(0, 59)) if has_ts else None, has_ts,
                  {"discovered_by": "店员目视", "recorded": has_ts})
    for _ in range(800):  # 门店报数：50% 有可信时间戳
        day_key, sid, sku_id = rng.choice(stockout_rows)  # 关联缺货行
        d = date.fromisoformat(day_key)  # 日期
        channel = rng.choice(["group_chat", "phone", "system"])  # 渠道
        has_ts = rng.random() < 0.5  # 一半有
        add_event(None, sid, sku_id, "report_to_hq", channel,
                  iso_ts(d, rng.randint(9, 19), rng.randint(0, 59)) if has_ts else None, has_ts,
                  {"channel": channel, "note": "关键原因未结构化"})
    for _ in range(300):  # 上架事件：全部留空
        day_key, sid, sku_id = rng.choice(stockout_rows)  # 关联缺货行
        add_event(None, sid, sku_id, "shelved", "none", None, False, {"note": "无记录"})
    for _ in range(120):  # 截单事件（独立样本）
        d = rng.choice(days)  # 随机日期
        add_event(None, rng.choice(STORE_IDS), None, "cutoff", "system", iso_ts(d, 11, 0), True,
                  {"cutoff_time": "11:00"})

    # ---------- 写出 ----------
    out_dir.mkdir(parents=True, exist_ok=True)  # 输出目录
    write_csv(out_dir / "stores.csv", ["store_id", "name", "region", "tier", "delivery_calendar_version", "is_synthetic"], stores)
    write_csv(out_dir / "skus.csv", ["sku_id", "name", "category", "abc_class", "case_size", "moq_cases", "unit_cost_cents", "warehouse_cap", "is_synthetic"], skus)
    write_csv(out_dir / "promotions.csv", ["promo_id", "store_id", "sku_id", "version", "start_date", "end_date", "status", "is_synthetic"], promo_rows)
    write_csv(out_dir / "snapshots.csv", ["biz_date", "store_id", "sku_id", "book_stock", "shelf_available", "backroom_stock", "in_transit", "sales_qty", "is_promo", "account_diff", "diff_cause", "unit_cost_cents", "captured_at", "is_synthetic"], snapshots)
    write_csv(out_dir / "orders.csv", ["order_id", "biz_no", "store_id", "sku_id", "biz_date", "status", "suggestion_qty", "before_round_qty", "rounded_qty", "capacity_capped_qty", "final_qty", "suggestion_json", "snapshot_json", "versions_json", "guardrail_json", "degraded", "reason", "owner_role", "owner_name", "manual_confirm_at", "created_by", "created_at", "submitted_at", "signed_by", "signed_at", "reject_reason", "is_demo", "is_synthetic"], orders)
    write_csv(out_dir / "events.csv", ["event_id", "order_id", "store_id", "sku_id", "event_type", "channel", "occurred_at", "ts_reliable", "payload", "is_synthetic"], events)

    stats = {
        "stores": len(stores), "skus": len(skus), "snapshots": len(snapshots),
        "orders": len(orders), "events": len(events), "promotions": len(promo_rows),
        "stockout_last_day": sum(1 for r in snapshots if r[0] == iso_day(days[-1]) and r[4] == 0),
        "stockout_rate_last_day": round(sum(1 for r in snapshots if r[0] == iso_day(days[-1]) and r[4] == 0) / 400, 4),
        "stockout_rate_time_weighted": round(sum(1 for r in snapshots if r[4] == 0) / len(snapshots), 4),
        "account_diff_rate": round(sum(1 for r in snapshots if r[9] == 1) / len(snapshots), 4),
    }
    return stats


def main() -> None:
    """命令行入口。

    入参：--out 输出目录（默认仓库 data/seed）。
    出参：无（打印统计）。
    失败时的行为：异常向上抛出，进程非零退出。
    """

    parser = argparse.ArgumentParser(description="生成合成数据")  # 参数解析
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "seed"))  # 默认输出
    args = parser.parse_args()  # 解析
    stats = generate(Path(args.out))  # 生成
    print(json.dumps(stats, ensure_ascii=False, indent=2))  # 打印可核对统计


if __name__ == "__main__":
    main()
