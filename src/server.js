'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { URL } = require('node:url');

const { open } = require('./db');
const { HttpError, tx, yuan } = require('./util');
const staffDomain = require('./domain/staff');
const stays = require('./domain/stays');
const inv = require('./domain/inventory');
const billing = require('./domain/billing');
const reviewsDomain = require('./domain/reviews');
const reminders = require('./tasks/reminders');
const pms = require('./adapters/pms');

const ROOT = path.join(__dirname, '..');
const PUBLIC = path.join(ROOT, 'public');
const PHOTO_DIR = path.join(ROOT, 'photos');
const DB_PATH = process.env.MINIBAR_DB || path.join(ROOT, 'data', 'minibar.db');
const PORT = Number(process.env.PORT || 3000);

const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.json': 'application/json; charset=utf-8',
  '.ico': 'image/x-icon',
};

function createServer(db) {
  // ---------- 查询辅助 ----------
  function floorboard(staff) {
    const floors = db.prepare('SELECT DISTINCT floor FROM rooms ORDER BY floor').all().map((r) => r.floor);
    return floors
      .filter((f) => staffDomain.canAccessFloor(staff, f))
      .map((floor) => {
        const rooms = db.prepare(
          `SELECT r.*, o.id AS order_id, o.guest_anon, o.status AS order_status,
                  o.scheduled_checkout_at,
                  (SELECT COUNT(*) FROM consumption_items ci WHERE ci.order_id = o.id AND ci.status='pending') AS pending_count,
                  (SELECT COUNT(*) FROM reviews rv WHERE rv.order_id = o.id AND rv.state='open') AS review_count
             FROM rooms r LEFT JOIN stay_orders o ON o.id = r.current_order_id
            WHERE r.floor = ? ORDER BY r.label`
        ).all(floor);
        return { floor, rooms };
      });
  }

  function consumptionQueue(status) {
    const sql = `
      SELECT ci.*, p.name AS product_name, p.image, r.floor, r.label AS room_label, o.guest_anon,
             ic.version AS count_version, ic.counted_at
        FROM consumption_items ci
        JOIN products p ON p.id = ci.product_id
        JOIN stay_orders o ON o.id = ci.order_id
        JOIN rooms r ON r.id = o.room_id
        JOIN inventory_counts ic ON ic.id = ci.count_id
        ${status ? 'WHERE ci.status = ?' : ''}
       ORDER BY ci.created_at DESC`;
    return status ? db.prepare(sql).all(status) : db.prepare(sql).all();
  }

  function writeOffs() {
    return db.prepare(
      `SELECT w.*, p.name AS product_name, b.lot_no, r.label AS room_label
         FROM write_offs w JOIN products p ON p.id = w.product_id
         LEFT JOIN batches b ON b.id = w.batch_id
         LEFT JOIN rooms r ON r.id = w.room_id
        ORDER BY w.at DESC`
    ).all();
  }

  function nearExpiry() {
    return db.prepare(
      `SELECT b.id, b.lot_no, b.expires_at, b.status, b.qty_remaining, p.name AS product_name, p.image,
              CAST(CEIL(julianday(b.expires_at) - julianday('now')) AS INTEGER) AS days_left,
              (SELECT COALESCE(SUM(qty),0) FROM cart_stock c WHERE c.batch_id=b.id) AS cart_qty,
              (SELECT COALESCE(SUM(qty),0) FROM room_stock rs WHERE rs.batch_id=b.id) AS room_qty
         FROM batches b JOIN products p ON p.id = b.product_id
        WHERE julianday(b.expires_at) - julianday('now') <= ?
        ORDER BY b.expires_at`
    ).all(inv.NEAR_EXPIRY_DAYS);
  }

  // ---------- 路由 ----------
  const routes = [];
  const route = (method, pattern, handler) => routes.push({ method, pattern, handler });

  route('GET', /^\/api\/whoami$/, (req, res, ctx) => {
    ctx.send({ staff: publicStaff(ctx.staff) });
  });
  route('GET', /^\/api\/staff$/, (req, res, ctx) => {
    ctx.send({ staff: db.prepare('SELECT id,name,role,floor_scope FROM staff WHERE active=1 ORDER BY role,id').all().map(publicStaff) });
  });
  route('GET', /^\/api\/floorboard$/, (req, res, ctx) => ctx.send({ floors: floorboard(ctx.staff) }));
  route('GET', /^\/api\/products$/, (req, res, ctx) =>
    ctx.send({ products: db.prepare('SELECT * FROM products WHERE active=1 ORDER BY name').all() }));
  route('GET', /^\/api\/comp-reasons$/, (req, res, ctx) =>
    ctx.send({ reasons: db.prepare('SELECT * FROM comp_reasons WHERE active=1').all() }));

  route('GET', /^\/api\/rooms\/([^/]+)\/count-sheet$/, (req, res, ctx, m) => {
    const room = stays.getRoom(db, m[1]);
    staffDomain.requireFloor(ctx.staff, room.floor);
    const sheet = stays.roomCountSheet(db, room.id);
    const order = room.current_order_id ? stays.getOrder(db, room.current_order_id) : null;
    ctx.send({ ...sheet, order });
  });
  route('GET', /^\/api\/rooms\/([^/]+)\/history$/, (req, res, ctx, m) => {
    const room = stays.getRoom(db, m[1]);
    staffDomain.requireFloor(ctx.staff, room.floor);
    ctx.send({ history: stays.countHistory(db, room.id) });
  });
  route('POST', /^\/api\/rooms\/([^/]+)\/counts$/, (req, res, ctx, m) => {
    const room = stays.getRoom(db, m[1]);
    staffDomain.requireFloor(ctx.staff, room.floor);
    if (!['attendant', 'manager'].includes(ctx.staff.role)) throw new HttpError(403, '仅服务员可提交盘点', 'role_forbidden');
    const result = stays.submitCount(db, {
      roomId: room.id, staffId: ctx.staff.id,
      baseVersion: req.body.baseVersion, lines: req.body.lines || [],
      clientGuid: req.body.clientGuid, device: req.body.device,
      countedAt: req.body.countedAt, note: req.body.note,
    });
    if (!result.deduplicated && result.shortages?.length) {
      for (const s of result.shortages) {
        reviewsDomain.openReview(db, {
          flow: 'cart_shortage', orderId: result.count.order_id,
          openedBy: ctx.staff.id, note: `补货车短缺：${s.name} 缺 ${s.shortage} 件`,
        });
      }
    }
    if (!result.deduplicated && result.nearExpiry?.length) {
      for (const ne of result.nearExpiry.filter((x) => x.nearExpiry.length)) {
        for (const b of ne.nearExpiry) {
          reviewsDomain.openReview(db, {
            flow: 'near_expiry', orderId: result.count.order_id,
            openedBy: ctx.staff.id, note: `上架临期批次 ${b.lotNo}（${ne.name}）剩余 ${b.daysLeft} 天`,
          });
        }
      }
    }
    ctx.send(result, result.deduplicated ? 200 : 201);
  });

  route('POST', /^\/api\/checkin$/, (req, res, ctx) => {
    staffDomain.requireManager(ctx.staff, '办理入住');
    const order = stays.checkIn(db, {
      roomId: req.body.roomId, guestAnon: req.body.guestAnon,
      scheduledCheckoutAt: req.body.scheduledCheckoutAt, staffId: ctx.staff.id,
    });
    ctx.send({ order }, 201);
  });

  route('GET', /^\/api\/cart$/, (req, res, ctx) => ctx.send({ cart: inv.cartList(db) }));
  route('POST', /^\/api\/batches\/receive$/, (req, res, ctx) => {
    staffDomain.requireManager(ctx.staff, '批次入库');
    const b = tx(db, () => inv.receiveBatch(db, req.body));
    ctx.send({ batch: b }, 201);
  });

  route('GET', /^\/api\/batches\/near-expiry$/, (req, res, ctx) => ctx.send({ batches: nearExpiry() }));
  route('GET', /^\/api\/write-offs$/, (req, res, ctx) => ctx.send({ writeOffs: writeOffs() }));
  route('POST', /^\/api\/write-offs$/, (req, res, ctx) => {
    const body = req.body;
    if (body.source === 'room' && body.roomId) {
      const room = stays.getRoom(db, body.roomId);
      if (room) staffDomain.requireFloor(ctx.staff, room.floor);
    }
    const wo = tx(db, () => inv.writeOff(db, { ...body, staffId: ctx.staff.id }));
    ctx.send({ writeOff: wo }, 201);
  });

  route('GET', /^\/api\/consumptions$/, (req, res, ctx) => {
    let items = consumptionQueue(ctx.query.status || null);
    if (ctx.staff.role !== 'manager') {
      const floors = staffDomain.scopeFloors(ctx.staff);
      if (floors) items = items.filter((i) => floors.includes(i.floor));
    }
    ctx.send({ consumptions: items });
  });
  route('POST', /^\/api\/consumptions\/([^/]+)\/confirm$/, (req, res, ctx, m) => {
    const con = mustConsumption(db, m[1]);
    const room = roomOfOrder(db, con.order_id);
    staffDomain.requireFloor(ctx.staff, room.floor);
    const result = tx(db, () => billing.confirmConsumption(db, con, ctx.staff));
    ctx.send(result);
  });
  route('POST', /^\/api\/consumptions\/([^/]+)\/comp$/, (req, res, ctx, m) => {
    const con = mustConsumption(db, m[1]);
    const room = roomOfOrder(db, con.order_id);
    staffDomain.requireFloor(ctx.staff, room.floor);
    staffDomain.requireManagerReason(ctx.staff, req.body.reason, '免单');
    const result = tx(db, () => billing.compConsumption(db, con, ctx.staff, req.body.reason));
    ctx.send(result);
  });
  route('POST', /^\/api\/consumptions\/([^/]+)\/reverse$/, (req, res, ctx, m) => {
    const con = mustConsumption(db, m[1]);
    const room = roomOfOrder(db, con.order_id);
    staffDomain.requireFloor(ctx.staff, room.floor);
    staffDomain.requireManagerReason(ctx.staff, req.body.reason, '账单冲销');
    const result = tx(db, () => billing.reverseConsumption(db, con, ctx.staff, req.body.reason));
    ctx.send(result);
  });

  route('GET', /^\/api\/reviews$/, (req, res, ctx) => {
    let list = reviewsDomain.listReviews(db, ctx.query.state || null);
    if (ctx.staff.role !== 'manager') {
      const floors = staffDomain.scopeFloors(ctx.staff);
      if (floors) {
        list = list.filter((rv) => {
          const room = db.prepare('SELECT r.floor FROM stay_orders o JOIN rooms r ON r.id=o.room_id WHERE o.id=?').get(rv.order_id);
          return room && floors.includes(room.floor);
        });
      }
    }
    ctx.send({ reviews: list });
  });
  route('POST', /^\/api\/reviews$/, (req, res, ctx) => {
    const rv = tx(db, () => reviewsDomain.openReview(db, {
      flow: req.body.flow, orderId: req.body.orderId,
      consumptionId: req.body.consumptionId, openedBy: ctx.staff.id, note: req.body.note,
    }));
    ctx.send({ review: rv }, 201);
  });
  route('POST', /^\/api\/reviews\/([^/]+)\/resolve$/, (req, res, ctx, m) => {
    const rv = reviewsDomain.getReview(db, m[1]);
    if (!rv) throw new HttpError(404, '复核工单不存在', 'unknown_review');
    const room = db.prepare('SELECT r.floor FROM stay_orders o JOIN rooms r ON r.id=o.room_id WHERE o.id=?').get(rv.order_id);
    if (room) staffDomain.requireFloor(ctx.staff, room.floor);
    const result = reviewsDomain.resolveReview(db, {
      reviewId: rv.id, staff: ctx.staff, action: req.body.action,
      reason: req.body.reason, targetOrderId: req.body.targetOrderId, note: req.body.note,
    });
    ctx.send({ review: result });
  });

  route('GET', /^\/api\/orders\/([^/]+)\/bill$/, (req, res, ctx, m) => {
    const order = mustOrder(db, m[1]);
    const room = db.prepare('SELECT * FROM rooms WHERE id=?').get(order.room_id);
    staffDomain.requireFloor(ctx.staff, room.floor);
    const bill = billing.billTimeline(db, order.id);
    const items = db.prepare('SELECT * FROM consumption_items WHERE order_id=? ORDER BY created_at').all(order.id);
    const pmsStatus = pms.fetchPmsRoomStatus(db, room.id);
    ctx.send({ order, room, bill: { ...bill, totalYuan: yuan(bill.totalCents) }, consumptions: items, pmsStatus });
  });
  route('POST', /^\/api\/orders\/([^/]+)\/freeze$/, (req, res, ctx, m) => {
    const order = mustOrder(db, m[1]);
    const room = db.prepare('SELECT * FROM rooms WHERE id=?').get(order.room_id);
    staffDomain.requireFloor(ctx.staff, room.floor);
    const result = billing.freezeCheckout(db, { orderId: order.id, staffId: ctx.staff.id });
    ctx.send(result);
  });
  route('POST', /^\/api\/orders\/([^/]+)\/settle$/, (req, res, ctx, m) => {
    const order = mustOrder(db, m[1]);
    const autoConfirm = req.body.autoConfirm !== false;
    // 结算适配器作为回调传入：在同一事务内拿到总额、取结算参考号并关单
    const result = billing.settleAndClose(db, {
      orderId: order.id, staff: ctx.staff, autoConfirm,
      settlement(totalCents) {
        return pms.submitSettlement({ orderId: order.id, amountCents: totalCents, guestAnon: order.guest_anon });
      },
    });
    ctx.send(result);
  });

  route('GET', /^\/api\/notifications$/, (req, res, ctx) =>
    ctx.send({ notifications: reminders.list(db, ctx.query.unread === '1') }));
  route('POST', /^\/api\/notifications\/([^/]+)\/read$/, (req, res, ctx, m) => {
    reminders.markRead(db, m[1]);
    ctx.send({ ok: true });
  });
  route('POST', /^\/api\/reminders\/scan$/, (req, res, ctx) =>
    ctx.send({ created: reminders.scan(db) }));

  // 包装照片：接收原始字节，存入本地 photos 目录
  route('POST', /^\/api\/photos$/, async (req, res, ctx) => {
    const buf = req.rawBody;
    const filename = sanitizePhoto(req.headers['x-filename'] || `photo-${Date.now()}.bin`);
    fs.mkdirSync(PHOTO_DIR, { recursive: true });
    fs.writeFileSync(path.join(PHOTO_DIR, filename), buf);
    const id = require('./util').uid('pho');
    db.prepare('INSERT INTO photos(id,ref_type,ref_id,filename,content_type,uploaded_at) VALUES (?,?,?,?,?,?)')
      .run(id, req.headers['x-ref-type'] || 'unlinked', req.headers['x-ref-id'] || null,
        filename, req.headers['content-type'] || null, new Date().toISOString());
    ctx.send({ id, filename, bytes: buf.length }, 201);
  });

  // ---------- 服务器 ----------
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
      const pathname = url.pathname;

      if (pathname.startsWith('/api/')) {
        const ctx = {
          query: Object.fromEntries(url.searchParams),
          staff: staffDomain.getStaff(db, req.headers['x-staff-id'] || 'stf_mgr'),
          send(payload, status = 200) {
            const body = JSON.stringify(payload, null, 2);
            res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
            res.end(body);
          },
        };
        const match = routes.find((r) => r.method === req.method && r.pattern.test(pathname));
        if (!match) throw new HttpError(404, '接口不存在', 'not_found');
        if (req.method === 'POST' || req.method === 'PUT' || req.method === 'PATCH') {
          const isPhoto = /^\/api\/photos$/.test(pathname);
          const buf = await readRaw(req);
          if (isPhoto) {
            req.rawBody = buf;
            req.body = {};
          } else {
            const raw = buf.toString('utf-8');
            req.body = raw ? JSON.parse(raw) : {};
          }
        } else {
          req.body = {};
        }
        await match.handler(req, res, ctx, pathname.match(match.pattern));
        return;
      }

      serveStatic(pathname, res);
    } catch (err) {
      if (err instanceof SyntaxError) {
        res.writeHead(400, { 'content-type': 'application/json; charset=utf-8' });
        return res.end(JSON.stringify({ error: 'bad_json', message: '请求体不是合法 JSON' }));
      }
      if (err instanceof HttpError) {
        res.writeHead(err.status, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ error: err.code, message: err.message }));
      } else {
        res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ error: 'internal', message: String(err.message || err) }));
      }
    }
  });

  return server;
}

