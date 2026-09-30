"""核心领域服务：批次 FEFO、入住快照、盘点版本链、消费确认、复核流、账单与结算。

所有写操作都在单连接事务内完成，SQLite 串行化访问，适合演示规模。
"""
import sqlite3

from . import config
from .clock import clock, days_between
from .errors import (
    MinibarError, VersionConflict, OrderFrozen, PermissionDenied,
    IdempotentReplay, require_role, can_access_floor,
)


# --------------------------------------------------------------------- #
# 批次 / 补货车 / FEFO
# --------------------------------------------------------------------- #
def cart_batches(conn, cart_id=1):
    """补货车批次：未报损，按效期升序（FEFO 取用顺序）。"""
    rows = conn.execute(
        "SELECT b.*, p.name AS product_name, p.sku, p.unit_price "
        "FROM batch b JOIN product p ON p.id = b.product_id "
        "WHERE b.loc = 'cart' AND b.cart_id = ? AND b.damaged = 0 "
        "ORDER BY b.expiry_date, b.id", (cart_id,)).fetchall()
    return rows


def cart_list_view(conn, cart_id=1):
    """补货车清单（按商品汇总 + 明细批次），并标记临期/过期。"""
    today = clock.today()
    groups = {}
    for r in cart_batches(conn, cart_id):
        d = days_between(r["expiry_date"], today)
        g = groups.setdefault(r["product_id"], {
            "product_id": r["product_id"], "sku": r["sku"],
            "name": r["product_name"], "unit_price": r["unit_price"],
            "total_qty": 0, "batches": []})
        g["total_qty"] += r["qty"]
        g["batches"].append({**dict(r), "days_to_expiry": d,
                              "expiring": 0 <= d <= config.EXPIRY_WARN_DAYS,
                              "expired": d < 0})
    damaged = conn.execute(
        "SELECT b.*, p.name AS product_name FROM batch b JOIN product p ON p.id=b.product_id "
        "WHERE b.loc='cart' AND b.cart_id=? AND b.damaged=1 ORDER BY b.expiry_date",
        (cart_id,)).fetchall()
    return {"groups": sorted(groups.values(), key=lambda g: g["name"]),
            "damaged": [dict(d) for d in damaged]}


def _fefo_pick(conn, product_id, need, cart_id):
    """从补货车按效期升序取 need 件，返回 [(batch_id, qty)]；不足返回部分。"""
    picks = []
    remaining = need
    today = clock.today()
    rows = conn.execute(
        "SELECT * FROM batch WHERE loc='cart' AND cart_id=? AND product_id=? "
        "AND damaged=0 AND qty>0 AND expiry_date >= ? "
        "ORDER BY expiry_date, id",
        (cart_id, product_id, today.isoformat())).fetchall()
    for r in rows:
        if remaining <= 0:
            break
        take = min(r["qty"], remaining)
        picks.append((r["id"], take))
        remaining -= take
    return picks, remaining


def _move_batch_units(conn, batch_id, qty, loc, room_id=None, cart_id=None):
    """批次余量转 loc（从车上拆出转入房间；报损则整批/部分）。

    部分转移采用拆批：保留原批次剩余，新建同号子批（batch_no#）放目标位置，
    以保证先进先出可追溯。
    """
    r = conn.execute("SELECT * FROM batch WHERE id=?", (batch_id,)).fetchone()
    if qty > r["qty"]:
        raise MinibarError("批次余量不足")
    if qty == r["qty"]:
        conn.execute(
            "UPDATE batch SET loc=?, room_id=?, cart_id=? WHERE id=?",
            (loc, room_id, cart_id if loc == "cart" else None, batch_id))
        return batch_id
    # 拆批：同来源批次可能多次拆分，后缀递增避免 batch_no 冲突
    n = conn.execute(
        "SELECT COUNT(*) c FROM batch WHERE batch_no LIKE ?",
        (f"{r['batch_no']}#S{batch_id}%",)).fetchone()["c"]
    conn.execute("UPDATE batch SET qty = qty - ? WHERE id=?", (qty, batch_id))
    cur = conn.execute(
        "INSERT INTO batch (batch_no, product_id, qty, initial_qty, expiry_date, "
        "loc, room_id, cart_id, damaged, source_batch_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (f"{r['batch_no']}#S{batch_id}-{n+1}", r["product_id"], qty, qty,
         r["expiry_date"], loc, room_id, cart_id if loc == "cart" else None,
         batch_id, clock.iso()))
    return cur.lastrowid


