'use strict';

const { uid, now } = require('../util');
const { HttpError, tx, audit } = require('../util');

function charge(db, { orderId, consumptionId, amountCents, kind, reason, staffId, idemKey }) {
  const key = idemKey || (consumptionId ? `${kind}:${consumptionId}` : uid('idem'));
  // 幂等：同一 (订单, 幂等键) 直接返回原账单行，不依赖底层报错文案
  const existing = db.prepare('SELECT * FROM bill_charges WHERE order_id = ? AND idempotency_key = ?').get(orderId, key);
  if (existing) return { id: existing.id, idempotent: true };
  const id = uid('chg');
  db.prepare(
    `INSERT INTO bill_charges(id, order_id, consumption_id, kind, amount_cents, reason, created_by, idempotency_key, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(id, orderId, consumptionId || null, kind, amountCents, reason || null, staffId || null, key, now());
  return { id, idempotent: false };
}

/** 确认消费 -> 正向 minibar 账单（幂等） */
function confirmConsumption(db, con, staff) {
  if (con.status === 'confirmed') {
    const existing = db.prepare(
      "SELECT * FROM bill_charges WHERE consumption_id = ? AND kind = 'minibar'"
    ).get(con.id);
    return { id: existing ? existing.id : null, idempotent: true };
  }
  const r = charge(db, {
    orderId: con.order_id, consumptionId: con.id, amountCents: con.amount_cents,
    kind: 'minibar', reason: '消费确认', staffId: staff.id, idemKey: `charge:${con.id}`,
  });
  db.prepare("UPDATE consumption_items SET status='confirmed', resolved_at=?, resolved_by=? WHERE id=?")
    .run(now(), staff.id, con.id);
  audit(db, staff.id, 'confirm_consumption', { consumptionId: con.id, amount: con.amount_cents });
  return r;
}

/** 免单：经理 + 原因，账单记 comp（金额为 0 或负向冲抵由调用决定；这里以 kind=comp 的负值体现冲抵） */
function compConsumption(db, con, staff, reason) {
  // 若已正向计费，先幂等冲销，再留 comp 痕迹
  if (hasCharge(db, con.id)) {
    reverseConsumption(db, con, staff, `免单：${reason}`, true);
  }
  const r = charge(db, {
    orderId: con.order_id, consumptionId: con.id, amountCents: 0,
    kind: 'comp', reason, staffId: staff.id, idemKey: `comp:${con.id}`,
  });
  audit(db, staff.id, 'comp_consumption', { consumptionId: con.id, reason });
  return r;
}

function hasCharge(db, consumptionId) {
  const row = db.prepare(
    "SELECT COALESCE(SUM(amount_cents),0) AS s FROM bill_charges WHERE consumption_id = ? AND kind='minibar'"
  ).get(consumptionId);
  return row.s > 0;
}

/** 冲销：经理 + 原因，负向账单，幂等键 reversal:<consumptionId>。冻结订单也允许（用于争议） */
function reverseConsumption(db, con, staff, reason, skipManager) {
  if (!skipManager) {
    const { requireManagerReason } = require('./staff');
    requireManagerReason(staff, reason, '账单冲销');
  }
  // 幂等：同一消费的冲销只入一次；按正向消费与既有冲销轧差，净额为 0 则无需再冲
  const net = db.prepare(
    "SELECT COALESCE(SUM(amount_cents),0) AS s FROM bill_charges WHERE consumption_id = ? AND kind IN ('minibar','reversal')"
  ).get(con.id).s;
  if (net <= 0) {
    return { id: null, idempotent: true, nothingToReverse: true };
  }
  const r = charge(db, {
    orderId: con.order_id, consumptionId: con.id, amountCents: -con.amount_cents,
    kind: 'reversal', reason, staffId: staff.id, idemKey: `reversal:${con.id}`,
  });
  if (!r.idempotent) audit(db, staff.id, 'reverse_consumption', { consumptionId: con.id, reason, amount: -con.amount_cents });
  return r;
}

/** 跨房放错：在目标订单补计 */
function chargeRelocated(db, con, targetOrderId, staff, reason) {
  const r = charge(db, {
    orderId: targetOrderId, consumptionId: con.id, amountCents: con.amount_cents,
    kind: 'minibar', reason, staffId: staff.id, idemKey: `relocate:${con.id}`,
  });
  audit(db, staff.id, 'relocate_charge', { consumptionId: con.id, targetOrderId, amount: con.amount_cents });
  return r;
}

/** 退房后补录：经理 + 原因，在已冻结/关闭订单上加一条调整。无关联消费时必须给幂等键 */
function postCheckoutAdjustment(db, { orderId, amountCents, staff, reason, consumptionId, idemKey }) {
  const cid = consumptionId || null;
  const key = idemKey || (cid ? `postcheckout:${cid}:${orderId}` : `postcheckout:${uid('idem')}:${orderId}`);
  const r = charge(db, {
    orderId, consumptionId: cid, amountCents,
    kind: 'adjustment', reason: `退房后补录：${reason}`, staffId: staff.id, idemKey: key,
  });
  audit(db, staff.id, 'post_checkout_adjustment', { orderId, amountCents, reason, idempotent: r.idempotent });
  return r;
}

function billTimeline(db, orderId) {
  const charges = db.prepare(
    `SELECT c.*, p.name AS product_name
       FROM bill_charges c
       LEFT JOIN consumption_items ci ON ci.id = c.consumption_id
       LEFT JOIN products p ON p.id = ci.product_id
      WHERE c.order_id = ? ORDER BY c.created_at ASC, c.id ASC`
  ).all(orderId);
  const total = charges.reduce((s, c) => s + c.amount_cents, 0);
  return { charges, totalCents: total };
}

/**
 * 退房冻结：锁住最后一次盘点。
 *  - 自动把该订单所有 pending 消费暂挂（不计费），订单与房间进入 frozen/pending_checkout。
 *  - 冻结后不可再提交普通盘点；补录走 post_checkout 复核。
 */
function freezeCheckout(db, { orderId, staffId }) {
  return tx(db, () => {
    const order = db.prepare('SELECT * FROM stay_orders WHERE id = ?').get(orderId);
    if (!order) throw new HttpError(404, '订单不存在', 'unknown_order');
    if (order.status === 'closed') throw new HttpError(409, '订单已关闭', 'order_closed');
    if (order.status === 'frozen') return { idempotent: true, order };

    const last = db.prepare(
      "SELECT * FROM inventory_counts WHERE order_id = ? ORDER BY version DESC LIMIT 1"
    ).get(orderId);
    if (!last) throw new HttpError(409, '缺少盘点，无法冻结', 'no_count');

    db.prepare("UPDATE inventory_counts SET state = 'locked' WHERE id = ?").run(last.id);
    db.prepare("UPDATE stay_orders SET status = 'frozen', frozen_count_id = ? WHERE id = ?").run(last.id, orderId);
    db.prepare("UPDATE rooms SET status = 'pending_checkout' WHERE id = ?").run(order.room_id);
    audit(db, staffId, 'freeze_checkout', { orderId, countId: last.id, version: last.version });
    return {
      idempotent: false,
      order: db.prepare('SELECT * FROM stay_orders WHERE id = ?').get(orderId),
      lockedCount: db.prepare('SELECT * FROM inventory_counts WHERE id = ?').get(last.id),
    };
  });
}

/**
 * 前台确认账单并关闭订单。仅 frozen 可关闭。
 * 关闭前自动确认剩余 pending 消费（计入账单），随后通过 settlement(totalCents) 回调
 * 取得结算参考号（由模拟/真实适配器提供）。整步在一个事务内完成。
 */
function settleAndClose(db, { orderId, staff, autoConfirm, settlement }) {
  return tx(db, () => {
    const order = db.prepare('SELECT * FROM stay_orders WHERE id = ?').get(orderId);
    if (!order) throw new HttpError(404, '订单不存在', 'unknown_order');
    if (order.status !== 'frozen') throw new HttpError(409, '只有退房冻结的订单才能确认账单并关闭', 'not_frozen');

    const { requireFloor } = require('./staff');
    const room = db.prepare('SELECT * FROM rooms WHERE id = ?').get(order.room_id);
    requireFloor(staff, room.floor);

    const pending = db.prepare("SELECT * FROM consumption_items WHERE order_id = ? AND status = 'pending'").all(orderId);
    if (autoConfirm === false && pending.length) {
      throw new HttpError(409, `仍有 ${pending.length} 笔待确认消费，请先复核或确认`, 'pending_exists');
    }
    for (const con of pending) confirmConsumption(db, con, staff);

    const { totalCents } = billTimeline(db, orderId);
    const result = settlement ? settlement(totalCents) : {};
    const ref = result.reference || result.settlementRef || null;
    db.prepare(
      "UPDATE stay_orders SET status='closed', settled_at=?, settlement_ref=? WHERE id=?"
    ).run(now(), ref, orderId);
    db.prepare("UPDATE rooms SET status='vacant', current_order_id = NULL WHERE id=?").run(order.room_id);
    audit(db, staff.id, 'settle_close', { orderId, totalCents, settlementRef: ref });
    return { order: db.prepare('SELECT * FROM stay_orders WHERE id = ?').get(orderId), totalCents, settlementRef: ref, settlement: result };
  });
}

module.exports = {
  charge, confirmConsumption, compConsumption, reverseConsumption, chargeRelocated,
  postCheckoutAdjustment, billTimeline, freezeCheckout, settleAndClose,
};
