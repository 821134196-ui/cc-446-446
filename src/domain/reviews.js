'use strict';

const { uid, now } = require('../util');
const { HttpError, tx, audit } = require('../util');
const billing = require('./billing');

const FLOWS = ['guest_deny', 'cross_room', 'near_expiry', 'cart_shortage', 'offline_reupload', 'post_checkout'];

function openReview(db, { flow, orderId, consumptionId, openedBy, note }) {
  if (!FLOWS.includes(flow)) throw new HttpError(400, '未知复核流程', 'bad_flow');
  const id = uid('rev');
  db.prepare(
    `INSERT INTO reviews(id, flow, consumption_id, order_id, state, opened_by, note, created_at)
     VALUES (?, ?, ?, ?, 'open', ?, ?, ?)`
  ).run(id, flow, consumptionId || null, orderId, openedBy || null, note || null, now());
  if (consumptionId) {
    db.prepare('UPDATE consumption_items SET review_flow = ? WHERE id = ?').run(flow, consumptionId);
  }
  return getReview(db, id);
}

function getReview(db, id) {
  return db.prepare('SELECT * FROM reviews WHERE id = ?').get(id);
}

function listReviews(db, state) {
  const sql = state
    ? 'SELECT * FROM reviews WHERE state = ? ORDER BY created_at DESC'
    : 'SELECT * FROM reviews ORDER BY created_at DESC';
  return state ? db.prepare(sql).all(state) : db.prepare(sql).all();
}

/**
 * 解决复核工单。不同 flow 的处理动作：
 *  - guest_deny      : 客人否认 -> 经理核实后冲销（deny），不计费
 *  - cross_room      : 跨房放错 -> 计入 charged_order_id（relocated），从错误订单移除
 *  - near_expiry     : 批次临期 -> 转报损处理或免单（comp）
 *  - cart_shortage   : 补货车短缺 -> 登记，待补货后回填（resolved，无金额动作）
 *  - offline_reupload: 离线重传 -> 核对去重，确认保留或冲销
 *  - post_checkout   : 退房后补录 -> 经理确认补账（在已冻结/关闭订单上补一条调整）
 */