# --------------------------------------------------------------------- #
# 报损：已过期 / 包装破损 —— 只能转报损，不能再次上架
# --------------------------------------------------------------------- #
def write_off_batch(conn, batch_id, reason, staff, note=""):
    require_role(staff, config.ROLE_ATTENDANT, config.ROLE_MANAGER)
    if reason not in config.WRITEOFF_REASONS:
        raise MinibarError("报损原因必须为 expired 或 damaged")
    r = conn.execute("SELECT * FROM batch WHERE id=?", (batch_id,)).fetchone()
    if r is None:
        raise MinibarError("批次不存在")
    if r["loc"] == "writeoff":
        raise IdempotentReplay("该批次已报损")
    today = clock.today()
    if reason == "expired":
        if days_between(r["expiry_date"], today) >= 0:
            raise MinibarError("未过期商品不得按过期报损")
    elif reason == "damaged" and not r["damaged"]:
        # 允许直接登记破损并报损
        pass
    conn.execute(
        "UPDATE batch SET loc='writeoff', room_id=NULL, cart_id=NULL, "
        "damaged=1, writeoff_reason=?, written_off_at=? WHERE id=?",
        (reason, clock.iso(), batch_id))
    cur = conn.execute(
        "INSERT INTO bill_line (order_id, line_type, amount, reason, idem_key, "
        "ref_type, ref_id, created_by, created_at) VALUES (NULL, 'writeoff', 0, ?, "
        "?, 'batch', ?, ?, ?)",
        (f"{reason}:{note}", f"wo-batch-{batch_id}", batch_id, staff["id"], clock.iso()))
    conn.commit()
    return cur.lastrowid


def mark_damaged(conn, batch_id, staff, note=""):
    """服务员发现包装破损：标记后只能走报损，不能补货上架。

    破损不属六类复核单——它只能报损；照片落本地目录后在效期报损页处理。
    """
    require_role(staff, config.ROLE_ATTENDANT, config.ROLE_MANAGER)
    r = conn.execute("SELECT * FROM batch WHERE id=?", (batch_id,)).fetchone()
    if r["loc"] == "writeoff":
        raise MinibarError("已报损批次")
    conn.execute("UPDATE batch SET damaged=1 WHERE id=?", (batch_id,))
    return batch_id


# --------------------------------------------------------------------- #
# 入住与快照
# --------------------------------------------------------------------- #
def open_stay(conn, room_id, guest_alias, staff):
    """入住：客房 -> occupied，并生成 v0 房内库存快照。"""
    require_role(staff, config.ROLE_FRONT_DESK, config.ROLE_MANAGER)
    room = conn.execute("SELECT * FROM room WHERE id=?", (room_id,)).fetchone()
    can_access_floor(staff, room["floor"])
    if room["status"] != "vacant":
        raise MinibarError("客房非空房，不能办理入住")
    order_no = f"SO-{room['room_no']}-{clock.now().strftime('%m%d')}-{room_id}"
    cur = conn.execute(
        "INSERT INTO stay_order (order_no, room_id, guest_alias, check_in_at, state) "
        "VALUES (?, ?, ?, ?, 'open')",
        (order_no, room_id, guest_alias, clock.iso()))
    order_id = cur.lastrowid
    _snapshot_room(conn, order_id, 0, None, "check_in", staff)
    conn.execute("UPDATE room SET status='occupied', pms_status='OC' WHERE id=?", (room_id,))
    conn.commit()
    return order_id


