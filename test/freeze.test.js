'use strict';

const test = require('node:test');
const assert = require('node:assert');
const stays = require('../src/domain/stays');
const billing = require('../src/domain/billing');
const { buildScenario } = require('./helpers');

test('退房冻结锁住最后一次盘点，冻结后普通盘点被拒绝，结算后才能关闭订单', () => {
  const { db, staff } = buildScenario();

  // 401 消费啤酒 2
  const sheet = stays.roomCountSheet(db, '401');
  const r = stays.submitCount(db, {
    roomId: '401', staffId: staff.att4.id, baseVersion: sheet.baseVersion,
    lines: [{ productId: 'p_beer', countedQty: 4, replenishQty: 0 }],
  });
  const con = db.prepare('SELECT * FROM consumption_items WHERE id=?').get(r.consumptions[0].id);
  billing.confirmConsumption(db, con, staff.fd4);

  const orderId = stays.getRoom(db, '401').current_order_id;
  const freeze = billing.freezeCheckout(db, { orderId, staffId: staff.fd4.id });
  assert.equal(freeze.order.status, 'frozen');
  assert.equal(freeze.lockedCount.state, 'locked', '最后一次盘点被锁定');

  // 冻结幂等：再次冻结不报错也不换锁定单
  const again = billing.freezeCheckout(db, { orderId, staffId: staff.fd4.id });
  assert.equal(again.idempotent, true);
  assert.equal(again.order.frozen_count_id, freeze.lockedCount.id);

  // 冻结后普通盘点必须被拒绝（引导走退房后补录）
  assert.throws(
    () => stays.submitCount(db, {
      roomId: '401', staffId: staff.att4.id,
      baseVersion: stays.roomCountSheet(db, '401').baseVersion,
      lines: [{ productId: 'p_beer', countedQty: 3, replenishQty: 0 }],
    }),
    (e) => e.code === 'frozen'
  );

  // 未冻结状态不能直接结算关闭：构造在住订单尝试
  const liveId = stays.getRoom(db, '301').current_order_id;
  assert.throws(
    () => billing.settleAndClose(db, {
      orderId: liveId, staff: staff.fd3, autoConfirm: true,
      settlement: () => ({ reference: 'X' }),
    }),
    (e) => e.code === 'not_frozen'
  );

  // 前台确认账单（含模拟结算参考号）后订单关闭、房态转空房
  const closed = billing.settleAndClose(db, {
    orderId, staff: staff.fd4, autoConfirm: true,
    settlement: (total) => ({ reference: `MOCK-${total}` }),
  });
  assert.equal(closed.order.status, 'closed');
  assert.equal(closed.settlement.reference, 'MOCK-3600', '啤酒 2×1800 = 3600 分');
  assert.equal(stays.getRoom(db, '401').status, 'vacant');
  assert.equal(stays.getRoom(db, '401').current_order_id, null);
});
