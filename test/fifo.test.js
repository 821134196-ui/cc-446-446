'use strict';

const test = require('node:test');
const assert = require('node:assert');
const stays = require('../src/domain/stays');
const inv = require('../src/domain/inventory');
const { buildScenario } = require('./helpers');

test('消费按批次先进先出核销：先扣效期最早的临期批次', () => {
  const { db, staff, batches } = buildScenario();

  // 301 房水：OLD(剩2天) x2 + NEW(180天) x2；盘后余 3 即消费 1 瓶
  stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: 0,
    lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }],
  });

  // 只消费了 1 瓶，应全部来自 OLD
  const con = db.prepare("SELECT * FROM consumption_items WHERE product_id='p_water' ORDER BY created_at LIMIT 1").get();
  assert.equal(con.batch_id, batches.waterOld, '应核销效期最早的批次');
  assert.equal(con.qty, 1);

  const oldLeft = db.prepare('SELECT qty FROM room_stock WHERE room_id=? AND batch_id=?').get('301', batches.waterOld);
  const newLeft = db.prepare('SELECT qty FROM room_stock WHERE room_id=? AND batch_id=?').get('301', batches.waterNew);
  assert.equal(oldLeft.qty, 1, '旧批剩 1');
  assert.equal(newLeft.qty, 2, '新批未动');

  // 再消费 2 瓶：跨批次，先取旧批剩余 1，再取新批 1
  stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: 1,
    lines: [{ productId: 'p_water', countedQty: 1, replenishQty: 0 }],
  });
  // 当前房内水总数：基线4 -> 第一次后3 -> 第二次后（实盘1，即再消费2）
  const total = inv.roomProductQty(db, '301', 'p_water');
  assert.equal(total, 1);
  const oldLeft2 = db.prepare('SELECT qty FROM room_stock WHERE room_id=? AND batch_id=?').get('301', batches.waterOld);
  assert.equal(oldLeft2.qty, 0, '旧批应已清空（FIFO 优先消耗）');
});

test('补货从补货车 FIFO 转入，且已过期/破损批次不会再次上架', () => {
  const { db } = buildScenario();

  // 人为制造一个过期批次和破损批次放在车上
  const expired = inv.receiveBatch(db, { productId: 'p_water', lotNo: 'EXPIRED', expiresAt: new Date(Date.now() - 86400000).toISOString(), qty: 5 });
  const damaged = inv.receiveBatch(db, { productId: 'p_water', lotNo: 'DAMAGED', expiresAt: new Date(Date.now() + 90 * 86400000).toISOString(), qty: 5 });
  db.prepare('UPDATE batches SET status=? WHERE id=?').run('expired', expired.id);
  db.prepare('UPDATE batches SET status=? WHERE id=?').run('damaged', damaged.id);
  // 回归：状态仍是 active 但效期已过的批次，也绝不能上架
  const staleFlag = inv.receiveBatch(db, { productId: 'p_water', lotNo: 'STALE_ACTIVE', expiresAt: new Date(Date.now() - 60000).toISOString(), qty: 5 });
  const fresh = inv.receiveBatch(db, { productId: 'p_water', lotNo: 'FRESH', expiresAt: new Date(Date.now() + 200 * 86400000).toISOString(), qty: 8 });

  // 清掉车上既存的正常老批次余量，确保车上候选为：过期、破损、效期已过(active)、FRESH
  db.prepare("UPDATE cart_stock SET qty=0 WHERE product_id='p_water' AND batch_id NOT IN (?,?,?,?)")
    .run(expired.id, damaged.id, staleFlag.id, fresh.id);

  // 从车上补 6 瓶到 301：可上架批次只有 FRESH（过期/破损/效期已过全被排除）
  const result = inv.replenishFromCart(db, '301', 'p_water', 6);
  const batchIds = result.transfers.map((t) => t.batchId);
  assert.ok(!batchIds.includes(expired.id), '过期批次不得上架');
  assert.ok(!batchIds.includes(damaged.id), '破损批次不得上架');
  assert.ok(!batchIds.includes(staleFlag.id), '状态正常但效期已过的批次不得上架');
  assert.ok(batchIds.includes(fresh.id), '应只取正常批次');

  // FIFO：正常批次里效期最早的优先（FRESH 之外还可能有更老的 NEW/OLD 余量）
  const cartOrder = inv.cartBatches(db, 'p_water').map((b) => b.id);
  assert.deepEqual(cartOrder, [...cartOrder].sort((a, b) => {
    const ba = db.prepare('SELECT expires_at FROM batches WHERE id=?').get(a).expires_at;
    const bb = db.prepare('SELECT expires_at FROM batches WHERE id=?').get(b).expires_at;
    return ba < bb ? -1 : ba > bb ? 1 : 0;
  }), '补货车批次按效期升序（FIFO）');
});

test('过期/破损商品只能转报损，报损后批次状态被冻结且不再上车', () => {
  const { db, staff } = buildScenario();
  const b = inv.receiveBatch(db, { productId: 'p_nuts', lotNo: 'WILL_EXPIRE', expiresAt: new Date(Date.now() - 3600000).toISOString(), qty: 4 });
  const wo = inv.writeOff(db, {
    productId: 'p_nuts', batchId: b.id, qty: 4, reason: 'expired', source: 'cart', staffId: staff.mgr.id,
  });
  assert.equal(wo.reason, 'expired');
  const batch = inv.getBatch(db, b.id);
  assert.equal(batch.status, 'expired');
  assert.ok(!inv.cartBatches(db, 'p_nuts').some((x) => x.id === b.id), '报损批次不再出现在可上架清单');
});
