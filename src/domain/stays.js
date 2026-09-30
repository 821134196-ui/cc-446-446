'use strict';

const { uid, now } = require('../util');
const { HttpError, tx, audit } = require('../util');
const inv = require('./inventory');

/**
 * 入住：创建匿名订单，并在入住前生成房内库存快照（version = 0 的基线盘点）。
 */
function checkIn(db, { roomId, guestAnon, scheduledCheckoutAt, staffId }) {
  return tx(db, () => {
    const room = db.prepare('SELECT * FROM rooms WHERE id = ?').get(roomId);
    if (!room) throw new HttpError(404, '客房不存在', 'unknown_room');
    if (room.current_order_id) throw new HttpError(409, '该客房已有在住订单', 'room_occupied');

    const orderId = uid('ord');
    db.prepare(
      `INSERT INTO stay_orders(id, room_id, guest_anon, check_in_at, scheduled_checkout_at, status, created_at)
       VALUES (?, ?, ?, ?, ?, 'checked_in', ?)`
    ).run(orderId, roomId, guestAnon || `匿名客人 ${orderId.slice(-4)}`, now(), scheduledCheckoutAt || null, now());
    db.prepare("UPDATE rooms SET status = 'occupied', current_order_id = ? WHERE id = ?").run(orderId, roomId);

    // 入住前基线快照：当前房内陈列即为应有数
    const countId = uid('cnt');
    db.prepare(
      `INSERT INTO inventory_counts(id, order_id, room_id, version, state, staff_id, device, counted_at, submitted_at, note)
       VALUES (?, ?, ?, 0, 'submitted', ?, ?, ?, ?, ?)`
    ).run(countId, orderId, roomId, staffId, 'system', now(), now(), '入住前库存快照');

    const products = db.prepare('SELECT * FROM products WHERE active = 1').all();
    const insLine = db.prepare(
      'INSERT INTO count_lines(id, count_id, product_id, expected_qty, counted_qty, replenish_qty) VALUES (?, ?, ?, ?, ?, 0)'
    );
    for (const p of products) {
      const qty = inv.roomProductQty(db, roomId, p.id);
      insLine.run(uid('cln'), countId, p.id, qty, qty);
    }
    audit(db, staffId, 'checkin_snapshot', { orderId, roomId });
    return getOrder(db, orderId);
  });
}

function getOrder(db, id) {
  return db.prepare('SELECT * FROM stay_orders WHERE id = ?').get(id);
}

function getRoom(db, id) {
  return db.prepare('SELECT * FROM rooms WHERE id = ?').get(id);
}

function latestCount(db, roomId) {
  return db.prepare('SELECT * FROM inventory_counts WHERE room_id = ? ORDER BY version DESC LIMIT 1').get(roomId);
}

function countLines(db, countId) {
  return db.prepare(
    `SELECT cl.*, p.name, p.sku, p.image, p.price_cents, p.display_cap
       FROM count_lines cl JOIN products p ON p.id = cl.product_id
      WHERE cl.count_id = ? ORDER BY p.name`
  ).all(countId);
}

/**
 * 提交盘点。
 * lines: [{ productId, countedQty, replenishQty }]
 * baseVersion: 服务员开单时看到的版本；与当前最新版本不一致即版本冲突。
 * clientGuid: 离线重传去重键。
 *
 * 返回 { count, consumptions, replenished, shortages, nearExpiry, anomalies }
 */