def _snapshot_room(conn, order_id, version, parent_version, kind, staff):
    """固化当前房内各商品数量为不可变快照版本。"""
    cur = conn.execute(
        "INSERT INTO stock_snapshot (order_id, version, parent_version, kind, "
        "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, version, parent_version, kind, clock.iso(), staff["id"]))
    snap_id = cur.lastrowid
    order = conn.execute("SELECT room_id FROM stay_order WHERE id=?", (order_id,)).fetchone()
    rows = conn.execute(
        "SELECT product_id, SUM(qty) AS q FROM batch WHERE loc='room' AND room_id=? "
        "GROUP BY product_id", (order["room_id"],)).fetchall()
    # 陈列上限覆盖：即使为 0 也固化进快照
    caps = {r["product_id"]: r["cap_qty"] for r in conn.execute(
        "SELECT product_id, cap_qty FROM display_cap WHERE room_id=?", (order["room_id"],))}
    qty_map = {r["product_id"]: r["q"] for r in rows}
    for pid, cap in caps.items():
        conn.execute(
            "INSERT INTO stock_snapshot_item (snapshot_id, product_id, qty) VALUES (?, ?, ?)",
            (snap_id, pid, qty_map.get(pid, 0)))
    conn.execute("UPDATE stay_order SET snap_version=? WHERE id=?", (version, order_id))
    return snap_id


def latest_snapshot_items(conn, order_id):
    v = conn.execute("SELECT snap_version FROM stay_order WHERE id=?", (order_id,)).fetchone()["snap_version"]
    snap = conn.execute("SELECT * FROM stock_snapshot WHERE order_id=? AND version=?",
                        (order_id, v)).fetchone()
    items = conn.execute(
        "SELECT ssi.product_id, ssi.qty, p.name, p.sku, p.unit_price, p.image "
        "FROM stock_snapshot_item ssi JOIN product p ON p.id=ssi.product_id "
        "WHERE ssi.snapshot_id=? ORDER BY p.category, p.name", (snap["id"],)).fetchall()
    return snap, [dict(x) for x in items]


# --------------------------------------------------------------------- #
# 盘点提交（版本链 / 离线重传 / FEFO 补货 / 消费生成 / 短缺复核）
# --------------------------------------------------------------------- #
def submit_stocktake(conn, order_id, counts, staff, idem_key, counted_at=None,
                     note="", cart_id=1):
    """服务员提交盘点。

    counts: {product_id: counted_qty}
    * idem_key 相同 -> 幂等重放，直接返回原单据（离线重传）。
    * expected_version 取单据携带值（服务员离线时基于的版本）；本演示从
      当前版本生成 expected_version，冲突场景由测试/重传接口显式传入。
    返回 (stocktake_id, status, shortages)。
    """
    return _submit(conn, order_id, counts, staff, idem_key,
                   counted_at or clock.iso(), note, cart_id,
                   expected_version=None)


def submit_stocktake_with_version(conn, order_id, counts, staff, idem_key,
                                  expected_version, counted_at=None, note="", cart_id=1):
    """显式携带基准版本的提交（离线盘点重传 / 版本冲突测试入口）。"""
    return _submit(conn, order_id, counts, staff, idem_key,
                   counted_at or clock.iso(), note, cart_id,
                   expected_version=expected_version)


def _submit(conn, order_id, counts, staff, idem_key, counted_at, note, cart_id,
            expected_version):
    require_role(staff, config.ROLE_ATTENDANT, config.ROLE_MANAGER)
    order = conn.execute("SELECT * FROM stay_order WHERE id=?", (order_id,)).fetchone()
    if order is None:
        raise MinibarError("订单不存在")
    room = conn.execute("SELECT * FROM room WHERE id=?", (order["room_id"],)).fetchone()
    can_access_floor(staff, room["floor"])
    if order["state"] == "closed":
        raise OrderFrozen("订单已关闭，不能再盘点")
    frozen = order["state"] == "frozen"

    # 幂等：离线重传同一 idem_key
    existing = conn.execute("SELECT * FROM stocktake WHERE idem_key=?", (idem_key,)).fetchone()
    if existing is not None:
        if existing["status"] == "accepted":
            raise IdempotentReplay("盘点已提交，请勿重复上传")
        # 此前被拒的旧版本重传：状态维持 rejected_stale，回流复核单
        raise IdempotentReplay("该离线盘点单为旧版本，已进入版本冲突复核")

    current_v = order["snap_version"]
    if expected_version is None:
        expected_version = current_v
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM stocktake WHERE order_id=?",
                       (order_id,)).fetchone()[0]

    cur = conn.execute(
        "INSERT INTO stocktake (order_id, seq, expected_version, status, idem_key, "
        "counted_at, submitted_at, submitted_by, note) VALUES (?, ?, ?, 'pending_resend', "
        "?, ?, ?, ?, ?)",
        (order_id, seq, expected_version, idem_key, counted_at, clock.iso(),
         staff["id"], note))
    take_id = cur.lastrowid
    for pid, qty in counts.items():
        conn.execute(
            "INSERT INTO stocktake_count (stocktake_id, product_id, counted_qty) "
            "VALUES (?, ?, ?)", (take_id, pid, int(qty)))

    # 退房冻结期间提交盘点 -> 不允许直接改库存，转退房后补录复核
    if frozen:
        conn.execute("UPDATE stocktake SET status='rejected_stale' WHERE id=?", (take_id,))
        _open_case(conn, config.REV_POST_CHECKOUT, order, room,
                   summary=f"退房冻结后收到第 {seq} 次盘点（{note or '离线补传'}），需前台复核补录",
                   staff=staff, stocktake_id=take_id)
        conn.commit()
        return take_id, "post_checkout", []

    # 版本冲突：旧版本不得覆盖新快照
    if expected_version != current_v:
        conn.execute("UPDATE stocktake SET status='rejected_stale' WHERE id=?", (take_id,))
        _open_case(conn, config.REV_OFFLINE_RESEND, order, room,
                   summary=(f"离线盘点基于 v{expected_version}，当前已为 v{current_v}，"
                            f"旧版本不得覆盖，需人工比对后重盘"),
                   staff=staff, stocktake_id=take_id)
        conn.commit()
        return take_id, "rejected_stale", []

    return _accept_stocktake(conn, take_id, order, room, staff, cart_id)


