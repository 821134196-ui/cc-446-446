'use strict';

const test = require('node:test');
const assert = require('node:assert');
const stays = require('../src/domain/stays');
const billing = require('../src/domain/billing');
const { buildScenario } = require('./helpers');

function makeCharge(db, staff) {
  const sheet = stays.roomCountSheet(db, '301');
  const r = stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: sheet.baseVersion,
    lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }],
  });
  const con = db.prepare('SELECT * FROM consumption_items WHERE id=?').get(r.consumptions[0].id);
  billing.confirmConsumption(db, con, staff.fd3);
  return con;
}

test('账单冲销幂等：重复冲销只产生一条负向账单，金额不重复扣减', () => {
  const { db, staff } = buildScenario();
  const con = makeCharge(db, staff);

  const totalAfterCharge = billing.billTimeline(db, con.order_id).totalCents;
  assert.equal(totalAfterCharge, 800, '正向计费 800 分');

  const rev1 = billing.reverseConsumption(db, con, staff.mgr, '客人否认，经理核实');
  assert.equal(rev1.idempotent, false);

  const totalAfterReverse = billing.billTimeline(db, con.order_id).totalCents;
  assert.equal(totalAfterReverse, 0, '冲销后净额归 0');

  // 再次冲销：幂等，无新增负值
  const rev2 = billing.reverseConsumption(db, con, staff.mgr, '重复冲销请求');
  assert.equal(rev2.idempotent, true);
  assert.equal(rev2.nothingToReverse, true);
  assert.equal(billing.billTimeline(db, con.order_id).totalCents, 0);

  const rows = db.prepare("SELECT * FROM bill_charges WHERE consumption_id=? ORDER BY created_at").all(con.id);
  assert.equal(rows.length, 2, '一条正向 + 一条冲销，冲销不重复');
  assert.equal(rows[0].kind, 'minibar');
  assert.equal(rows[1].kind, 'reversal');
  assert.equal(rows[1].amount_cents, -800);
});

test('免单与冲销要求经理权限并留存原因', () => {
  const { db, staff } = buildScenario();
  const con = makeCharge(db, staff);

  // 前台不是经理 -> 拒绝
  assert.throws(() => billing.reverseConsumption(db, con, staff.fd3, '某原因'),
    (e) => e.code === 'manager_required');

  // 经理但缺原因 -> 拒绝
  assert.throws(() => billing.reverseConsumption(db, con, staff.mgr, '   '),
    (e) => e.code === 'reason_required');

  // 经理 + 原因 -> 成功
  const ok = billing.reverseConsumption(db, con, staff.mgr, '客人否认，监控核实未取');
  assert.equal(ok.idempotent, false);
});

test('退房后补录为带原因的调整账，且同一补录幂等', () => {
  const { db, staff } = buildScenario();
  const orderId = stays.getRoom(db, '301').current_order_id;
  billing.freezeCheckout(db, { orderId, staffId: staff.fd3.id });
  billing.settleAndClose(db, { orderId, staff: staff.fd3, autoConfirm: true, settlement: () => ({ reference: 'MOCK-0' }) });

  const a1 = billing.postCheckoutAdjustment(db, {
    orderId, amountCents: 1500, staff: staff.mgr, reason: '退房后补录迷你吧消费', idemKey: 'postcheckout:test-key',
  });
  assert.equal(a1.idempotent, false);
  const a2 = billing.postCheckoutAdjustment(db, {
    orderId, amountCents: 1500, staff: staff.mgr, reason: '重复提交', idemKey: 'postcheckout:test-key',
  });
  assert.equal(a2.idempotent, true);
  const total = billing.billTimeline(db, orderId).totalCents;
  assert.equal(total, 1500, '补录只计一次');
});
