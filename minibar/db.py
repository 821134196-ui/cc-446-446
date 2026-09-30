"""SQLite 连接与 schema：库存版本链 + 账单 + 复核流程。

设计要点
--------
* stock_snapshot 为不可变版本（version 自增、parent_version 串起版本链），
  盘点提交只做乐观锁校验（expected_version 必须等于当前版本），旧版本不得覆盖。
* stocktake 以 idem_key 支持离线盘点重传去重；被拒的旧版本提交落 offline_resend 复核。
* bill_line 以 idem_key 保证冲销/重放幂等；reversal_of 指向被冲销行。
* 批次位置 cart -> room -> writeoff：writeoff 终态只能由过期/破损进入，不可再上架。
* stay_order.frozen_at 非空即退房冻结，冻结后只能由前台确认账单关闭订单；
  冻结后补录消费走 post_checkout 复核。
"""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 员工（匿名演示账号）----------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,      -- 工号
    name        TEXT NOT NULL,             -- 演示化名
    role        TEXT NOT NULL,             -- manager / front_desk / attendant
    floor_scope TEXT NOT NULL DEFAULT '*'  -- 可访问楼层，逗号分隔，'*' 为全部
);

-- 客房 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS room (
    id         INTEGER PRIMARY KEY,
    room_no    TEXT NOT NULL UNIQUE,
    floor      INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'vacant', -- vacant / occupied / checkout_pending
    pms_status TEXT NOT NULL DEFAULT 'VC'       -- 模拟 PMS 房态码
);
CREATE INDEX IF NOT EXISTS idx_room_floor ON room(floor);

-- 商品 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product (
    id          INTEGER PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    unit_price  REAL NOT NULL,              -- 售价
    cost_price  REAL NOT NULL DEFAULT 0,
    image       TEXT NOT NULL DEFAULT ''
);

-- 每个客房、每种商品的陈列上限 --------------------------------------
CREATE TABLE IF NOT EXISTS display_cap (
    room_id    INTEGER NOT NULL REFERENCES room(id),
    product_id INTEGER NOT NULL REFERENCES product(id),
    cap_qty    INTEGER NOT NULL CHECK (cap_qty >= 0),
    PRIMARY KEY (room_id, product_id)
);

-- 批次 -------------------------------------------------------------
-- loc: cart（补货车）/ room（客房）/ writeoff（报损终态）
CREATE TABLE IF NOT EXISTS batch (
    id            INTEGER PRIMARY KEY,
    batch_no      TEXT NOT NULL UNIQUE,
    product_id    INTEGER NOT NULL REFERENCES product(id),
    qty           INTEGER NOT NULL DEFAULT 0,  -- 当前余量
    initial_qty   INTEGER NOT NULL,
    expiry_date   TEXT NOT NULL,               -- ISO 日期
    loc           TEXT NOT NULL,
    room_id       INTEGER REFERENCES room(id), -- loc=room 时所在客房
    cart_id       INTEGER,                     -- loc=cart 时补货车编号
    writeoff_reason TEXT,                      -- expired / damaged
    damaged        INTEGER NOT NULL DEFAULT 0, -- 包装破损标记（车上/房内发现，待报损）
    source_batch_id INTEGER REFERENCES batch(id), -- 拆批来源批次
    written_off_at TEXT,
    created_at    TEXT NOT NULL,
    CHECK (loc IN ('cart', 'room', 'writeoff'))
);
CREATE INDEX IF NOT EXISTS idx_batch_loc_room ON batch(loc, room_id);
CREATE INDEX IF NOT EXISTS idx_batch_cart ON batch(cart_id) WHERE loc = 'cart';
CREATE INDEX IF NOT EXISTS idx_batch_expiry ON batch(expiry_date);