def _accept_stocktake(conn, take_id, order, room, staff, cart_id):
    items = conn.execute(
        "SELECT sc.*, p.name, p.unit_price FROM stocktake_count sc "
        "JOIN product p ON p.id=sc.product_id WHERE sc.stocktake_id=?",
        (take_id,)).fetchall()
    _, snap_items = latest_snapshot_items(conn, order["id"])
    prev = {x["product_id"]: x["qty"] for x in snap_items}

    shortages = []
    new_consumptions = []
    for it in items:
        pid = it["product_id"]
        before = prev.get(pid, 0)
        after = it["counted_qty"]
        if after < 0:
            raise MinibarError("盘点数量不能为负")
        cap = conn.execute("SELECT cap_qty FROM display_cap WHERE room_id=? AND product_id=?",
                           (room["id"], pid)).fetchone()
        cap_qty = cap["cap_qty"] if cap else before
        consumed = before - after
        if consumed > 0:
            # 按 FEFO 从房内批次核销消费（效期近的先消耗）
            _consume_from_room(conn, order["id"], room["id"], pid, consumed,
                               it["unit_price"], take_id, staff)
            new_consumptions.append((pid, consumed))
        need = max(0, cap_qty - after)
        if need > 0:
            picks, short = _fefo_pick(conn, pid, need, cart_id)
            for batch_id, q in picks:
                _move_batch_units(conn, batch_id, q, "room", room_id=room["id"])
            if short > 0:
                shortages.append((pid, short))
                _open_case(conn, config.REV_CART_SHORTAGE, order, room,
                           product_id=pid, qty=short, staff=staff,
                           summary=f"补货车商品短缺 {short} 件，无法补足陈列上限")

    new_version = order["snap_version"] + 1
    snap_id = _snapshot_room(conn, order["id"], new_version,
                             order["snap_version"], "stocktake", staff)
    conn.execute("UPDATE stocktake SET status='accepted', final_version=? WHERE id=?",
                 (new_version, take_id))
    conn.commit()
    return take_id, "accepted", shortages


