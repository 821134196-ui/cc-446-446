'use strict';

const { uid, now } = require('../util');
const inv = require('../domain/inventory');

/**
 * 进程内提醒任务：定时扫描临期批次与在住未盘客房，写入 notifications。
 * 不依赖外部调度器，server 启动时 start()，退出时 stop()。
 */
function scan(db) {
  const created = [];

  // 1) 临期批次（补货车 + 房内），效期 <= 阈值天数
  const nearBatches = db.prepare(
    `SELECT b.*, p.name AS product_name,
            CAST(CEIL((julianday(b.expires_at) - julianday('now'))) AS INTEGER) AS days_left
       FROM batches b JOIN products p ON p.id = b.product_id
      WHERE b.status = 'active' AND b.qty_remaining > 0
        AND julianday(b.expires_at) - julianday('now') <= ?
      ORDER BY b.expires_at`
  ).all(inv.NEAR_EXPIRY_DAYS);
  for (const b of nearBatches) {
    const existed = db.prepare("SELECT 1 FROM notifications WHERE type='near_expiry' AND ref=? AND read=0").get(b.id);
    if (existed) continue;
    const word = b.days_left <= 0 ? '已到期' : `还剩 ${b.days_left} 天`;
    const id = uid('ntf');
    db.prepare("INSERT INTO notifications(id, type, message, ref, created_at) VALUES (?, 'near_expiry', ?, ?, ?)")
      .run(id, `批次 ${b.lot_no}（${b.product_name}）${word}，请安排报损或优先上架`, b.id, now());
    created.push(id);
  }

  // 2) 在住客房超过 24 小时未盘点（未盘提醒）
  const stale = db.prepare(
    `SELECT r.id AS room_id, r.floor, r.label, o.id AS order_id, o.check_in_at,
            (SELECT MAX(submitted_at) FROM inventory_counts c WHERE c.room_id = r.id) AS last_submit
       FROM rooms r JOIN stay_orders o ON o.id = r.current_order_id
      WHERE r.status = 'occupied'
        AND ( (SELECT MAX(submitted_at) FROM inventory_counts c WHERE c.room_id = r.id) IS NULL
           OR julianday('now') - julianday((SELECT MAX(submitted_at) FROM inventory_counts c WHERE c.room_id = r.id)) > 1 )
      ORDER BY r.floor, r.label`
  ).all();
  for (const s of stale) {
    const ref = `room:${s.room_id}`;
    const existed = db.prepare("SELECT 1 FROM notifications WHERE type='uncounted' AND ref=? AND read=0").get(ref);
    if (existed) continue;
    const id = uid('ntf');
    db.prepare("INSERT INTO notifications(id, type, message, ref, created_at) VALUES (?, 'uncounted', ?, ?, ?)")
      .run(id, `${s.floor} 层 ${s.label} 在住超过 24 小时未盘点，请安排服务员盘点`, ref, now());
    created.push(id);
  }

  return created;
}

function list(db, onlyUnread = false) {
  return db.prepare(
    `SELECT * FROM notifications ${onlyUnread ? 'WHERE read = 0' : ''} ORDER BY created_at DESC`
  ).all();
}

function markRead(db, id) {
  db.prepare('UPDATE notifications SET read = 1 WHERE id = ?').run(id);
}

let timer = null;
function start(db, intervalMs = 60 * 60 * 1000) {
  stop();
  const run = () => {
    try { scan(db); } catch (_) { /* 提醒失败不影响主流程 */ }
  };
  run(); // 启动即扫一次
  timer = setInterval(run, intervalMs);
  if (timer.unref) timer.unref();
}
function stop() {
  if (timer) { clearInterval(timer); timer = null; }
}

module.exports = { scan, list, markRead, start, stop };
