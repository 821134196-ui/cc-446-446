'use strict';

const { HttpError } = require('../util');

function getStaff(db, staffId) {
  const s = db.prepare('SELECT * FROM staff WHERE id = ? AND active = 1').get(staffId);
  if (!s) throw new HttpError(401, '员工不存在或已停用', 'unknown_staff');
  return s;
}

function isManager(staff) {
  return staff.role === 'manager';
}

function scopeFloors(staff) {
  if (isManager(staff) || !staff.floor_scope) return null; // null = 全部楼层
  return JSON.parse(staff.floor_scope);
}

function canAccessFloor(staff, floor) {
  const floors = scopeFloors(staff);
  return floors === null || floors.includes(floor);
}

function requireFloor(staff, floor) {
  if (!canAccessFloor(staff, floor)) {
    throw new HttpError(403, `无权访问 ${floor} 层`, 'floor_forbidden');
  }
}

function requireManager(staff, action = '该操作') {
  if (!isManager(staff)) {
    throw new HttpError(403, `${action}需要经理权限`, 'manager_required');
  }
}

/** 免单 / 冲销原因必填且需经理 */
function requireManagerReason(staff, reason, action = '该操作') {
  requireManager(staff, action);
  if (!reason || !String(reason).trim()) {
    throw new HttpError(400, `${action}必须留存原因`, 'reason_required');
  }
}

module.exports = { getStaff, isManager, scopeFloors, canAccessFloor, requireFloor, requireManager, requireManagerReason };