def _consume_from_room(conn, order_id, room_id, product_id, qty, unit_price,
                       take_id, staff):
    """房内按 FEFO 扣减批次并生成一条待确认消费（跨多批也只记一行）。"""
    remaining = qty
    first_batch = None
    rows = conn.execute(
        "SELECT * FROM batch WHERE loc='room' AND room_id=? AND product_id=? AND qty>0 "
        "ORDER BY expiry_date, id", (room_id, product_id)).fetchall()
    for r in rows:
        if remaining <= 0:
            break
        if first_batch is None:
            first_batch = r["id"]
        take = min(r["qty"], remaining)
        if take == r["qty"]:
            conn.execute("UPDATE batch SET qty=0 WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE batch SET qty=qty-? WHERE id=?", (take, r["id"]))
        remaining -= take
    # 账物不符（房内批次不足）也记账，batch_id 留空并挂跨房放错复核
    conn.execute(
        "INSERT INTO consumption (order_id, product_id, batch_id, qty, unit_price, "
        "stocktake_id, detected_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (order_id, product_id, first_batch, qty, unit_price, take_id, clock.iso()))
    if remaining > 0:
        order = _load_order(conn, order_id)
        _open_case(conn, config.REV_WRONG_ROOM, order,
                   conn.execute("SELECT * FROM room WHERE id=?", (room_id,)).fetchone(),
                   product_id=product_id, qty=remaining, staff=staff,
                   summary=(f"账物不符：房内批次不足以扣减 {remaining} 件，"
                            "疑似跨房放错，需核查"))


# --------------------------------------------------------------------- #
# 消费确认 / 否认 / 跨房放错
# --------------------------------------------------------------------- #
def confirm_consumption(conn, consumption_id, staff):
    """前台确认消费 -> 生成 charge 账单行（幂等）。"""
    require_role(staff, config.ROLE_FRONT_DESK, config.ROLE_MANAGER)
    c = conn.execute("SELECT * FROM consumption WHERE id=?", (consumption_id,)).fetchone()
    order = _load_order(conn, c["order_id"])
    can_access_floor(staff, _floor_of(conn, order))
    if c["status"] in ("confirmed",):
        raise IdempotentReplay("该消费已确认")
    if c["status"] in ("waived", "reversed"):
        raise MinibarError("该消费已免单或冲销")
    if order["state"] == "closed":
        raise OrderFrozen("订单已关闭")
    idem = f"charge-c{c['id']}"
    if conn.execute("SELECT 1 FROM bill_line WHERE idem_key=?", (idem,)).fetchone():
        raise IdempotentReplay("账单行已存在")
    amount = round(c["qty"] * c["unit_price"], 2)
    cur = conn.execute(
        "INSERT INTO bill_line (order_id, line_type, amount, reason, idem_key, "
        "ref_type, ref_id, created_by, created_at) VALUES (?, 'charge', ?, ?, ?, "
        "'consumption', ?, ?, ?)",
        (c["order_id"], amount, "消费确认", idem, c["id"], staff["id"], clock.iso()))
    conn.execute("UPDATE consumption SET status='confirmed', bill_line_id=? WHERE id=?",
                 (cur.lastrowid, c["id"]))
    conn.commit()
    return cur.lastrowid


def guest_deny_consumption(conn, consumption_id, staff, note=""):
    """客人否认消费 -> 争议 + guest_denial 复核，账单暂缓。"""
    require_role(staff, config.ROLE_FRONT_DESK, config.ROLE_ATTENDANT, config.ROLE_MANAGER)
    c = conn.execute("SELECT * FROM consumption WHERE id=?", (consumption_id,)).fetchone()
    order = _load_order(conn, c["order_id"])
    can_access_floor(staff, _floor_of(conn, order))
    if c["status"] not in ("pending", "disputed"):
        raise MinibarError("仅待确认消费可提出否认")
    conn.execute("UPDATE consumption SET status='disputed' WHERE id=?", (consumption_id,))
    case_id = _open_case(conn, config.REV_GUEST_DENIAL, order,
                         _room_of(conn, order), consumption_id=c["id"],
                         product_id=c["product_id"], qty=c["qty"], staff=staff,
                         summary=f"客人否认消费 ×{c['qty']}（{note or '待经理核实'}）")
    conn.commit()
    return case_id


def flag_wrong_room(conn, consumption_id, staff, target_room_id, note=""):
    """跨房放错：商品被算到本房，实际消费在 target_room。"""
    require_role(staff, config.ROLE_ATTENDANT, config.ROLE_FRONT_DESK, config.ROLE_MANAGER)
    c = conn.execute("SELECT * FROM consumption WHERE id=?", (consumption_id,)).fetchone()
    order = _load_order(conn, c["order_id"])
    can_access_floor(staff, _floor_of(conn, order))
    target = conn.execute("SELECT * FROM room WHERE id=?", (target_room_id,)).fetchone()
    case_id = _open_case(conn, config.REV_WRONG_ROOM, order, _room_of(conn, order),
                         consumption_id=c["id"], product_id=c["product_id"],
                         qty=c["qty"], staff=staff,
                         room_id=target_room_id,
                         summary=(f"疑似跨房放错：本房消费 ×{c['qty']} 实际归属 "
                                  f"{target['room_no']}（{note}）"))
    conn.execute("UPDATE consumption SET status='disputed' WHERE id=?", (consumption_id,))
    conn.commit()
    return case_id


def resolve_wrong_room(conn, case_id, staff, note=""):
    """经理裁定跨房放错：冲销原房账单/消费，在目标房订单补 charge。"""
    require_role(staff, config.ROLE_MANAGER)
    case = conn.execute("SELECT * FROM review_case WHERE id=? AND kind='wrong_room'",
                        (case_id,)).fetchone()
    c = conn.execute("SELECT * FROM consumption WHERE id=?", (case["consumption_id"],)).fetchone()
    target_order = conn.execute(
        "SELECT * FROM stay_order WHERE room_id=? AND state!='closed' "
        "ORDER BY id DESC LIMIT 1", (case["room_id"],)).fetchone()
    if c["bill_line_id"]:
        reverse_bill_line(conn, c["bill_line_id"], staff, reason="跨房放错更正")
    conn.execute("UPDATE consumption SET status='reversed' WHERE id=?", (c["id"],))
    if target_order:
        amount = round(c["qty"] * c["unit_price"], 2)
        conn.execute(
            "INSERT INTO bill_line (order_id, line_type, amount, reason, idem_key, "
            "ref_type, ref_id, created_by, created_at) VALUES (?, 'charge', ?, ?, ?, "
            "'manual', ?, ?, ?)",
            (target_order["id"], amount, f"跨房放错转入：{note}",
             f"charge-wrongroom-c{c['id']}", c["id"], staff["id"], clock.iso()))
    _resolve_case(conn, case_id, staff,
                  f"已转至房 {target_order['order_no'] if target_order else '?'}：{note}")
    conn.commit()


# --------------------------------------------------------------------- #
# 退房冻结 / 账单 / 免单 / 冲销（经理 + 原因 + 幂等）
# --------------------------------------------------------------------- #
def freeze_checkout(conn, order_id, staff):
    """退房冻结：锁住最后一次盘点快照，等待前台确认账单。"""
    require_role(staff, config.ROLE_FRONT_DESK, config.ROLE_MANAGER)
    order = _load_order(conn, order_id)
    can_access_floor(staff, _floor_of(conn, order))
    if order["state"] == "frozen":
        raise IdempotentReplay("订单已冻结")
    if order["state"] == "closed":
        raise OrderFrozen("订单已关闭")
    conn.execute(
        "UPDATE stay_order SET state='frozen', frozen_at=?, frozen_by=? WHERE id=?",
        (clock.iso(), staff["id"], order_id))
    conn.execute("UPDATE room SET status='checkout_pending', pms_status='OD' WHERE id=?",
                 (order["room_id"],))
    conn.commit()


def waive_consumption(conn, consumption_id, staff, reason, note=""):
    """免单：经理权限 + 留存原因；产生负向 waiver 账单行。"""
    require_role(staff, config.ROLE_MANAGER)
    if reason not in config.WAIVER_REASONS:
        raise MinibarError("免单原因不合法")
    c = conn.execute("SELECT * FROM consumption WHERE id=?", (consumption_id,)).fetchone()
    order = _load_order(conn, c["order_id"])
    if c["status"] == "waived":
        raise IdempotentReplay("已免单")
    if c["status"] == "reversed":
        raise MinibarError("已冲销消费不能免单")
    idem = f"waiver-c{c['id']}"
    if conn.execute("SELECT 1 FROM bill_line WHERE idem_key=?", (idem,)).fetchone():
        raise IdempotentReplay("免单已存在")
    # 仅冲抵已入账 charge；消费尚未确认时免单金额记 0（原因仍留存账单时间线）
    charged = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM bill_line WHERE ref_type='consumption' "
        "AND ref_id=? AND line_type='charge'", (c["id"],)).fetchone()["t"]
    amount = -round(min(charged, c["qty"] * c["unit_price"]), 2)
    cur = conn.execute(
        "INSERT INTO bill_line (order_id, line_type, amount, reason, idem_key, "
        "ref_type, ref_id, created_by, created_at) VALUES (?, 'waiver', ?, ?, ?, "
        "'consumption', ?, ?, ?)",
        (c["order_id"], amount, f"{reason}:{note}", idem, c["id"], staff["id"], clock.iso()))
    conn.execute("UPDATE consumption SET status='waived', bill_line_id=? WHERE id=?",
                 (cur.lastrowid, c["id"]))
    # 联动关闭可能存在的争议复核
    case = conn.execute(
        "SELECT id FROM review_case WHERE consumption_id=? AND status IN ('open','in_review')",
        (consumption_id,)).fetchone()
    if case:
        _resolve_case(conn, case["id"], staff, f"经理免单：{reason} {note}")
    conn.commit()
    return cur.lastrowid


