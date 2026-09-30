'use strict';

const test = require('node:test');
const assert = require('node:assert');
const stays = require('../src/domain/stays');
const { buildScenario } = require('./helpers');

test('同一客房两次盘点必须按版本提交，旧版本不得覆盖新快照', () => {
  const { db, staff } = buildScenario();

  const sheet0 = stays.roomCountSheet(db, '301');
  assert.equal(sheet0.baseVersion, 0, '入住基线快照为 v0');

  // 服务员基于 v0 提交，消费 1 瓶水 -> v1
  const r1 = stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: 0,
    lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }],
  });
  assert.equal(r1.count.version, 1);

  // 另一台设备仍拿着 v0 旧版本提交 -> 必须冲突
  assert.throws(
    () => stays.submitCount(db, {
      roomId: '301', staffId: staff.att3.id, baseVersion: 0,
      lines: [{ productId: 'p_water', countedQty: 2, replenishQty: 0 }],
    }),
    (err) => err.code === 'version_conflict'
  );

  // 旧版本未覆盖：最新仍是 v1，v0 不是最新，且只生成过一次消费
  const head = stays.latestCount(db, '301');
  assert.equal(head.version, 1, '旧版本不得覆盖新快照');
  const cons = db.prepare("SELECT COUNT(*) AS n FROM consumption_items WHERE order_id=?").get(stays.getRoom(db, '301').current_order_id).n;
  assert.equal(cons, 1);

  // 基于最新 v1 才能继续提交 -> v2
  const r2 = stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: 1,
    lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }],
  });
  assert.equal(r2.count.version, 2);
});

test('离线重传相同 client_guid 幂等去重，不重复入账', () => {
  const { db, staff } = buildScenario();
  const guid = 'pda-301-guid-0001';
  const first = stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: 0, clientGuid: guid,
    lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }],
  });
  assert.equal(first.deduplicated, false);
  assert.equal(first.count.version, 1);

  // 同设备离线后重传同一 GUID：返回去重，版本不再增长，不产生第二笔消费
  const again = stays.submitCount(db, {
    roomId: '301', staffId: staff.att3.id, baseVersion: 0, clientGuid: guid,
    lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }],
  });
  assert.equal(again.deduplicated, true);
  assert.equal(stays.latestCount(db, '301').version, 1);
});
