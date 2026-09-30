'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { open } = require('./db');
const { uid, now, isoDaysFromNow, daysUntil } = require('./util');
const inv = require('./domain/inventory');
const stays = require('./domain/stays');
const billing = require('./domain/billing');
const reviews = require('./domain/reviews');
const reminders = require('./tasks/reminders');

const DATA_DIR = path.join(__dirname, '..', 'data');
const DB_PATH = process.env.MINIBAR_DB || path.join(DATA_DIR, 'minibar.db');
const IMG_DIR = path.join(__dirname, '..', 'public', 'img');
const PHOTO_DIR = path.join(__dirname, '..', 'photos');

const PRODUCTS = [
  { sku: 'WATER', name: '矿泉水', price: 800, cap: 4, emoji: '💧', bg: '#16324f' },
  { sku: 'COLA', name: '可乐', price: 1000, cap: 4, emoji: '🥤', bg: '#5a2a2a' },
  { sku: 'BEER', name: '迷你啤酒', price: 1800, cap: 6, emoji: '🍺', bg: '#3a4a2a' },
  { sku: 'NUTS', name: '混合坚果', price: 2200, cap: 3, emoji: '🥜', bg: '#4a3a20' },
  { sku: 'CHOCO', name: '巧克力', price: 1500, cap: 3, emoji: '🍫', bg: '#3d2620' },
  { sku: 'JUICE', name: '鲜榨果汁', price: 1200, cap: 4, emoji: '🧃', bg: '#7a4a1e' },
];

function svg(p) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">
<rect width="160" height="160" rx="14" fill="${p.bg}"/>
<rect x="6" y="6" width="148" height="148" rx="10" fill="none" stroke="#c9a24b" stroke-width="2"/>
<text x="80" y="78" font-size="58" text-anchor="middle">${p.emoji}</text>
<text x="80" y="128" font-size="18" text-anchor="middle" fill="#f7f3ea" font-family="sans-serif">${p.name}</text>
<text x="80" y="148" font-size="12" text-anchor="middle" fill="#c9a24b" font-family="sans-serif">¥${(p.price / 100).toFixed(2)}</text>
</svg>`;
}

function photoSvg(label) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">
<rect width="320" height="240" fill="#efe9dc"/>
<rect x="10" y="10" width="300" height="220" rx="8" fill="none" stroke="#c9a24b" stroke-width="3" stroke-dasharray="8 6"/>
<text x="160" y="120" font-size="70" text-anchor="middle">📦</text>
<text x="160" y="180" font-size="20" text-anchor="middle" fill="#16324f" font-family="sans-serif">${label}</text>
</svg>`;
}