def reverse_bill_line(conn, bill_line_id, staff, reason):
    """账单冲销：经理 + 原因；对同一行重复冲销幂等（idem_key 固定）。"""
    require_role(staff, config.ROLE_MANAGER)
    if not reason or not reason.strip():
        raise MinibarError("冲销必须留存原因")
    line = conn.execute("SELECT * FROM bill_line WHERE id=?", (bill_line_id,)).fetchone()
    if line is None:
        raise MinibarError("账单行不存在")
    if line["line_type"] == "reversal":
        raise MinibarError("不能冲销一条冲销记录")
    idem = f"reverse-l{bill_line_id}"
    dup = conn.execute("SELECT * FROM bill_line WHERE idem_key=?", (idem,)).fetchone()
    if dup:
        # 冲销幂等：直接返回既有冲销行
        return dup["id"]
    cur = conn.execute(
        "INSERT INTO bill_line (order_id, line_type, amount, reason, idem_key, "
        "reversal_of, ref_type, ref_id, created_by, created_at) "
        "VALUES (?, 'reversal', ?, ?, ?, ?, 'bill_line', ?, ?, ?)",
        (line["order_id"], -round(line["amount"], 2), f"冲销：{reason}", idem,
         bill_line_id, bill_line_id, staff["id"], clock.iso()))
    if line["ref_type"] == "consumption":
        conn.execute("UPDATE consumption SET status='reversed' WHERE id=?",
                     (line["ref_id"],))
    conn.commit()
    return cur.lastrowid


def post_checkout_add_charge(conn, order_id, product_id, qty, staff, note):
    """退房后补录：订单冻结时只能进 post_checkout 复核，经理核准后才入账单。"""
    require_role(staff, config.ROLE_FRONT_DESK, config.ROLE_ATTENDANT, config.ROLE_MANAGER)
    order = _load_order(conn, order_id)
    can_access_floor(staff, _floor_of(conn, order))
    if order["state"] != "frozen":
        raise MinibarError("退房后补录仅适用于冻结订单")
    p = conn.execute("SELECT * FROM product WHERE id=?", (product_id,)).fetchone()
    case_id = _open_case(conn, config.REV_POST_CHECKOUT, order, _room_of(conn, order),
                         product_id=product_id, qty=qty, staff=staff,
                         summary=f"退房后补录 {p['name']} ×{qty}（{note}），待经理核准")
    conn.commit()
    return case_id


def approve_post_checkout(conn, case_id, staff):
    """经理核准备录：冻结状态下追加账单行（幂等）。"""
    require_role(staff, config.ROLE_MANAGER)
    case = conn.execute("SELECT * FROM review_case WHERE id=? AND kind='post_checkout'",
                        (case_id,)).fetchone()
    if case["stocktake_id"]:
        # 盘点型补录：将该离线盘点按当前版本差异补账（简化：按单中 pending 消费核准）
        take = conn.execute("SELECT * FROM stocktake WHERE id=?",
                            (case["stocktake_id"],)).fetchone()
        # 不重放库存移动，仅把当时生成在复核单上的商品补录；演示数据走显式补录
        pass
    p = conn.execute("SELECT * FROM product WHERE id=?", (case["product_id"],)).fetchone()
    idem = f"postcheckout-case{case['id']}"
    if not conn.execute("SELECT 1 FROM bill_line WHERE idem_key=?", (idem,)).fetchone():
        amount = round(case["qty"] * p["unit_price"], 2)
        conn.execute(
            "INSERT INTO bill_line (order_id, line_type, amount, reason, idem_key, "
            "ref_type, ref_id, created_by, created_at) VALUES (?, 'charge', ?, ?, ?, "
            "'review_case', ?, ?, ?)",
            (case["order_id"], amount, f"退房后补录核准：{case['summary']}", idem,
             case["id"], staff["id"], clock.iso()))
    _resolve_case(conn, case_id, staff, "经理核准，已追加账单")
    conn.commit()