function resolveReview(db, { reviewId, staff, action, reason, targetOrderId, note }) {
  return tx(db, () => {
    const review = getReview(db, reviewId);
    if (!review) throw new HttpError(404, '复核工单不存在', 'unknown_review');
    if (review.state !== 'open') throw new HttpError(409, '工单已处理', 'review_closed');

    const con = review.consumption_id
      ? db.prepare('SELECT * FROM consumption_items WHERE id = ?').get(review.consumption_id)
      : null;
    let result;

    switch (review.flow) {
      case 'guest_deny': {
        if (!con) throw new HttpError(400, '缺少关联消费', 'no_consumption');
        // 客人否认：经理确认后该消费不成立
        if (action === 'uphold') {
          result = '维持计费';
          db.prepare("UPDATE consumption_items SET status='confirmed', resolved_at=?, resolved_by=? WHERE id=?")
            .run(now(), staff.id, con.id);
        } else {
          // 需要经理 + 原因，冲销已入账金额
          requireReasonFor(reason);
          db.prepare("UPDATE consumption_items SET status='denied', reason_code=?, note=?, resolved_at=?, resolved_by=? WHERE id=?")
            .run('guest_deny', reason, now(), staff.id, con.id);
          billing.reverseConsumption(db, con, staff, reason);
          result = '客人否认成立，已冲销';
        }
        break;
      }
      case 'cross_room': {
        if (!con) throw new HttpError(400, '缺少关联消费', 'no_consumption');
        if (action !== 'relocate') {
          requireReasonFor(reason);
          db.prepare("UPDATE consumption_items SET status='written_off', reason_code='cross_room_void', note=?, resolved_at=?, resolved_by=? WHERE id=?")
            .run(reason, now(), staff.id, con.id);
          billing.reverseConsumption(db, con, staff, reason);
          result = '跨房放错撤销';
          break;
        }
        if (!targetOrderId) throw new HttpError(400, '跨房放错需指定计入订单', 'target_required');
        const target = db.prepare('SELECT * FROM stay_orders WHERE id = ?').get(targetOrderId);
        if (!target) throw new HttpError(404, '目标订单不存在', 'unknown_target');
        // 经理需核实两个楼层权限
        const rooms = [con.order_id, targetOrderId].map((oid) =>
          db.prepare('SELECT * FROM rooms WHERE current_order_id = ? OR id = (SELECT room_id FROM stay_orders WHERE id=?)').get(oid, oid));
        for (const r of rooms) {
          const { requireFloor } = require('./staff');
          requireFloor(staff, r.floor);
        }
        requireReasonFor(reason, true);
        // 从原订单移除（已入账则冲销），计入目标订单
        billing.reverseConsumption(db, con, staff, `跨房放错转出：${reason || '客人移动'}`);
        db.prepare(
          "UPDATE consumption_items SET status='relocated', charged_order_id=?, reason_code='cross_room', note=?, resolved_at=?, resolved_by=? WHERE id=?"
        ).run(targetOrderId, reason || null, now(), staff.id, con.id);
        billing.chargeRelocated(db, con, targetOrderId, staff, reason || '跨房放错补计');
        result = `已转入订单 ${targetOrderId}`;
        break;
      }
      case 'near_expiry': {
        // 临期批次上架后被消费：经理决定免单（酒店承担）或维持
        if (!con) throw new HttpError(400, '缺少关联消费', 'no_consumption');
        if (action === 'uphold') {
          db.prepare("UPDATE consumption_items SET status='confirmed', resolved_at=?, resolved_by=? WHERE id=?")
            .run(now(), staff.id, con.id);
          result = '维持计费';
        } else {
          requireReasonFor(reason);
          db.prepare("UPDATE consumption_items SET status='comp', reason_code='near_expiry', note=?, resolved_at=?, resolved_by=? WHERE id=?")
            .run(reason, now(), staff.id, con.id);
          billing.compConsumption(db, con, staff, reason);
          result = '临期免单';
        }
        break;
      }
      case 'cart_shortage': {
        // 无金额动作：登记短缺，等待补货后回填
        result = action === 'backfill' ? '已补货回填' : '短缺已登记，待补货';
        break;
      }
      case 'offline_reupload': {
        if (action === 'discard' && con) {
          requireReasonFor(reason);
          db.prepare("UPDATE consumption_items SET status='written_off', reason_code='offline_dup', note=?, resolved_at=?, resolved_by=? WHERE id=?")
            .run(reason, now(), staff.id, con.id);
          billing.reverseConsumption(db, con, staff, `离线重传统重：${reason}`);
          result = '重传统重，已冲销';
        } else {
          if (con) {
            db.prepare("UPDATE consumption_items SET status='confirmed', resolved_at=?, resolved_by=? WHERE id=?")
              .run(now(), staff.id, con.id);
          }
          result = '离线盘点核对无误，保留';
        }
        break;
      }
      case 'post_checkout': {
        // 退房后补录：经理确认，在（可能已关闭的）订单上补一条调整账
        if (!targetOrderId) throw new HttpError(400, '补录需指定订单', 'target_required');
        requireReasonFor(reason);
        const amountCents = Number(note && note.amountCents) || (con ? con.amount_cents : 0);
        if (amountCents <= 0) throw new HttpError(400, '补录金额必须大于 0', 'bad_amount');
        billing.postCheckoutAdjustment(db, {
          orderId: targetOrderId, amountCents, staff, reason, consumptionId: con ? con.id : null,
        });
        result = `退房后补录 ${amountCents} 分`;
        break;

      }
      default:
        throw new HttpError(400, '未支持的流程', 'bad_flow');
    }

    db.prepare("UPDATE reviews SET state='resolved', result=?, closed_at=? WHERE id=?")
      .run(result, now(), reviewId);
    audit(db, staff.id, 'resolve_review', { reviewId, flow: review.flow, action, result });
    return getReview(db, reviewId);

    function requireReasonFor(r, optional = false) {
      const { requireManager, requireManagerReason } = require('./staff');
      requireManager(staff, '争议复核');
      if (!optional) requireManagerReason(staff, r, '争议处理');
    }
  });
}

module.exports = { FLOWS, openReview, getReview, listReviews, resolveReview };
