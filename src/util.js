'use strict';

const crypto = require('node:crypto');

const uid = (prefix) => `${prefix}_${crypto.randomBytes(6).toString('hex')}`;

const now = () => new Date().toISOString();
const today = () => now().slice(0, 10);

const addDays = (base, days) => new Date(base.getTime() + days * 86400000);
const isoDaysFromNow = (days, base = new Date()) => addDays(base, days).toISOString();

/** 距到期还有多少天（负数表示已过期），按本地日历日取整 */
const daysUntil = (iso) => {
  const ms = new Date(iso).getTime() - Date.now();
  return Math.ceil(ms / 86400000);
};

const isExpired = (iso) => new Date(iso).getTime() <= Date.now();

/** 分 -> 元，金额一律保留两位小数 */
const yuan = (cents) => (Number(cents || 0) / 100).toFixed(2);

class HttpError extends Error {
  constructor(status, message, code) {
    super(message);
    this.status = status;
    this.code = code || 'error';
  }
}

/** 在事务中执行 fn，异常自动回滚 */
function tx(db, fn) {
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = fn();
    db.exec('COMMIT');
    return result;
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}

function audit(db, staffId, action, detail) {
  db.prepare(
    `INSERT INTO audit_log(id, at, staff_id, action, detail) VALUES (?, ?, ?, ?, ?)`
  ).run(uid('log'), now(), staffId, action, detail ? JSON.stringify(detail) : null);
}

module.exports = { uid, now, today, addDays, isoDaysFromNow, daysUntil, isExpired, yuan, HttpError, tx, audit };
