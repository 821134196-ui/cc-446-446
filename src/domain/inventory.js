'use strict';

const { uid, now, daysUntil } = require('../util');
const { HttpError } = require('../util');

const NEAR_EXPIRY_DAYS = 3;

/** 收货入补货车，生成批次 */
function receiveBatch(db, { productId, lotNo, expiresAt, qty, note }) {
  const product = db.prepare('SELECT * FROM products WHERE id = ? AND active = 1').get(productId);
  if (!product) throw new HttpError(404, '商品不存在', 'unknown_product');
  if (qty <= 0) throw new HttpError(400, '入库数量必须大于 0', 'bad_qty');

  const id = uid('bat');
  db.prepare(
    `INSERT INTO batches(id, product_id, lot_no, received_at, expires_at, qty_total, qty_remaining, note)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(id, productId, lotNo, now(), expiresAt, qty, qty, note || null);
  db.prepare('INSERT INTO cart_stock(product_id, batch_id, qty) VALUES (?, ?, ?)').run(productId, id, qty);
  return getBatch(db, id);
}

function getBatch(db, id) {
  return db.prepare('SELECT * FROM batches WHERE id = ?').get(id);
}

/**
 * 补货车上可上架的批次：状态为 active 且未过效期（FIFO 按效期最早优先）。
 * 已过期 / 包装破损 / 仅状态未更新但效期已过的批次都不得再次上架。
 */
function cartBatches(db, productId) {
  return db.prepare(
    `SELECT b.*, c.qty AS cart_qty
       FROM cart_stock c JOIN batches b ON b.id = c.batch_id
      WHERE c.product_id = ? AND c.qty > 0
        AND b.status = 'active'
        AND datetime(b.expires_at) > datetime('now')
      ORDER BY b.expires_at ASC, b.id ASC`
  ).all(productId);
}

function roomBatches(db, roomId, productId) {
  return db.prepare(
    `SELECT b.*, r.qty AS room_qty
       FROM room_stock r JOIN batches b ON b.id = r.batch_id
      WHERE r.room_id = ? AND r.product_id = ? AND r.qty > 0
      ORDER BY b.expires_at ASC, b.id ASC`
  ).all(roomId, productId);
}

function roomProductQty(db, roomId, productId) {
  const row = db
    .prepare('SELECT COALESCE(SUM(qty),0) AS q FROM room_stock WHERE room_id = ? AND product_id = ?')
    .get(roomId, productId);
  return row.q;
}

/**
 * 从房内库存按 FIFO 扣减，返回实际扣减明细。
 * 效期最早的批次先核销。
 */
function deductRoomFIFO(db, roomId, productId, qty) {
  const taken = [];
  let remaining = qty;
  for (const b of roomBatches(db, roomId, productId)) {
    if (remaining <= 0) break;
    const take = Math.min(b.room_qty, remaining);
    db.prepare('UPDATE room_stock SET qty = qty - ? WHERE room_id = ? AND product_id = ? AND batch_id = ?')
      .run(take, roomId, productId, b.id);
    taken.push({ batchId: b.id, lotNo: b.lot_no, qty: take, expiresAt: b.expires_at });
    remaining -= take;
  }
  if (remaining > 0) {
    throw new HttpError(409, `房内库存不足，无法核销 ${remaining} 件`, 'stock_mismatch');
  }
  return taken;
}

/**
 * 从补货车 FIFO 上架到房间。已过期 / 包装破损批次不会被选取（status != active）。
 * 返回 { transfers, shortage, nearExpiry }。
 */
function replenishFromCart(db, roomId, productId, requested) {
  const transfers = [];
  const nearExpiry = [];
  let remaining = requested;
  for (const b of cartBatches(db, productId)) {
    if (remaining <= 0) break;
    const take = Math.min(b.cart_qty, remaining);
    db.prepare('UPDATE cart_stock SET qty = qty - ? WHERE product_id = ? AND batch_id = ?')
      .run(take, productId, b.id);
    db.prepare('UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ?').run(take, b.id);
    db.prepare(
      `INSERT INTO room_stock(room_id, product_id, batch_id, qty) VALUES (?, ?, ?, ?)
       ON CONFLICT(room_id, product_id, batch_id) DO UPDATE SET qty = qty + excluded.qty`
    ).run(roomId, productId, b.id, take);
    const dLeft = daysUntil(b.expires_at);
    const t = { batchId: b.id, lotNo: b.lot_no, qty: take, expiresAt: b.expires_at, daysLeft: dLeft };
    transfers.push(t);
    if (dLeft <= NEAR_EXPIRY_DAYS) nearExpiry.push(t);
    remaining -= take;
  }
  if (requested > 0 && cartBatches(db, productId).length === 0 && transfers.length === 0) {
    // 补货车无任何批次
  }
  return { transfers, nearExpiry, shortage: remaining, requested };
}

/**
 * 报损：已过期或包装破损只能报损，永不再上架。
 * source=cart 从补货车扣；source=room 从房内扣。
 */
function writeOff(db, { productId, batchId, qty, reason, source, roomId, photo, staffId, note }) {
  if (!['expired', 'damaged'].includes(reason)) {
    throw new HttpError(400, '报损原因只能是 expired 或 damaged', 'bad_reason');
  }
  if (!['cart', 'room'].includes(source)) throw new HttpError(400, '报损来源非法', 'bad_source');
  if (qty <= 0) throw new HttpError(400, '报损数量必须大于 0', 'bad_qty');

  const batch = getBatch(db, batchId);
  if (!batch || batch.product_id !== productId) throw new HttpError(404, '批次不存在', 'unknown_batch');

  if (source === 'cart') {
    const row = db.prepare('SELECT qty FROM cart_stock WHERE product_id = ? AND batch_id = ?').get(productId, batchId);
    if (!row || row.qty < qty) throw new HttpError(409, '补货车该批次数量不足', 'cart_qty');
    db.prepare('UPDATE cart_stock SET qty = qty - ? WHERE product_id = ? AND batch_id = ?').run(qty, productId, batchId);
    db.prepare('UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ?').run(qty, batchId);
  } else {
    if (!roomId) throw new HttpError(400, '房内报损需要 roomId', 'room_required');
    const row = db.prepare('SELECT qty FROM room_stock WHERE room_id = ? AND product_id = ? AND batch_id = ?')
      .get(roomId, productId, batchId);
    if (!row || row.qty < qty) throw new HttpError(409, '房内该批次数量不足', 'room_qty');
    db.prepare('UPDATE room_stock SET qty = qty - ? WHERE room_id = ? AND product_id = ? AND batch_id = ?')
      .run(qty, roomId, productId, batchId);
  }

  // 一旦认定过期/破损，整批冻结，不得再次上架
  const newStatus = reason === 'expired' ? 'expired' : 'damaged';
  if (batch.status === 'active' || (reason === 'damaged' && batch.status !== 'damaged')) {
    db.prepare('UPDATE batches SET status = ? WHERE id = ?').run(newStatus, batchId);
  }

  const id = uid('wo');
  db.prepare(
    `INSERT INTO write_offs(id, batch_id, product_id, qty, reason, source, room_id, photo, staff_id, note, at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(id, batchId, productId, qty, reason, source, roomId || null, photo || null, staffId, note || null, now());
  return db.prepare('SELECT * FROM write_offs WHERE id = ?').get(id);
}

/** 补货车清单（按商品聚合 + 批次明细） */
function cartList(db) {
  const products = db.prepare('SELECT * FROM products WHERE active = 1 ORDER BY name').all();
  return products.map((p) => {
    const batches = db.prepare(
      `SELECT b.id, b.lot_no, b.expires_at, b.status, c.qty,
              CAST(julianday(b.expires_at) - julianday('now') AS INTEGER) AS days_left
         FROM cart_stock c JOIN batches b ON b.id = c.batch_id
        WHERE c.product_id = ? AND c.qty > 0
        ORDER BY b.expires_at`
    ).all(p.id);
    return { ...p, cartQty: batches.reduce((s, b) => s + b.qty, 0), batches };
  }).filter((p) => p.cartQty > 0);
}

module.exports = {
  NEAR_EXPIRY_DAYS,
  receiveBatch, getBatch, cartBatches, roomBatches, roomProductQty,
  deductRoomFIFO, replenishFromCart, writeOff, cartList,
};