function submitCount(db, params) {
  const { roomId, staffId, lines, device, note } = params;
  const baseVersion = Number(params.baseVersion);
  const clientGuid = params.clientGuid || null;
  const countedAt = params.countedAt || now();

  return tx(db, () => {
    const room = getRoom(db, roomId);
    if (!room) throw new HttpError(404, '客房不存在', 'unknown_room');
    const order = getOrder(db, room.current_order_id);
    if (!order) throw new HttpError(409, '该客房没有在住订单', 'no_order');
    if (order.status === 'closed') throw new HttpError(409, '订单已关闭，不能再盘点', 'order_closed');
    if (order.status === 'frozen') {
      throw new HttpError(409, '订单已退房冻结，补录须走退房后复核流程', 'frozen');
    }

    // 离线重传：相同 client_guid 直接返回原单（幂等），不重复入账
    if (clientGuid) {
      const dup = db.prepare('SELECT * FROM inventory_counts WHERE client_guid = ?').get(clientGuid);
      if (dup) {
        return { deduplicated: true, count: dup, consumptions: [], replenished: [], shortages: [], nearExpiry: [], anomalies: [] };
      }
    }

    const head = latestCount(db, roomId);
    const currentVersion = head ? head.version : 0;
    if (!Number.isInteger(baseVersion) || baseVersion < 0) {
      throw new HttpError(400, 'baseVersion 非法', 'bad_version');
    }
    if (baseVersion !== currentVersion) {
      throw new HttpError(409,
        `版本冲突：您的基线版本 v${baseVersion} 已过期，当前为 v${currentVersion}，请刷新后重盘`,
        'version_conflict');
    }
    const newVersion = currentVersion + 1;

    const countId = uid('cnt');
    db.prepare(
      `INSERT INTO inventory_counts(id, order_id, room_id, version, client_guid, state, staff_id, device, counted_at, submitted_at, note)
       VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?)`
    ).run(countId, order.id, roomId, newVersion, clientGuid, staffId, device || null, countedAt, now(), note || null);

    const products = db.prepare('SELECT * FROM products WHERE active = 1').all();
    const byProduct = new Map(lines.map((l) => [l.productId, l]));
    const consumptions = [];
    const replenished = [];
    const shortages = [];
    const nearExpiry = [];
    const anomalies = [];

    for (const p of products) {
      const line = byProduct.get(p.id) || {};
      const counted = Math.max(0, Math.floor(Number(line.countedQty ?? inv.roomProductQty(db, roomId, p.id))));
      const replenish = Math.max(0, Math.floor(Number(line.replenishQty || 0)));
      const expected = inv.roomProductQty(db, roomId, p.id);

      db.prepare(
        'INSERT INTO count_lines(id, count_id, product_id, expected_qty, counted_qty, replenish_qty) VALUES (?, ?, ?, ?, ?, ?)'
      ).run(uid('cln'), countId, p.id, expected, counted, replenish);

      // 减少量 -> 待确认消费（FIFO 核销房内批次）
      if (counted < expected) {
        const consumedQty = expected - counted;
        let taken = [];
        try {
          taken = inv.deductRoomFIFO(db, roomId, p.id, consumedQty);
        } catch (err) {
          if (err.code === 'stock_mismatch') {
            anomalies.push({ productId: p.id, expected, counted, issue: '账面与批次库存不一致' });
            throw err;
          }
          throw err;
        }
        // 消费按 FIFO 主批次计价（一批一价：取最先核销批次；跨批次时加权拆分简化为最早批次价）
        const firstBatch = taken[0];
        const cid = uid('con');
        const amount = consumedQty * p.price_cents;
        db.prepare(
          `INSERT INTO consumption_items(id, order_id, count_id, product_id, batch_id, qty, unit_price_cents, amount_cents, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
        ).run(cid, order.id, countId, p.id, firstBatch.batchId, consumedQty, p.price_cents, amount, now());
        consumptions.push({
          id: cid, productId: p.id, name: p.name, qty: consumedQty,
          amountCents: amount, batches: taken,
        });
      } else if (counted > expected) {
        // 多出：疑似跨房放错入房，先登记异常，不自动消费
        anomalies.push({ productId: p.id, expected, counted, issue: '实盘多于应有，疑似跨房放错' });
      }

      // 补足到陈列上限（若调用方给的补货数为 0 且缺陈列，也不强制；以前台提交为准）
      if (replenish > 0) {
        const r = inv.replenishFromCart(db, roomId, p.id, replenish);
        replenished.push({ productId: p.id, name: p.name, ...r });
        if (r.shortage > 0) shortages.push({ productId: p.id, name: p.name, shortage: r.shortage });
        if (r.nearExpiry.length) {
          nearExpiry.push({ productId: p.id, name: p.name, batches: r.nearExpiry });
        }
      }
    }

    // 旧盘点标记为被取代（新快照已形成，不允许旧版本覆盖）
    db.prepare("UPDATE inventory_counts SET state = 'superseded' WHERE room_id = ? AND id != ? AND state = 'submitted'")
      .run(roomId, countId);

    audit(db, staffId, 'submit_count', { roomId, version: newVersion, consumed: consumptions.length, replenished: replenished.length });
    return {
      deduplicated: false,
      count: db.prepare('SELECT * FROM inventory_counts WHERE id = ?').get(countId),
      lines: countLines(db, countId),
      consumptions, replenished, shortages, nearExpiry, anomalies,
    };
  });
}

/** 房内盘点视图：商品、图片、当前数（最新快照）、建议补货数（补到陈列上限） */
function roomCountSheet(db, roomId) {
  const room = getRoom(db, roomId);
  if (!room) throw new HttpError(404, '客房不存在', 'unknown_room');
  const head = latestCount(db, roomId);
  const products = db.prepare('SELECT * FROM products WHERE active = 1 ORDER BY name').all();
  const items = products.map((p) => {
    const current = inv.roomProductQty(db, roomId, p.id);
    const batches = inv.roomBatches(db, roomId, p.id).map((b) => ({
      batchId: b.id, lotNo: b.lot_no, expiresAt: b.expires_at, qty: b.room_qty, status: b.status,
    }));
    const suggest = Math.max(0, p.display_cap - current);
    return { productId: p.id, sku: p.sku, name: p.name, image: p.image, priceCents: p.price_cents,
      displayCap: p.display_cap, current, suggestReplenish: suggest, batches };
  });
  return { room, baseVersion: head ? head.version : 0, lastCount: head, items };
}

/** 盘点历史（版本链） */
function countHistory(db, roomId) {
  const counts = db.prepare('SELECT * FROM inventory_counts WHERE room_id = ? ORDER BY version DESC').all(roomId);
  return counts.map((c) => ({ ...c, lines: countLines(db, c.id) }));
}

module.exports = { checkIn, getOrder, getRoom, latestCount, countLines, submitCount, roomCountSheet, countHistory };
