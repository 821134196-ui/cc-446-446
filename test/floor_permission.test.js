'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { createServer } = require('../src/server');
const { buildScenario } = require('./helpers');

function startServer(db) {
  return new Promise((resolve) => {
    const server = createServer(db);
    server.listen(0, () => {
      const { port } = server.address();
      resolve({
        port,
        close: () => new Promise((r) => server.close(r)),
      });
    });
  });
}

async function req(port, { method = 'GET', path, staff = 'mgr', body }) {
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method,
    headers: {
      'X-Staff-Id': staff,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  return { status: res.status, json };
}

test('楼层权限：服务员/前台只能访问所辖楼层，越层返回 403，经理可跨层', async () => {
  const { db } = buildScenario();
  const { port, close } = await startServer(db);
  try {
    // 3 层服务员读自己楼层 -> 200
    const own = await req(port, { path: '/api/rooms/301/count-sheet', staff: 'att3' });
    assert.equal(own.status, 200);

    // 3 层服务员读 4 层 -> 403
    const cross = await req(port, { path: '/api/rooms/401/count-sheet', staff: 'att3' });
    assert.equal(cross.status, 403);
    assert.equal(cross.json.error, 'floor_forbidden');

    // 3 层服务员不能向 4 层提交盘点
    const post = await req(port, {
      method: 'POST', path: '/api/rooms/401/counts', staff: 'att3',
      body: { baseVersion: 0, lines: [] },
    });
    assert.equal(post.status, 403);

    // 楼层列表只出现所辖楼层
    const board = await req(port, { path: '/api/floorboard', staff: 'fd3' });
    assert.deepEqual(board.json.floors.map((f) => f.floor), [3]);

    // 经理可见全部楼层
    const mgrBoard = await req(port, { path: '/api/floorboard', staff: 'mgr' });
    assert.deepEqual(mgrBoard.json.floors.map((f) => f.floor).sort(), [3, 4]);
  } finally {
    await close();
  }
});

test('免单/冲销在 HTTP 层要求经理身份，经理成功', async () => {
  const { db } = buildScenario();
  const { port, close } = await startServer(db);
  try {
    // 服务员盘点 301 产生一笔待确认水费
    const sheet = await req(port, { path: '/api/rooms/301/count-sheet', staff: 'att3' });
    const counted = await req(port, {
      method: 'POST', path: '/api/rooms/301/counts', staff: 'att3',
      body: { baseVersion: 0, lines: [{ productId: 'p_water', countedQty: 3, replenishQty: 0 }] },
    });
    const cid = counted.json.consumptions[0].id;

    // 前台先确认计费
    const confirm = await req(port, { method: 'POST', path: `/api/consumptions/${cid}/confirm`, staff: 'fd3' });
    assert.equal(confirm.status, 200);

    // 前台尝试冲销 -> 403
    const fdRev = await req(port, {
      method: 'POST', path: `/api/consumptions/${cid}/reverse`, staff: 'fd3',
      body: { reason: '无权' },
    });
    assert.equal(fdRev.status, 403);
    assert.equal(fdRev.json.error, 'manager_required');

    // 经理缺原因 -> 400
    const mgrNoReason = await req(port, {
      method: 'POST', path: `/api/consumptions/${cid}/reverse`, staff: 'mgr', body: {},
    });
    assert.equal(mgrNoReason.status, 400);

    // 经理 + 原因 -> 200
    const mgrOk = await req(port, {
      method: 'POST', path: `/api/consumptions/${cid}/reverse`, staff: 'mgr',
      body: { reason: '客人否认，经理核实' },
    });
    assert.equal(mgrOk.status, 200);
  } finally {
    await close();
  }
});
