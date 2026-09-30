"""载入匿名演示样例：在住、待退（冻结）、六类争议复核。

用法：python -m minibar.seed [--reset]
--reset 会删除并重建数据库；默认库已存在则跳过。
"""
import sys
from datetime import timedelta

from . import config, services
from .art import generate
from .clock import clock
from .db import connect, init_db

# (sku, 名称, 类别, 售价, 成本, 图片)
PRODUCTS = [
    ("MB-W01", "矿泉水", "饮品", 3.0, 1.1, "water.svg"),
    ("MB-C01", "可乐", "饮品", 6.0, 2.2, "cola.svg"),
    ("MB-S01", "苏打水", "饮品", 5.0, 1.8, "spark.svg"),
    ("MB-J01", "橙汁", "饮品", 8.0, 3.0, "juice.svg"),
    ("MB-B01", "精酿啤酒", "酒精", 12.0, 4.5, "beer.svg"),
    ("MB-N01", "混合坚果", "零食", 15.0, 6.0, "nuts.svg"),
    ("MB-K01", "榛子巧克力", "零食", 10.0, 3.8, "choco.svg"),
    ("MB-P01", "海盐薯片", "零食", 9.0, 3.2, "chips.svg"),
]

# 每房陈列上限（sku -> 数量）
CAPS = {
    "MB-W01": 4, "MB-C01": 2, "MB-S01": 2, "MB-J01": 2,
    "MB-B01": 2, "MB-N01": 1, "MB-K01": 1, "MB-P01": 1,
}

ROOMS = {
    2: ["201", "202", "203", "204"],
    3: ["301", "302", "303", "304"],
}

STAFF = [
    ("M01", "王岚", "manager", "*"),
    ("F01", "陈雪", "front_desk", "*"),
    ("F02", "周琳", "front_desk", "2"),
    ("A01", "李勤", "attendant", "2"),
    ("A02", "赵影", "attendant", "3"),
]


def _d(offset):
    return (clock.today() + timedelta(days=offset)).isoformat()


def _dt(offset_day, hour=10):
    return (clock.now() + timedelta(days=offset_day)).replace(
        hour=hour, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _batch(conn, batch_no, sku, qty, expiry_offset, loc="cart", room_id=None,
           cart_id=1, damaged=0, reason=None, product_ids=None):
    pid = product_ids[sku] if product_ids is not None else conn.execute(
        "SELECT id FROM product WHERE sku=?", (sku,)).fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO batch (batch_no, product_id, qty, initial_qty, expiry_date, loc, "
        "room_id, cart_id, damaged, writeoff_reason, written_off_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_no, pid, qty, qty, _d(expiry_offset), loc, room_id,
         cart_id if loc == "cart" else None, damaged, reason,
         clock.iso() if loc == "writeoff" else None, clock.iso()))
    return cur.lastrowid


