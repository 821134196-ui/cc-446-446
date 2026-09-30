'use strict';

// 模拟外部适配器：酒店 PMS 房态、支付/结算网关。
// 真实部署可替换为 HTTP 客户端；此处返回确定性的演示结果。

const rooms = require('../domain/stays');

/** 模拟 PMS：根据内部订单推导对外房态，并附加演示用清洁标记 */
function fetchPmsRoomStatus(db, roomId) {
  const room = rooms.getRoom(db, roomId);
  if (!room) return null;
  const map = {
    vacant: 'VD',           // Vacant Dirty 待清洁
    occupied: 'OC',         // Occupied Clean
    pending_checkout: 'OD',// Occupied 待退
    disputed: 'DX',
    maintenance: 'OO',
  };
  return {
    roomId: room.id,
    floor: room.floor,
    pmsCode: map[room.status] || 'VD',
    housekeepingReady: room.status === 'vacant',
    source: 'mock-pms',
    fetchedAt: new Date().toISOString(),
  };
}

/** 模拟结算网关：返回成功结算凭据（演示，无真实支付） */
function submitSettlement({ orderId, amountCents, guestAnon }) {
  const ref = `MOCK-${orderId.slice(-6).toUpperCase()}-${Math.abs(hash(`${orderId}:${amountCents}`)).toString(36).toUpperCase().slice(0, 6)}`;
  return {
    ok: true,
    reference: ref,
    amountCents,
    currency: 'CNY',
    guest: guestAnon,
    capturedAt: new Date().toISOString(),
    provider: 'mock-settlement',
  };
}

function hash(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = (h << 5) - h + str.charCodeAt(i);
    h |= 0;
  }
  return h;
}

module.exports = { fetchPmsRoomStatus, submitSettlement };