def settle_and_close(conn, order_id, staff, adapter):
    """前台确认账单 -> 模拟结算通道 -> 关闭订单。仅冻结订单可结算。"""
    require_role(staff, config.ROLE_FRONT_DESK, config.ROLE_MANAGER)
    order = _load_order(conn, order_id)
    can_access_floor(staff, _floor_of(conn, order))
    if order["state"] != "frozen":
        raise OrderFrozen("只有退房冻结的订单才能确认结算")
    # 仍有未决消费/复核则阻止关闭
    pending = conn.execute(
        "SELECT COUNT(*) c FROM consumption WHERE order_id=? AND status IN "
        "('pending','disputed')", (order_id,)).fetchone()["c"]
    if pending:
        raise MinibarError("仍有消费待确认或争议未处理，不能关闭订单")
    open_cases = conn.execute(
        "SELECT COUNT(*) c FROM review_case WHERE order_id=? AND status IN "
        "('open','in_review')", (order_id,)).fetchone()["c"]
    if open_cases:
        raise MinibarError("仍有复核单未结案，不能关闭订单")
    total = bill_total(conn, order_id)
    result = adapter.settle(order["order_no"], total)
    conn.execute(
        "INSERT INTO settlement (order_id, settlement_no, amount, channel, ok, raw, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, result["settlement_no"], total, result["channel"],
         1 if result["ok"] else 0, result.get("raw", ""), clock.iso()))
    conn.execute(
        "UPDATE stay_order SET state='closed', check_out_at=?, closed_at=? WHERE id=?",
        (clock.iso(), clock.iso(), order_id))
    conn.execute("UPDATE room SET status='vacant', pms_status='VC' WHERE id=?",
                 (order["room_id"],))
    conn.commit()
    return result


# --------------------------------------------------------------------- #
# 查询视图
# --------------------------------------------------------------------- #
def bill_total(conn, order_id):
    r = conn.execute("SELECT COALESCE(SUM(amount),0) t FROM bill_line WHERE order_id=?",
                     (order_id,)).fetchone()
    return round(r["t"], 2)


def bill_lines(conn, order_id):
    rows = conn.execute(
        "SELECT bl.*, s.name AS staff_name FROM bill_line bl "
        "LEFT JOIN staff s ON s.id=bl.created_by WHERE bl.order_id=? ORDER BY bl.id",
        (order_id,)).fetchall()
    return [dict(r) for r in rows]


def order_timeline(conn, order_id):
    """争议消费按账单时间线展开：事件（消费/确认/免单/冲销/冻结/结算）。"""
    events = []
    for c in conn.execute(
            "SELECT c.*, p.name AS product_name FROM consumption c "
            "JOIN product p ON p.id=c.product_id WHERE c.order_id=? ORDER BY c.id",
            (order_id,)).fetchall():
        events.append({
            "at": c["detected_at"], "kind": f"consumption:{c['status']}",
            "title": f"盘点形成消费 · {c['product_name']} ×{c['qty']}",
            "amount": round(c["qty"] * c["unit_price"], 2), "status": c["status"],
            "ref_id": c["id"]})
    for l in bill_lines(conn, order_id):
        events.append({
            "at": l["created_at"], "kind": f"bill:{l['line_type']}",
            "title": {"charge": "入账", "waiver": "经理免单",
                      "reversal": "经理冲销", "writeoff": "报损"}.get(l["line_type"], l["line_type"])
                     + f" · {l['reason']}",
            "amount": round(l["amount"], 2), "status": l["line_type"],
            "ref_id": l["id"]})
    order = _load_order(conn, order_id)
    if order["frozen_at"]:
        events.append({"at": order["frozen_at"], "kind": "frozen",
                       "title": "退房冻结 · 锁定最后盘点", "amount": None, "status": "frozen"})
    st = conn.execute("SELECT * FROM settlement WHERE order_id=? ORDER BY id DESC LIMIT 1",
                      (order_id,)).fetchone()
    if st:
        events.append({"at": st["created_at"], "kind": "settled",
                       "title": f"前台确认账单 · 结算 {st['settlement_no']}",
                       "amount": round(st["amount"], 2), "status": "settled"})
    events.sort(key=lambda e: (e["at"], e.get("ref_id") or 0))
    return events