function seed(dbPath = DB_PATH) {
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  fs.mkdirSync(IMG_DIR, { recursive: true });
  fs.mkdirSync(PHOTO_DIR, { recursive: true });
  for (const ext of ['-wal', '-shm']) {
    try { fs.rmSync(dbPath + ext); } catch (_) {}
  }
  try { fs.rmSync(dbPath); } catch (_) {}

  const db = open(dbPath);
  const ins = (sql, ...a) => db.prepare(sql).run(...a);

  // ---------- 员工（楼层权限） ----------
  const staff = [
    ['stf_mgr', '周经理', 'manager', null],
    ['stf_fd3', '前台·林(3层)', 'frontdesk', '[3]'],
    ['stf_fd4', '前台·许(4层)', 'frontdesk', '[4]'],
    ['stf_att3', '服务员·阿桂(3层)', 'attendant', '[3]'],
    ['stf_att4', '服务员·阿禾(4层)', 'attendant', '[4]'],
  ];
  for (const [id, name, role, scope] of staff) {
    ins('INSERT INTO staff(id,name,role,floor_scope) VALUES (?,?,?,?)', id, name, role, scope);
  }

  // ---------- 免单原因 ----------
  const reasons = [
    ['guest_deny', '客人否认消费'],
    ['near_expiry', '批次临期酒店承担'],
    ['service_recovery', '服务补偿'],
    ['cross_room', '跨房放错'],
    ['package_damaged', '包装破损'],
  ];
  for (const [code, label] of reasons) ins('INSERT INTO comp_reasons(code,label) VALUES (?,?)', code, label);

  // ---------- 商品 ----------
  const pid = {};
  for (const p of PRODUCTS) {
    const id = `prd_${p.sku.toLowerCase()}`;
    pid[p.sku] = id;
    ins('INSERT INTO products(id,sku,name,image,price_cents,display_cap) VALUES (?,?,?,?,?,?)',
      id, p.sku, p.name, `/img/${p.sku.toLowerCase()}.svg`, p.price, p.cap);
    fs.writeFileSync(path.join(IMG_DIR, `${p.sku.toLowerCase()}.svg`), svg(p));
  }

  // ---------- 批次 ----------
  const batch = (sku, lot, expDays, qty, status = 'active', note = null) => {
    const b = inv.receiveBatch(db, {
      productId: pid[sku], lotNo: lot, expiresAt: isoDaysFromNow(expDays), qty, note,
    });
    if (status !== 'active') ins('UPDATE batches SET status=? WHERE id=?', status, b.id);
    return b.id;
  };
  const waterOld = batch('WATER', 'W2409-A', 2, 10);   // 临期
  const waterFresh = batch('WATER', 'W2512-B', 180, 20);
  const colaFresh = batch('COLA', 'C2511-A', 200, 12);
  const beerExp = batch('BEER', 'B2406-OLD', -3, 6, 'expired');   // 已过期（创建即冻结，禁止上架）
  const beerFresh = batch('BEER', 'B2510-A', 300, 24);
  const nutsDamaged = batch('NUTS', 'N-DMG-01', 120, 4, 'damaged', '外包装挤压破损');
  const nutsFresh = batch('NUTS', 'N2508-A', 120, 9);
  const chocoFresh = batch('CHOCO', 'K2601-A', 400, 9);
  const juiceNear = batch('JUICE', 'J2410-A', 1, 8);    // 临期
  const juiceFresh = batch('JUICE', 'J2512-A', 150, 6);

  // ---------- 客房 ----------
  const rooms = [
    ['301', 3, '301 房'], ['302', 3, '302 房'], ['303', 3, '303 房'],
    ['401', 4, '401 房'], ['402', 4, '402 房'], ['403', 4, '403 房'],
  ];
  for (const [id, floor, label] of rooms) ins('INSERT INTO rooms(id,floor,label) VALUES (?,?,?)', id, floor, label);
  ins("UPDATE rooms SET status='maintenance' WHERE id='403'");

  // 直接按 FIFO 精确布置房内批次（同时从补货车扣减）
  function place(roomId, sku, batchId, qty) {
    const productId = pid[sku];
    ins('INSERT INTO room_stock(room_id,product_id,batch_id,qty) VALUES (?,?,?,?)', roomId, productId, batchId, qty);
    ins('UPDATE cart_stock SET qty=qty-? WHERE product_id=? AND batch_id=?', qty, productId, batchId);
    ins('UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?', qty, batchId);
  }
  // 301：水 4 件，含 2 件临期旧批 + 2 件新批，演示 FIFO
  place('301', 'WATER', waterOld, 2);
  place('301', 'WATER', waterFresh, 2);
  place('301', 'BEER', beerFresh, 6);
  place('301', 'NUTS', nutsFresh, 3);
  place('301', 'CHOCO', chocoFresh, 3);
  // 302：基础陈列，果汁临期
  place('302', 'WATER', waterFresh, 4);
  place('302', 'JUICE', juiceNear, 4);
  place('302', 'COLA', colaFresh, 4);
  // 401：待退客房
  place('401', 'WATER', waterFresh, 4);
  place('401', 'BEER', beerFresh, 6);
  place('401', 'NUTS', nutsFresh, 3);
  place('401', 'CHOCO', chocoFresh, 3);
  // 402：争议客房
  place('402', 'BEER', beerFresh, 6);
  place('402', 'WATER', waterFresh, 4);

  // ---------- 入住（生成基线快照） ----------
  const mgr = db.prepare("SELECT * FROM staff WHERE id='stf_mgr'").get();
  const att3 = db.prepare("SELECT * FROM staff WHERE id='stf_att3'").get();
  const att4 = db.prepare("SELECT * FROM staff WHERE id='stf_att4'").get();

  const o301 = stays.checkIn(db, { roomId: '301', guestAnon: '匿名客人 A-301', staffId: mgr.id });
  const o302 = stays.checkIn(db, { roomId: '302', guestAnon: '匿名客人 B-302', staffId: mgr.id });
  const o401 = stays.checkIn(db, { roomId: '401', guestAnon: '匿名客人 C-401', staffId: mgr.id });
  const o402 = stays.checkIn(db, { roomId: '402', guestAnon: '匿名客人 D-402', staffId: mgr.id });

  // 302 制造“在住超 24h 未盘点”
  const twoDaysAgo = isoDaysFromNow(-2);
  ins("UPDATE stay_orders SET check_in_at=?, created_at=? WHERE id=?", twoDaysAgo, twoDaysAgo, o302.id);
  ins("UPDATE inventory_counts SET counted_at=?, submitted_at=? WHERE order_id=?", twoDaysAgo, twoDaysAgo, o302.id);

  // ---------- 301：盘点消费 1 瓶水（FIFO 命中临期旧批），并补货 1 瓶 ----------
  const sheet301 = stays.roomCountSheet(db, '301');
  const r301 = stays.submitCount(db, {
    roomId: '301',
    staffId: att3.id,
    baseVersion: sheet301.baseVersion,
    device: 'pda-3f-01',
    lines: [
      { productId: pid.WATER, countedQty: 3, replenishQty: 1 },
      { productId: pid.BEER, countedQty: 6, replenishQty: 0 },
      { productId: pid.NUTS, countedQty: 3, replenishQty: 0 },
      { productId: pid.CHOCO, countedQty: 3, replenishQty: 0 },
    ],
  });
  const waterConsumption = r301.consumptions.find((c) => c.productId === pid.WATER);

  // ---------- 401：消费啤酒 2、坚果 1，前台确认后冻结，等待结算 ----------
  const sheet401 = stays.roomCountSheet(db, '401');
  const r401 = stays.submitCount(db, {
    roomId: '401', staffId: att4.id, baseVersion: sheet401.baseVersion, device: 'pda-4f-02',
    lines: [
      { productId: pid.BEER, countedQty: 4, replenishQty: 2 },
      { productId: pid.NUTS, countedQty: 2, replenishQty: 1 },
      { productId: pid.WATER, countedQty: 4, replenishQty: 0 },
      { productId: pid.CHOCO, countedQty: 3, replenishQty: 0 },
    ],
  });
  const fd4 = db.prepare("SELECT * FROM staff WHERE id='stf_fd4'").get();
  for (const c of r401.consumptions) {
    const con = db.prepare('SELECT * FROM consumption_items WHERE id=?').get(c.id);
    billing.confirmConsumption(db, con, fd4);
  }
  billing.freezeCheckout(db, { orderId: o401.id, staffId: fd4.id });

  // ---------- 402：消费啤酒 1，客人否认 -> 争议复核（开放） ----------
  const sheet402 = stays.roomCountSheet(db, '402');
  const r402 = stays.submitCount(db, {
    roomId: '402', staffId: att4.id, baseVersion: sheet402.baseVersion, device: 'pda-4f-02',
    lines: [
      { productId: pid.BEER, countedQty: 5, replenishQty: 1 },
      { productId: pid.WATER, countedQty: 4, replenishQty: 0 },
    ],
  });
  const beerCon = db.prepare('SELECT * FROM consumption_items WHERE id=?').get(r402.consumptions[0].id);
  ins("UPDATE rooms SET status='disputed' WHERE id='402'");
  reviews.openReview(db, {
    flow: 'guest_deny', orderId: o402.id, consumptionId: beerCon.id,
    openedBy: 'stf_fd4', note: '客人称未饮用该啤酒，要求核对',
  });

  // ---------- 报损样例：过期啤酒 + 破损坚果（含包装照片） ----------
  const dmgPhoto = 'sample-damaged.svg';
  fs.writeFileSync(path.join(PHOTO_DIR, dmgPhoto), photoSvg('包装破损凭证（演示）'));
  inv.writeOff(db, {
    productId: pid.BEER, batchId: beerExp, qty: 2, reason: 'expired', source: 'cart',
    staffId: att4.id, note: '批次已过效期，禁止上架',
  });
  const woDmg = inv.writeOff(db, {
    productId: pid.NUTS, batchId: nutsDamaged, qty: 2, reason: 'damaged', source: 'cart',
    photo: dmgPhoto, staffId: att4.id, note: '外包装破损漏气',
  });
  ins('INSERT INTO photos(id,ref_type,ref_id,filename,content_type,uploaded_at) VALUES (?,?,?,?,?,?)',
    uid('pho'), 'writeoff', woDmg.id, dmgPhoto, 'image/svg+xml', now());

  // ---------- 初始提醒 ----------
  reminders.scan(db);

  const summary = {
    dbPath,
    staff: staff.length,
    products: PRODUCTS.length,
    rooms: rooms.length,
    scenarios: {
      occupied: ['301 已盘点有一笔待确认水费', '302 在住超24h未盘'],
      pending_checkout: ['401 已冻结，等待前台确认账单并结算'],
      disputed: ['402 客人否认啤酒消费，复核工单开放'],
    },
    fifoDemo: { room: '301', consumedBatch: waterConsumption.batches[0].lotNo, note: '应优先核销临期批次 W2409-A' },
  };
  return { db, summary };
}

if (require.main === module) {
  const { summary } = seed();
  console.log('✅ 种子数据已写入：', summary.dbPath);
  console.log(JSON.stringify(summary, null, 2));
}

module.exports = { seed, DB_PATH, PHOTO_DIR, PRODUCTS };