def seed(reset=False):
    if reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()
    generate()
    conn = connect()
    if conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"]:
        conn.close()
        return False  # 已有数据，跳过

    # ---- 员工 / 客房 / 商品 / 陈列上限 ----
    staff_id = {}
    for code, name, role, scope in STAFF:
        cur = conn.execute(
            "INSERT INTO staff (code, name, role, floor_scope) VALUES (?, ?, ?, ?)",
            (code, name, role, scope))
        staff_id[code] = cur.lastrowid
    mgr, fd, fd2, a1, a2 = (staff_id[k] for k in ("M01", "F01", "F02", "A01", "A02"))
    mgr = conn.execute("SELECT * FROM staff WHERE id=?", (mgr,)).fetchone()
    front = conn.execute("SELECT * FROM staff WHERE id=?", (fd,)).fetchone()
    front2 = conn.execute("SELECT * FROM staff WHERE id=?", (fd2,)).fetchone()
    att1 = conn.execute("SELECT * FROM staff WHERE id=?", (a1,)).fetchone()
    att2 = conn.execute("SELECT * FROM staff WHERE id=?", (a2,)).fetchone()

    room_id = {}
    for floor, nos in ROOMS.items():
        for no in nos:
            cur = conn.execute(
                "INSERT INTO room (room_no, floor, status, pms_status) VALUES (?, ?, 'vacant', 'VC')",
                (no, floor))
            room_id[no] = cur.lastrowid

    pid = {}
    for sku, name, cat, price, cost, img in PRODUCTS:
        cur = conn.execute(
            "INSERT INTO product (sku, name, category, unit_price, cost_price, image) "
            "VALUES (?, ?, ?, ?, ?, ?)", (sku, name, cat, price, cost, img))
        pid[sku] = cur.lastrowid

    for rid in room_id.values():
        for sku, qty in CAPS.items():
            conn.execute(
                "INSERT INTO display_cap (room_id, product_id, cap_qty) VALUES (?, ?, ?)",
                (rid, pid[sku], qty))

    # ---- 补货车批次：含临期 / 过期 / 破损 ----
    _batch(conn, "CW-2609A", "MB-W01", 12, 3)            # 临期，FEFO 优先
    _batch(conn, "CW-3101A", "MB-W01", 24, 120)
    _batch(conn, "CC-2611A", "MB-C01", 10, 60)
    _batch(conn, "CC-3201A", "MB-C01", 12, 200)
    _batch(conn, "CS-2612A", "MB-S01", 8, 90)
    _batch(conn, "CJ-2610A", "MB-J01", 6, 5)             # 临期橙汁
    _batch(conn, "CJ-2508X", "MB-J01", 2, -2)            # 已过期，只能报损
    _batch(conn, "CB-2705A", "MB-B01", 8, 200)
    _batch(conn, "CN-2612A", "MB-N01", 4, 30)
    _batch(conn, "CK-2702A", "MB-K01", 1, 100)           # 仅 1 件，制造短缺
    _batch(conn, "CP-2710A", "MB-P01", 5, 400)
    damaged_id = _batch(conn, "CC-2609D", "MB-C01", 3, 80, damaged=1)  # 包装破损
    conn.commit()

    # 破损照片文件落本地目录
    photo_file = config.PHOTO_DIR / "seed-damaged-cc-2609d.svg"
    photo_file.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180">'
        '<rect width="240" height="180" fill="#F4EFE3"/>'
        '<g fill="none" stroke="#9B3A3A" stroke-width="6"><path d="M30 30 L210 150">'
        '</path><path d="M210 30 L30 150"></path></g>'
        '<text x="120" y="172" text-anchor="middle" font-family="sans-serif" '
        'font-size="12" fill="#1F2A44">包装破损证据照片（演示）</text></svg>',
        encoding="utf-8")
    conn.execute(
        "INSERT INTO photo (filename, batch_id, uploaded_by, created_at, note) "
        "VALUES (?, ?, ?, ?, '车厢内发现易拉罐压损')",
        (photo_file.name, damaged_id, att1["id"], clock.iso()))

    # 历史报损记录
    _batch(conn, "CJ-2506X", "MB-J01", 2, -5, loc="writeoff", reason="expired")
    _batch(conn, "CN-2507D", "MB-N01", 1, 40, loc="writeoff", damaged=1, reason="damaged")
    conn.commit()

    def stock_room(room_no, offsets=None):
        """入住前按陈列上限铺房内批次（每商品一批）。"""
        offsets = offsets or {}
        for sku, qty in CAPS.items():
            _batch(conn, f"R{room_no}-{sku}", sku, qty,
                   offsets.get(sku, 180), loc="room", room_id=room_id[room_no])
        conn.commit()

    def caps_view(room_no, counted):
        return {pid[sku]: counted.get(sku, CAPS[sku]) for sku in CAPS}

    # ===================================================================
    # 1) 201 在住：昨日入住，有已入账消费 + 今日待确认 + 客人否认争议
    # ===================================================================
    stock_room("201")
    o201 = services.open_stay(conn, room_id["201"], "GUEST-7F3A", front)
    # 昨日第一次盘点：矿泉水 -2、啤酒 -1，全部确认
    t1, st, _ = services.submit_stocktake(
        conn, o201, caps_view("201", {"MB-W01": 2, "MB-B01": 1}),
        att1, "take-201-d1", counted_at=_dt(-1, 15))
    for c in conn.execute("SELECT id FROM consumption WHERE stocktake_id=?", (t1,)):
        services.confirm_consumption(conn, c["id"], front)
    # 今日盘点：可乐 -1、薯片 -1
    t2, st, _ = services.submit_stocktake(
        conn, o201, caps_view("201", {"MB-C01": 1, "MB-P01": 0}),
        att1, "take-201-d2", counted_at=_dt(0, 10))
    cons = {c["product_id"]: c["id"] for c in conn.execute(
        "SELECT * FROM consumption WHERE stocktake_id=?", (t2,))}
    # 可乐确认；薯片客人否认 -> guest_denial 复核
    services.confirm_consumption(conn, cons[pid["MB-C01"]], front)
    services.guest_deny_consumption(conn, cons[pid["MB-P01"]], front,
                                    note="客人称未食用薯片")

    # ===================================================================
    # 2) 301 在住：今日尚未盘点（未盘提醒样例）
    # ===================================================================
    stock_room("301")
    services.open_stay(conn, room_id["301"], "GUEST-2C91", front)

    # ===================================================================
    # 3) 202 待退：已冻结，账单含入账，剩一笔待确认，前台结算页处理
    # ===================================================================
    stock_room("202")
    o202 = services.open_stay(conn, room_id["202"], "GUEST-9B10", front)
    t, _, _ = services.submit_stocktake(
        conn, o202, caps_view("202", {"MB-J01": 1, "MB-N01": 0, "MB-K01": 0}),
        att1, "take-202-d1", counted_at=_dt(-1, 14))
    cons = conn.execute("SELECT * FROM consumption WHERE stocktake_id=?", (t,)).fetchall()
    for c in cons[:2]:
        services.confirm_consumption(conn, c["id"], front)   # 橙汁、坚果入账
    # 巧克力保持 pending -> 冻结后仍需处理，阻止直接关单
    services.freeze_checkout(conn, o202, front)

    # ===================================================================
    # 4) 203 在住 + 跨房放错：消费实际归属 204
    # ===================================================================
    stock_room("203")
    o203 = services.open_stay(conn, room_id["203"], "GUEST-4D77", front)
    stock_room("204")
    o204 = services.open_stay(conn, room_id["204"], "GUEST-5E02", front)
    t, _, _ = services.submit_stocktake(
        conn, o203, caps_view("203", {"MB-S01": 1}),
        att1, "take-203-d1", counted_at=_dt(0, 11))
    c_soda = conn.execute("SELECT id FROM consumption WHERE stocktake_id=?", (t,)).fetchone()["id"]
    services.flag_wrong_room(conn, c_soda, att1, room_id["204"],
                             note="204 客人借用，盘点放错房")

    # 204 盘点制造补货车短缺：巧克力被消耗（库存 1->0，需补 1），
    # 再额外盘点为 0 不产生更多消耗；改为 cap 已为 1，车上仅 1 件会被调走后即空。
    # 这里用坚果制造短缺：204 房坚果 0，需补 1，车上坚果 4 件足够；
    # 改为直接对巧克力构造：先把车上巧克力用空（201/202 补货已耗），
    # 204 巧克力盘点为 0 -> 需补 1 而车上无货 -> cart_shortage。
    t, _, short = services.submit_stocktake(
        conn, o204, caps_view("204", {"MB-K01": 0}),
        att1, "take-204-d1", counted_at=_dt(0, 11))
    # 若车上仍有余量则由前序补货决定，样例不强依赖；短缺复核也可能在 202 产生

    # ===================================================================
    # 5) 302 在住：离线盘点旧版本重传 -> offline_resend 复核
    # ===================================================================
    stock_room("302")
    o302 = services.open_stay(conn, room_id["302"], "GUEST-8A55", front)
    services.submit_stocktake(
        conn, o302, caps_view("302", {"MB-W01": 3}),
        att2, "take-302-v1", counted_at=_dt(0, 9))
    # 此时版本 v1；离线设备仍基于 v0 重传 -> 拒绝且不覆盖
    try:
        services.submit_stocktake_with_version(
            conn, o302, caps_view("302", {"MB-W01": 2}),
            att2, "take-302-offline", expected_version=0,
            counted_at=_dt(0, 8), note="楼层信号弱，离线表单重传")
    except services.IdempotentReplay:
        pass

    # ===================================================================
    # 6) 303 待退：退房冻结后前台补录 -> post_checkout 复核，待经理核准
    # ===================================================================
    stock_room("303")
    o303 = services.open_stay(conn, room_id["303"], "GUEST-6F19", front)
    services.freeze_checkout(conn, o303, front)
    services.post_checkout_add_charge(
        conn, o303, pid["MB-B01"], 1, front, note="查房发现迷你吧冰箱少一瓶啤酒")

    # 304 空房保留（楼层列表展示空房态）

    # ---- 临期批次复核样例（车上临期橙汁）----
    b = conn.execute("SELECT * FROM batch WHERE batch_no='CJ-2610A'").fetchone()
    conn.execute(
        "INSERT INTO review_case (case_no, kind, batch_id, product_id, qty, summary, "
        "status, opened_by, created_at) VALUES (?, 'batch_expiring', ?, ?, ?, ?, 'open', ?, ?)",
        (services._new_case_no(conn), b["id"], b["product_id"], b["qty"],
         "补货车橙汁 5 天内到期，请优先调配或报损评估", mgr["id"], clock.iso()))

    conn.commit()
    conn.close()
    return True


def main():
    reset = "--reset" in sys.argv
    created = seed(reset=reset)
    if created:
        print("演示数据已载入：在住(201/203/204/301/302)、待退(202/303)、六类复核样例。")
    else:
        print("数据库已存在且含数据，跳过（使用 --reset 重建）。")


if __name__ == "__main__":
    main()