let db;
function publicStaff(s) {
  return { id: s.id, name: s.name, role: s.role, floorScope: s.floor_scope ? JSON.parse(s.floor_scope) : null };
}
function mustConsumption(db, id) {
  const con = db.prepare('SELECT * FROM consumption_items WHERE id=?').get(id);
  if (!con) throw new HttpError(404, '消费记录不存在', 'unknown_consumption');
  return con;
}
function mustOrder(db, id) {
  const o = db.prepare('SELECT * FROM stay_orders WHERE id=?').get(id);
  if (!o) throw new HttpError(404, '订单不存在', 'unknown_order');
  return o;
}
function roomOfOrder(db, orderId) {
  const room = db.prepare('SELECT r.* FROM stay_orders o JOIN rooms r ON r.id=o.room_id WHERE o.id=?').get(orderId);
  if (!room) throw new HttpError(404, '订单客房不存在', 'unknown_room');
  return room;
}

function readRaw(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}
function sanitizePhoto(name) {
  return name.replace(/[^a-zA-Z0-9._-]/g, '_').slice(-120);
}

function serveStatic(pathname, res) {
  // 包装照片：/photos/<filename> -> 本地 photos 目录（禁止路径穿越）
  if (pathname.startsWith('/photos/')) {
    const name = path.basename(decodeURIComponent(pathname));
    const file = path.join(PHOTO_DIR, name);
    if (!file.startsWith(PHOTO_DIR) || !fs.existsSync(file)) {
      res.writeHead(404); return res.end('not found');
    }
    res.writeHead(200, { 'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    return fs.createReadStream(file).pipe(res);
  }
  let rel = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  let file = path.join(PUBLIC, rel);
  if (!file.startsWith(PUBLIC)) {
    res.writeHead(403); return res.end('forbidden');
  }
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(PUBLIC, 'index.html'); // 单页应用回退
  }
  const ext = path.extname(file).toLowerCase();
  res.writeHead(200, { 'content-type': MIME[ext] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

function start() {
  fs.mkdirSync(PHOTO_DIR, { recursive: true });
  // 一条本地启动命令：数据库不存在时自动加载在住/待退/争议样例
  if (!fs.existsSync(DB_PATH)) {
    console.log('未发现数据库，载入演示样例…');
    require('./seed').seed(DB_PATH);
  }
  db = open(DB_PATH);
  const server = createServer(db);
  reminders.start(db, 5 * 60 * 1000);
  server.listen(PORT, () => {
    console.log(`🏨 迷你吧平台已启动: http://localhost:${PORT}`);
    console.log(`   数据库: ${DB_PATH}`);
  });
}

if (require.main === module) start();

module.exports = { createServer, DB_PATH, PHOTO_DIR };