def floor_room_list(conn, staff, floor=None):
    """楼层房态紧凑列表（含在住订单、未决消费/复核、快照版本）。"""
    sql = ("SELECT r.*, o.id AS order_id, o.order_no, o.guest_alias, o.state AS order_state, "
           "o.snap_version, o.frozen_at FROM room r "
           "LEFT JOIN stay_order o ON o.room_id=r.id AND o.state!='closed' "
           "WHERE 1=1")
    args = []
    if floor is not None:
        sql += " AND r.floor=?"; args.append(floor)
    if staff["floor_scope"] != "*":
        allowed = ",".join("?" * len([x for x in staff["floor_scope"].split(",") if x.strip()]))
        sql += f" AND r.floor IN ({allowed})"
        args += [int(x) for x in staff["floor_scope"].split(",") if x.strip()]
    sql += " ORDER BY r.floor, r.room_no"
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        if r["order_id"]:
            d["pending_consumptions"] = conn.execute(
                "SELECT COUNT(*) c FROM consumption WHERE order_id=? AND status IN "
                "('pending','disputed')", (r["order_id"],)).fetchone()["c"]
            d["open_cases"] = conn.execute(
                "SELECT COUNT(*) c FROM review_case WHERE order_id=? AND status IN "
                "('open','in_review')", (r["order_id"],)).fetchone()["c"]
            d["bill_total"] = bill_total(conn, r["order_id"])
        else:
            d["pending_consumptions"] = d["open_cases"] = 0
            d["bill_total"] = 0.0
        out.append(d)
    return out


def expiring_batches(conn, days=None, include_expired=True):
    """效期看板：临期与已过期（车上+房内），破损单列。"""
    days = config.EXPIRY_WARN_DAYS if days is None else days
    today = clock.today()
    rows = conn.execute(
        "SELECT b.*, p.name AS product_name, r.room_no FROM batch b "
        "JOIN product p ON p.id=b.product_id "
        "LEFT JOIN room r ON r.id=b.room_id "
        "WHERE b.loc!='writeoff' AND b.qty>0 ORDER BY b.expiry_date").fetchall()
    out = []
    for r in rows:
        d = days_between(r["expiry_date"], today)
        if d < 0 and include_expired:
            tag = "expired"
        elif 0 <= d <= days:
            tag = "expiring"
        else:
            continue
        out.append({**dict(r), "days_to_expiry": d, "tag": tag})
    return out


# --------------------------------------------------------------------- #
# 复核单
# --------------------------------------------------------------------- #
def _new_case_no(conn):
    n = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM review_case").fetchone()[0]
    return f"RC-{n:04d}"


def _open_case(conn, kind, order, room, staff, summary, consumption_id=None,
               product_id=None, qty=None, stocktake_id=None, room_id=None):
    cur = conn.execute(
        "INSERT INTO review_case (case_no, kind, order_id, room_id, consumption_id, "
        "stocktake_id, product_id, qty, summary, status, opened_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        (_new_case_no(conn), kind, order["id"] if order else None,
         room_id or (room["id"] if room else None), consumption_id, stocktake_id,
         product_id, qty, summary, staff["id"], clock.iso()))
    return cur.lastrowid


def _resolve_case(conn, case_id, staff, resolution):
    conn.execute(
        "UPDATE review_case SET status='resolved', resolution=?, resolved_by=?, "
        "resolved_at=? WHERE id=?", (resolution, staff["id"], clock.iso(), case_id))


def list_cases(conn, status=None, kind=None):
    sql = ("SELECT rc.*, r.room_no, p.name AS product_name, s.name AS opened_by_name "
           "FROM review_case rc LEFT JOIN room r ON r.id=rc.room_id "
           "LEFT JOIN product p ON p.id=rc.product_id "
           "LEFT JOIN staff s ON s.id=rc.opened_by WHERE 1=1")
    args = []
    if status:
        sql += " AND rc.status=?"; args.append(status)
    if kind:
        sql += " AND rc.kind=?"; args.append(kind)
    sql += " ORDER BY rc.id DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def resolve_case_generic(conn, case_id, staff, resolution, reject=False):
    """通用结案（经理）；batch_expiring 通常由报损联动。"""
    require_role(staff, config.ROLE_MANAGER)
    _resolve_case(conn, case_id, staff, ("驳回：" if reject else "结案：") + resolution)
    conn.commit()


# --------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------- #
def _load_order(conn, order_id):
    o = conn.execute("SELECT * FROM stay_order WHERE id=?", (order_id,)).fetchone()
    if o is None:
        raise MinibarError("订单不存在")
    return o


def _room_of(conn, order):
    return conn.execute("SELECT * FROM room WHERE id=?", (order["room_id"],)).fetchone()


def _floor_of(conn, order):
    return conn.execute("SELECT floor FROM room WHERE id=?", (order["room_id"],)).fetchone()["floor"]


def staff_by_code(conn, code):
    return conn.execute("SELECT * FROM staff WHERE code=?", (code,)).fetchone()
