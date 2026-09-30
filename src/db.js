'use strict';

const { DatabaseSync } = require('node:sqlite');
const path = require('node:path');
const fs = require('node:fs');

const SCHEMA = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 员工 / 权限（楼层范围：NULL 表示全部楼层）
CREATE TABLE IF NOT EXISTS staff(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('manager','frontdesk','attendant')),
  floor_scope TEXT,              -- JSON 数组，如 [3,4]
  active INTEGER NOT NULL DEFAULT 1
);

-- 客房（status 由模拟 PMS 适配器同步）
CREATE TABLE IF NOT EXISTS rooms(
  id TEXT PRIMARY KEY,
  floor INTEGER NOT NULL,
  label TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'vacant'
    CHECK(status IN ('vacant','occupied','pending_checkout','disputed','maintenance')),
  current_order_id TEXT
);

CREATE TABLE IF NOT EXISTS products(
  id TEXT PRIMARY KEY,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  image TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  display_cap INTEGER NOT NULL,          -- 陈列上限
  active INTEGER NOT NULL DEFAULT 1
);

-- 批次（FIFO 按 expires_at 排序）
CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL REFERENCES products(id),
  lot_no TEXT NOT NULL,
  received_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  qty_total INTEGER NOT NULL,
  qty_remaining INTEGER NOT NULL,       -- 未上架且未报损的余量（在补货车上）
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active','expired','damaged','written_off')),
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_product ON batches(product_id, expires_at);

-- 补货车库存（按批次）
CREATE TABLE IF NOT EXISTS cart_stock(
  product_id TEXT NOT NULL REFERENCES products(id),
  batch_id TEXT NOT NULL REFERENCES batches(id),
  qty INTEGER NOT NULL CHECK(qty >= 0),
  PRIMARY KEY(product_id, batch_id)
);

-- 房内库存（按批次，用于先进先出核销）
CREATE TABLE IF NOT EXISTS room_stock(
  room_id TEXT NOT NULL REFERENCES rooms(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  batch_id TEXT NOT NULL REFERENCES batches(id),
  qty INTEGER NOT NULL CHECK(qty >= 0),
  PRIMARY KEY(room_id, product_id, batch_id)
);

-- 入住订单（匿名客人）
CREATE TABLE IF NOT EXISTS stay_orders(
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id),
  guest_anon TEXT NOT NULL,
  check_in_at TEXT NOT NULL,
  scheduled_checkout_at TEXT,
  status TEXT NOT NULL DEFAULT 'checked_in'
    CHECK(status IN ('checked_in','frozen','closed')),
  frozen_count_id TEXT,
  settled_at TEXT,
  settlement_ref TEXT,
  created_at TEXT NOT NULL
);

-- 盘点单：同一客房 version 单调递增
CREATE TABLE IF NOT EXISTS inventory_counts(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES stay_orders(id),
  room_id TEXT NOT NULL REFERENCES rooms(id),
  version INTEGER NOT NULL,
  client_guid TEXT,                    -- 离线重传去重
  state TEXT NOT NULL DEFAULT 'submitted'
    CHECK(state IN ('submitted','superseded','locked')),
  staff_id TEXT NOT NULL,
  device TEXT,
  counted_at TEXT NOT NULL,           -- 服务员实际盘点时间（可能离线）
  submitted_at TEXT NOT NULL,
  note TEXT,
  UNIQUE(room_id, version)
);
CREATE INDEX IF NOT EXISTS idx_counts_order ON inventory_counts(order_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_counts_guid ON inventory_counts(client_guid)
  WHERE client_guid IS NOT NULL;

CREATE TABLE IF NOT EXISTS count_lines(
  id TEXT PRIMARY KEY,
  count_id TEXT NOT NULL REFERENCES inventory_counts(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  expected_qty INTEGER NOT NULL,
  counted_qty INTEGER NOT NULL,
  replenish_qty INTEGER NOT NULL DEFAULT 0,
  UNIQUE(count_id, product_id)
);

-- 待确认 / 已确认消费
CREATE TABLE IF NOT EXISTS consumption_items(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES stay_orders(id),
  count_id TEXT NOT NULL REFERENCES inventory_counts(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  batch_id TEXT REFERENCES batches(id),
  qty INTEGER NOT NULL,
  unit_price_cents INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','confirmed','denied','comp','relocated','written_off')),
  reason_code TEXT,
  note TEXT,
  review_flow TEXT
    CHECK(review_flow IS NULL OR review_flow IN
      ('guest_deny','cross_room','near_expiry','cart_shortage','offline_reupload','post_checkout')),
  charged_order_id TEXT REFERENCES stay_orders(id),  -- 跨房放错：计入另一订单
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_cons_order ON consumption_items(order_id, status);

-- 账单（带符号：消费为正，冲销为负；幂等键唯一）
CREATE TABLE IF NOT EXISTS bill_charges(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES stay_orders(id),
  consumption_id TEXT REFERENCES consumption_items(id),
  kind TEXT NOT NULL CHECK(kind IN ('minibar','comp','reversal','adjustment')),
  amount_cents INTEGER NOT NULL,
  reason TEXT,
  created_by TEXT,
  idempotency_key TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(order_id, idempotency_key)
);

-- 免单原因字典
CREATE TABLE IF NOT EXISTS comp_reasons(
  code TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

-- 报损（过期 / 包装破损，只能报损不能重新上架）
CREATE TABLE IF NOT EXISTS write_offs(
  id TEXT PRIMARY KEY,
  batch_id TEXT REFERENCES batches(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  qty INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK(reason IN ('expired','damaged')),
  source TEXT NOT NULL CHECK(source IN ('cart','room')),
  room_id TEXT REFERENCES rooms(id),
  photo TEXT,
  staff_id TEXT NOT NULL,
  note TEXT,
  at TEXT NOT NULL
);

-- 包装照片登记（文件存本地 photos 目录）
CREATE TABLE IF NOT EXISTS photos(
  id TEXT PRIMARY KEY,
  ref_type TEXT NOT NULL,
  ref_id TEXT,
  filename TEXT NOT NULL,
  content_type TEXT,
  uploaded_at TEXT NOT NULL
);

-- 复核流程工单
CREATE TABLE IF NOT EXISTS reviews(
  id TEXT PRIMARY KEY,
  flow TEXT NOT NULL CHECK(flow IN
    ('guest_deny','cross_room','near_expiry','cart_shortage','offline_reupload','post_checkout')),
  consumption_id TEXT REFERENCES consumption_items(id),
  order_id TEXT NOT NULL REFERENCES stay_orders(id),
  state TEXT NOT NULL DEFAULT 'open'
    CHECK(state IN ('open','resolved','rejected')),
  opened_by TEXT,
  assigned_role TEXT NOT NULL DEFAULT 'manager',
  note TEXT,
  result TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_state ON reviews(state, flow);

-- 进程内提醒（临期 / 未盘）
CREATE TABLE IF NOT EXISTS notifications(
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL CHECK(type IN ('near_expiry','uncounted')),
  message TEXT NOT NULL,
  ref TEXT,
  created_at TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0
);

-- 审计（经理权限动作必须留存原因）
CREATE TABLE IF NOT EXISTS audit_log(
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  staff_id TEXT,
  action TEXT NOT NULL,
  detail TEXT
);
`;

function open(dbPath) {
  if (dbPath !== ':memory:') {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  }
  const db = new DatabaseSync(dbPath);
  db.exec(SCHEMA);
  return db;
}

module.exports = { open };
