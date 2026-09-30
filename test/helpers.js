'use strict';

const { open } = require('../src/db');
const inv = require('../src/domain/inventory');
const stays = require('../src/domain/stays');
const { isoDaysFromNow } = require('../src/util');

/**
 * 构造一个内存数据库测试场景：
 *  - 经理（全楼层）、3/4 层前台与服务员
 *  - 3 个商品（水/啤酒/坚果），含新老两个水批次用于 FIFO
 *  - 301（3层）、401（4层）已入住
 */
function buildScenario() {
  const db = open(':memory:');
  const ins = (sql, ...a) => db.prepare(sql).run(...a);

  ins('INSERT INTO staff(id,name,role,floor_scope) VALUES (?,?,?,?)', 'mgr', '经理', 'manager', null);
  ins('INSERT INTO staff(id,name,role,floor_scope) VALUES (?,?,?,?)', 'fd3', '前台3', 'frontdesk', '[3]');
  ins('INSERT INTO staff(id,name,role,floor_scope) VALUES (?,?,?,?)', 'fd4', '前台4', 'frontdesk', '[4]');
  ins('INSERT INTO staff(id,name,role,floor_scope) VALUES (?,?,?,?)', 'att3', '服务员3', 'attendant', '[3]');
  ins('INSERT INTO staff(id,name,role,floor_scope) VALUES (?,?,?,?)', 'att4', '服务员4', 'attendant', '[4]');

  const staff = {
    mgr: db.prepare("SELECT * FROM staff WHERE id='mgr'").get(),
    fd3: db.prepare("SELECT * FROM staff WHERE id='fd3'").get(),
    fd4: db.prepare("SELECT * FROM staff WHERE id='fd4'").get(),
    att3: db.prepare("SELECT * FROM staff WHERE id='att3'").get(),
    att4: db.prepare("SELECT * FROM staff WHERE id='att4'").get(),
  };

  ins('INSERT INTO products(id,sku,name,image,price_cents,display_cap) VALUES (?,?,?,?,?,?)', 'p_water', 'WATER', '矿泉水', '/img/w.svg', 800, 4);
  ins('INSERT INTO products(id,sku,name,image,price_cents,display_cap) VALUES (?,?,?,?,?,?)', 'p_beer', 'BEER', '啤酒', '/img/b.svg', 1800, 6);
  ins('INSERT INTO products(id,sku,name,image,price_cents,display_cap) VALUES (?,?,?,?,?,?)', 'p_nuts', 'NUTS', '坚果', '/img/n.svg', 2200, 3);

  const bWaterOld = inv.receiveBatch(db, { productId: 'p_water', lotNo: 'OLD', expiresAt: isoDaysFromNow(2), qty: 10 });
  const bWaterNew = inv.receiveBatch(db, { productId: 'p_water', lotNo: 'NEW', expiresAt: isoDaysFromNow(180), qty: 10 });
  const bBeer = inv.receiveBatch(db, { productId: 'p_beer', lotNo: 'BEER1', expiresAt: isoDaysFromNow(300), qty: 12 });
  const bNuts = inv.receiveBatch(db, { productId: 'p_nuts', lotNo: 'NUTS1', expiresAt: isoDaysFromNow(120), qty: 6 });

  ins("INSERT INTO rooms(id,floor,label) VALUES ('301',3,'301 房')");
  ins("INSERT INTO rooms(id,floor,label) VALUES ('401',4,'401 房')");

  function place(roomId, pid, batchId, qty) {
    ins('INSERT INTO room_stock(room_id,product_id,batch_id,qty) VALUES (?,?,?,?)', roomId, pid, batchId, qty);
    ins('UPDATE cart_stock SET qty=qty-? WHERE product_id=? AND batch_id=?', qty, pid, batchId);
    ins('UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?', qty, batchId);
  }
  place('301', 'p_water', bWaterOld.id, 2);
  place('301', 'p_water', bWaterNew.id, 2);
  place('301', 'p_beer', bBeer.id, 6);
  place('401', 'p_water', bWaterNew.id, 4);
  place('401', 'p_beer', bBeer.id, 6);
  place('401', 'p_nuts', bNuts.id, 3);

  const o301 = stays.checkIn(db, { roomId: '301', guestAnon: '匿名A', staffId: 'mgr' });
  const o401 = stays.checkIn(db, { roomId: '401', guestAnon: '匿名B', staffId: 'mgr' });

  return {
    db, staff,
    batches: { waterOld: bWaterOld.id, waterNew: bWaterNew.id, beer: bBeer.id, nuts: bNuts.id },
    orders: { r301: o301.id, r401: o401.id },
  };
}

module.exports = { buildScenario };