-- 入住订单（匿名演示）-----------------------------------------------
CREATE TABLE IF NOT EXISTS stay_order (
    id            INTEGER PRIMARY KEY,
    order_no      TEXT NOT NULL UNIQUE,
    room_id       INTEGER NOT NULL REFERENCES room(id),
    guest_alias   TEXT NOT NULL,             -- 匿名代号，如 GUEST-7F3A
    check_in_at   TEXT NOT NULL,
    check_out_at  TEXT,
    state         TEXT NOT NULL DEFAULT 'open',
        -- open（在住）/ frozen（退房冻结）/ closed（已关闭）
    snap_version  INTEGER NOT NULL DEFAULT 0,-- 房内库存当前版本
    frozen_at     TEXT,
    frozen_by     INTEGER REFERENCES staff(id),
    closed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_stay_room ON stay_order(room_id);

-- 不可变库存快照版本 -------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_snapshot (
    id             INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES stay_order(id),
    version        INTEGER NOT NULL,
    parent_version INTEGER,
    kind           TEXT NOT NULL,            -- check_in / stocktake
    created_at     TEXT NOT NULL,
    created_by     INTEGER REFERENCES staff(id),
    UNIQUE (order_id, version)
);

-- 快照行：版本固化时每种商品的数量与批次组成 -------------------------
CREATE TABLE IF NOT EXISTS stock_snapshot_item (
    snapshot_id INTEGER NOT NULL REFERENCES stock_snapshot(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES product(id),
    qty         INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, product_id)
);

-- 盘点单 -------------------------------------------------------------
-- status: accepted（已按版本提交）/ rejected_stale（旧版本，被拒并转复核）/
--         pending_resend（离线暂存，尚未并入）
CREATE TABLE IF NOT EXISTS stocktake (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES stay_order(id),
    seq              INTEGER NOT NULL,        -- 客房内第几次盘点
    expected_version INTEGER NOT NULL,        -- 服务员基于的快照版本
    final_version    INTEGER,                 -- 成功提交后的新版本
    status           TEXT NOT NULL DEFAULT 'pending_resend',
    idem_key         TEXT NOT NULL UNIQUE,    -- 离线重传幂等键
    counted_at       TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    submitted_by     INTEGER REFERENCES staff(id),
    note             TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_take_order ON stocktake(order_id);

CREATE TABLE IF NOT EXISTS stocktake_count (
    stocktake_id INTEGER NOT NULL REFERENCES stocktake(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES product(id),
    counted_qty  INTEGER NOT NULL,
    PRIMARY KEY (stocktake_id, product_id)
);

-- 消费 -----------------------------------------------------------------
-- status: pending（待确认）/ confirmed / disputed / waived（免单）/
--         reversed（被冲销）
CREATE TABLE IF NOT EXISTS consumption (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES stay_order(id),
    product_id    INTEGER NOT NULL REFERENCES product(id),
    batch_id      INTEGER REFERENCES batch(id),
    qty           INTEGER NOT NULL,
    unit_price    REAL NOT NULL,
    stocktake_id  INTEGER REFERENCES stocktake(id),
    detected_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    bill_line_id  INTEGER,
    UNIQUE (order_id, product_id, stocktake_id)
);
CREATE INDEX IF NOT EXISTS idx_cons_order ON consumption(order_id);

-- 账单行（幂等）。line_type: charge / waiver / reversal -----------------
CREATE TABLE IF NOT EXISTS bill_line (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER REFERENCES stay_order(id),
    line_type   TEXT NOT NULL,               -- charge / waiver / reversal
    amount      REAL NOT NULL,               -- 带符号：charge +, waiver/reversal -
    reason      TEXT NOT NULL DEFAULT '',
    idem_key    TEXT NOT NULL UNIQUE,
    reversal_of INTEGER REFERENCES bill_line(id),
    ref_type    TEXT NOT NULL DEFAULT '',    -- consumption / writeoff / manual
    ref_id      INTEGER,
    created_by  INTEGER REFERENCES staff(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bill_order ON bill_line(order_id);

-- 复核单 -----------------------------------------------------------------
-- kind: guest_denial / wrong_room / batch_expiring / cart_shortage /
--       offline_resend / post_checkout
-- status: open / in_review / resolved / rejected
CREATE TABLE IF NOT EXISTS review_case (
    id            INTEGER PRIMARY KEY,
    case_no       TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    order_id      INTEGER REFERENCES stay_order(id),
    room_id       INTEGER REFERENCES room(id),
    consumption_id INTEGER REFERENCES consumption(id),
    stocktake_id  INTEGER REFERENCES stocktake(id),
    batch_id      INTEGER REFERENCES batch(id),
    product_id    INTEGER REFERENCES product(id),
    qty           INTEGER,
    summary       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    resolution    TEXT DEFAULT '',
    opened_by     INTEGER REFERENCES staff(id),
    resolved_by   INTEGER REFERENCES staff(id),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_case_status ON review_case(status, kind);

-- 包装破损照片登记（文件落本地 PHOTO_DIR）--------------------------------
CREATE TABLE IF NOT EXISTS photo (
    id         INTEGER PRIMARY KEY,
    filename   TEXT NOT NULL UNIQUE,
    batch_id   INTEGER REFERENCES batch(id),
    room_id    INTEGER REFERENCES room(id),
    uploaded_by INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL,
    note       TEXT DEFAULT ''
);

-- 模拟适配器返回的结算结果 ----------------------------------------------
CREATE TABLE IF NOT EXISTS settlement (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES stay_order(id),
    settlement_no TEXT NOT NULL UNIQUE,
    amount        REAL NOT NULL,
    channel       TEXT NOT NULL DEFAULT 'MOCK-PSC', -- 模拟结算通道
    ok            INTEGER NOT NULL DEFAULT 1,
    raw           TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

-- 进程内提醒发送记录（临期 / 未盘）---------------------------------------
CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                -- expiring / not_stocktaken
    ref_key    TEXT NOT NULL,                -- 去重键，如 batch:12 / room:3:2026-09-30
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, ref_key)
);
"""


def connect(db_path=None):
    conn = sqlite3.connect(str(db_path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(conn):
    """简单事务上下文。"""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
