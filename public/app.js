'use strict';

const state = {
  staffId: localStorage.getItem('staffId') || 'stf_mgr',
  staff: null,
  staffList: [],
  view: 'floorboard',
  selectedRoom: null,
  floorboard: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const yuan = (c) => (Number(c || 0) / 100).toFixed(2);
const moneyClass = (c) => (c > 0 ? 'pos' : c < 0 ? 'neg' : 'zero');
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—');
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString('zh-CN') : '—');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STATUS_LABEL = {
  vacant: '空房', occupied: '在住', pending_checkout: '待退', disputed: '争议', maintenance: '维修',
  checked_in: '在住', frozen: '已冻结', closed: '已关闭',
};
const CON_STATUS = { pending: '待确认', confirmed: '已确认', denied: '客人否认', comp: '免单', relocated: '跨房转入', written_off: '已核销' };
const FLOW_LABEL = {
  guest_deny: '客人否认', cross_room: '跨房放错', near_expiry: '批次临期',
  cart_shortage: '补货车短缺', offline_reupload: '离线重传', post_checkout: '退房后补录',
};

async function api(method, url, body, rawOpts = {}) {
  const opts = { method, headers: { 'X-Staff-Id': state.staffId, ...rawOpts.headers } };
  if (body !== undefined && !rawOpts.raw) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  } else if (rawOpts.raw) {
    opts.body = body;
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || `请求失败 ${res.status}`);
    err.code = data.error;
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' error' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = 'toast'; }, 2600);
}

function setTitle(v) {
  $('#viewTitle').textContent = ({
    floorboard: '楼层房态', count: '房内盘点', cart: '补货车清单',
    review: '消费复核', expiry: '效期报损', checkout: '退房结算',
  })[v] || '';
}

function switchView(v) {
  state.view = v;
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  setTitle(v);
  render();
}

/* ---------------- 初始化 ---------------- */
async function init() {
  document.querySelectorAll('.nav-item').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.view)));
  $('#scanBtn').addEventListener('click', async () => {
    try { await api('POST', '/api/reminders/scan'); toast('已扫描临期与未盘提醒'); loadNotifs(); }
    catch (e) { toast(e.message, true); }
  });
  $('#staffSelect').addEventListener('change', async (e) => {
    state.staffId = e.target.value;
    localStorage.setItem('staffId', state.staffId);
    await bootstrapStaff();
    render();
  });
  await bootstrapStaff();
  loadNotifs();
  setInterval(loadNotifs, 30000);
  render();
}

async function bootstrapStaff() {
  const [{ staff: me }, { staff: list }] = await Promise.all([
    api('GET', '/api/whoami'), api('GET', '/api/staff'),
  ]);
  state.staff = me;
  state.staffList = list;
  const sel = $('#staffSelect');
  sel.innerHTML = list.map((s) => `<option value="${s.id}" ${s.id === state.staffId ? 'selected' : ''}>${esc(s.name)}（${({ manager: '经理', frontdesk: '前台', attendant: '服务员' })[s.role]}）</option>`).join('');
  $('#staffScope').textContent = me.floorScope ? `可访问楼层：${me.floorScope.join('、')}` : '可访问全部楼层';
}

async function loadNotifs() {
  try {
    const { notifications } = await api('GET', '/api/notifications?unread=1');
    const area = $('#notifArea');
    area.innerHTML = notifications.slice(0, 4).map((n) =>
      `<span class="notif ${n.type}">${n.type === 'near_expiry' ? '⏳' : '🧾'} ${esc(n.message)}</span>`).join('');
    const open = (await api('GET', '/api/reviews?state=open')).reviews.length;
    const badge = $('#navReviewBadge');
    badge.textContent = open;
    badge.classList.toggle('hidden', open === 0);
  } catch (_) {}
}

/* ---------------- 主渲染 ---------------- */
async function render() {
  const c = $('#content');
  c.innerHTML = '<div class="empty">加载中…</div>';
  try {
    if (state.view === 'floorboard') await renderFloorboard(c);
    else if (state.view === 'count') await renderCount(c);
    else if (state.view === 'cart') await renderCart(c);
    else if (state.view === 'review') await renderReview(c);
    else if (state.view === 'expiry') await renderExpiry(c);
    else if (state.view === 'checkout') await renderCheckout(c);
  } catch (e) {
    c.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

/* ---------------- 楼层房态（紧凑列表） ---------------- */
async function renderFloorboard(c) {
  const { floors } = await api('GET', '/api/floorboard');
  c.innerHTML = '';
  if (!floors.length) { c.appendChild(el('div', 'empty', '当前身份没有可访问的楼层')); return; }
  for (const f of floors) {
    const block = el('div', 'floor-block');
    const occ = f.rooms.filter((r) => r.order_id).length;
    block.innerHTML = `
      <div class="floor-head"><h2>${f.floor} 层</h2>
        <span class="floor-meta">共 ${f.rooms.length} 间 · 在住/待退 ${occ} 间</span></div>`;
    const table = el('table', 'room-table');
    table.innerHTML = `
      <thead><tr><th>房号</th><th>房态</th><th>匿名客人</th><th>计划退房</th><th>待办</th><th></th></tr></thead>
      <tbody>${f.rooms.map((r) => `
        <tr data-room="${r.id}">
          <td><span class="room-link"><span class="num">🔑</span>${esc(r.label)}</span></td>
          <td><span class="status-pill st-${r.status}">${STATUS_LABEL[r.status] || r.status}</span></td>
          <td>${r.guest_anon ? esc(r.guest_anon) : '<span class="muted">—</span>'}</td>
          <td class="muted">${r.scheduled_checkout_at ? fmtDate(r.scheduled_checkout_at) : '—'}</td>
          <td><div class="mini-badges">
            ${r.pending_count ? `<span class="mini-badge mb-pending">${r.pending_count} 待确认</span>` : ''}
            ${r.review_count ? `<span class="mini-badge mb-review">${r.review_count} 争议</span>` : ''}
          </div></td>
          <td class="right">${r.order_id
            ? `<button class="btn sm ghost" data-act="count">盘点</button> ${r.order_status === 'frozen' ? `<button class="btn sm gold" data-act="checkout">结算</button>` : `<button class="btn sm" data-act="freeze">退房冻结</button>`}`
            : `<span class="hint">无在住订单</span>`}</td>
        </tr>`).join('')}
      </tbody>`;
    table.querySelectorAll('tbody tr').forEach((tr) => {
      tr.addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        const roomId = tr.dataset.room;
        if (btn) {
          if (btn.dataset.act === 'count') { state.selectedRoom = roomId; switchView('count'); }
          if (btn.dataset.act === 'freeze') freezeRoom(roomId);
          if (btn.dataset.act === 'checkout') { state.selectedRoom = roomId; switchView('checkout'); }
          return;
        }
        state.selectedRoom = roomId;
        switchView('count');
      });
    });
    block.appendChild(table);
    c.appendChild(block);
  }
}

async function freezeRoom(roomId) {
  const { floors } = await api('GET', '/api/floorboard');
  const room = floors.flatMap((f) => f.rooms).find((r) => r.id === roomId);
  if (!room || !room.order_id) return;
  try {
    await api('POST', `/api/orders/${room.order_id}/freeze`);
    toast('已退房冻结，最后一次盘点已锁定');
    render(); loadNotifs();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 房内盘点（图片/当前数/补货数 三列） ---------------- */
let countCtx = null;
async function renderCount(c) {
  c.innerHTML = '';
  if (!state.selectedRoom) {
    c.appendChild(roomPickerPanel('count'));
    await bindRoomPicker(c, 'count');
    return;
  }
  const sheet = await api('GET', `/api/rooms/${state.selectedRoom}/count-sheet`);
  countCtx = sheet;
  const locked = sheet.order && sheet.order.status === 'frozen';
  const grid = el('div', 'count-grid');

  const left = el('div', 'panel count-table');
  const stale = sheet.lastCount && sheet.baseVersion !== undefined;
  left.innerHTML = `
    <div class="count-head">
      <h3 style="margin:0">${esc(sheet.room.label)} · 房内盘点</h3>
      <span class="vbanner ${stale ? '' : ''}">基线版本 v${sheet.baseVersion}</span>
      ${locked ? '<span class="status-pill st-pending_checkout">已退房冻结</span>' : ''}
      <span class="spacer"></span>
      <button class="btn sm ghost" id="pickOtherRoom">切换客房</button>
    </div>
    <p class="hint" style="margin:0 0 10px">按实际陈列清点：系统按「当前应有数 − 实盘数」生成待确认消费，并从补货车按先进先出转入新批次。</p>
    <table class="data">
      <colgroup><col class="c-img"><col class="c-name"><col class="c-current"><col class="c-repl"></colgroup>
      <thead><tr><th>商品图片</th><th>商品</th><th>当前数（实盘）</th><th>补货数</th></tr></thead>
      <tbody>${sheet.items.map((it) => `
        <tr>
          <td><img class="prod-img" src="${it.image}" alt="${esc(it.name)}"></td>
          <td><div class="prod-name">${esc(it.name)}</div>
            <div class="prod-sub">¥${yuan(it.priceCents)} · 上限 ${it.displayCap} · 应有 ${it.current}</div>
            <div class="batch-strip">${it.batches.map((b) =>
              `<span class="lot">批次 ${esc(b.lotNo)}×${b.qty}（${fmtDate(b.expiresAt)}）</span>`).join('')}</div>
          </td>
          <td><input class="qty-input" data-pid="${it.productId}" data-field="counted" type="number" min="0" value="${it.current}" ${locked ? 'disabled' : ''}></td>
          <td><input class="qty-input" data-pid="${it.productId}" data-field="replenish" type="number" min="0" value="0" ${locked ? 'disabled' : ''}>
            <div class="hint">建议 ${it.suggestReplenish}</div></td>
        </tr>`).join('')}
      </tbody>
    </table>
    <div class="row" style="margin-top:14px">
      <button class="btn gold" id="fillSuggest">填入建议补货</button>
      <button class="btn" id="submitCount" ${locked ? 'disabled' : ''}>提交盘点（v${sheet.baseVersion} → v${sheet.baseVersion + 1}）</button>
      <span class="spacer"></span>
      <button class="btn sm ghost" id="offlineBtn" ${locked ? 'disabled' : ''}>模拟离线重传</button>
    </div>`;

  const right = el('div');
  right.appendChild(await orderSidePanel(sheet));

  grid.appendChild(left);
  grid.appendChild(right);
  c.appendChild(grid);

  $('#pickOtherRoom', c).onclick = () => { state.selectedRoom = null; render(); };
  $('#fillSuggest', c).onclick = () => sheet.items.forEach((it) => {
    const repl = $(`.qty-input[data-pid="${it.productId}"][data-field="replenish"]`, c);
    const counted = $(`.qty-input[data-pid="${it.productId}"][data-field="counted"]`, c);
    repl.value = it.suggestReplenish;
    counted.value = it.current;
  });
  $('#submitCount', c).onclick = () => submitCount(c, sheet, false);
  $('#offlineBtn', c).onclick = () => submitCount(c, sheet, true);
}

async function submitCount(c, sheet, offline) {
  const lines = [...c.querySelectorAll('.qty-input[data-field="counted"]')].map((inp) => ({
    productId: inp.dataset.pid,
    countedQty: Number(inp.value),
    replenishQty: Number($(`.qty-input[data-pid="${inp.dataset.pid}"][data-field="replenish"]`, c).value),
  }));
  const clientGuid = offline ? `offline-${state.selectedRoom}-${sheet.baseVersion}-${Math.random().toString(16).slice(2, 8)}` : undefined;
  try {
    const r = await api('POST', `/api/rooms/${state.selectedRoom}/counts`, {
      baseVersion: sheet.baseVersion, lines, clientGuid,
      device: offline ? 'pda-offline' : 'web',
    });
    if (r.deduplicated) { toast('离线重传：相同盘点单已存在，已去重（不重复入账）'); }
    else {
      const parts = [`盘点 v${r.count.version} 已提交`];
      if (r.consumptions.length) parts.push(`生成 ${r.consumptions.length} 笔待确认消费`);
      if (r.shortages.length) parts.push(`补货车短缺 ${r.shortages.length} 项`);
      if (r.anomalies.length) parts.push(`异常 ${r.anomalies.length} 项`);
      toast(parts.join('，'));
    }
    render(); loadNotifs();
  } catch (e) {
    if (e.code === 'version_conflict') {
      toast('版本冲突：基线已过期，请刷新后重盘（旧版本未覆盖新快照）', true);
      render();
    } else toast(e.message, true);
  }
}

async function orderSidePanel(sheet) {
  const panel = el('div', 'panel');
  if (!sheet.order) { panel.innerHTML = '<h3>订单</h3><div class="empty">该客房当前无在住订单</div>'; return panel; }
  const data = await api('GET', `/api/orders/${sheet.order.id}/bill`);
  const pending = data.consumptions.filter((x) => x.status === 'pending');
  panel.innerHTML = `
    <h3>本单消费与账单</h3>
    <dl class="kv">
      <dt>客人</dt><dd>${esc(data.order.guest_anon)}</dd>
      <dt>状态</dt><dd><span class="status-pill st-${data.room.status}">${STATUS_LABEL[data.order.status]}</span></dd>
      <dt>入住</dt><dd>${fmtTime(data.order.check_in_at)}</dd>
    </dl>
    <table class="data" style="margin-top:10px">
      <thead><tr><th>商品</th><th>数量</th><th>状态</th><th class="right">金额</th></tr></thead>
      <tbody>${data.consumptions.length ? data.consumptions.map((x) => `
        <tr><td>${x.product_id ? esc(PID_NAME[x.product_id] || x.product_id.slice(-6)) : '调整'}</td>
        <td class="mono">${x.qty || ''}</td>
        <td><span class="tag ${x.status === 'pending' ? 'near' : x.status === 'confirmed' ? 'ok' : ''}">${CON_STATUS[x.status] || x.status}</span></td>
        <td class="money ${moneyClass(x.amount_cents)}">${x.amount_cents ? yuan(x.amount_cents) : '—'}</td></tr>`).join('')
        : '<tr><td colspan="4" class="empty">暂无消费</td></tr>'}</tbody>
    </table>
    <div class="row" style="margin-top:12px">
      <strong>账单合计</strong><span class="spacer"></span>
      <span class="money" style="font-size:18px;color:var(--navy)">¥${data.bill.totalYuan}</span>
    </div>
    ${pending.length ? `<div class="hint" style="margin-top:8px">${pending.length} 笔待确认，请到「消费复核」处理</div>` : ''}`;
  return panel;
}
const PID_NAME = {};

function roomPickerPanel() {
  const p = el('div', 'panel');
  p.innerHTML = '<h3>选择客房</h3><div id="roomPickerGrid" class="row"></div>';
  return p;
}
async function bindRoomPicker(c) {
  const { floors } = await api('GET', '/api/floorboard');
  const grid = $('#roomPickerGrid', c);
  grid.innerHTML = floors.map((f) =>
    `<div style="width:100%"><strong style="color:var(--navy)">${f.floor} 层</strong>
      <div class="row" style="margin:6px 0 14px">${f.rooms.map((r) =>
        `<button class="btn sm ghost" data-room="${r.id}">${esc(r.label)}</button>`).join('')}</div></div>`).join('');
  grid.querySelectorAll('button').forEach((b) => b.onclick = () => { state.selectedRoom = b.dataset.room; render(); });
}

/* ---------------- 补货车 ---------------- */
async function renderCart(c) {
  const [{ cart }, { products }] = await Promise.all([api('GET', '/api/cart'), api('GET', '/api/products')]);
  products.forEach((p) => { PID_NAME[p.id] = p.name; });
  c.innerHTML = '';
  const panel = el('div', 'panel');
  const isMgr = state.staff.role === 'manager';
  panel.innerHTML = `
    <div class="count-head"><h3 style="margin:0">补货车清单（按批次 FIFO）</h3><span class="spacer"></span>
      ${isMgr ? '<button class="btn gold sm" id="recvBtn">批次入库</button>' : '<span class="hint">批次入库需经理权限</span>'}
    </div>
    <table class="data"><thead><tr><th>商品</th><th>批次</th><th>效期</th><th>状态</th><th class="right">车存量</th></tr></thead>
    <tbody>${cart.flatMap((p) => p.batches.map((b) => `
      <tr><td>${esc(p.name)}</td><td class="mono">${esc(b.lot_no)}</td>
      <td>${fmtDate(b.expires_at)} <span class="tag ${b.days_left <= 0 ? 'expired' : b.days_left <= 3 ? 'near' : ''}">${b.days_left <= 0 ? '已到期' : b.days_left + ' 天'}</span></td>
      <td>${b.status === 'active' ? '<span class="tag ok">可上架</span>' : `<span class="tag ${b.status}">${b.status === 'expired' ? '已过期' : '包装破损'}</span>`}</td>
      <td class="money">${b.qty}</td></tr>`)).join('') || '<tr><td colspan="5" class="empty">补货车为空</td></tr>'}</tbody></table>`;
  c.appendChild(panel);
  if (isMgr) $('#recvBtn').onclick = receiveBatchModal;
}

function receiveBatchModal() {
  openModal('批次入库到补货车', `
    <div class="field"><label>商品</label><select id="fProduct"></select></div>
    <div class="field"><label>批号</label><input id="fLot" placeholder="如 W2603-C"></div>
    <div class="field"><label>到期日期</label><input id="fExp" type="date"></div>
    <div class="field"><label>数量</label><input id="fQty" type="number" min="1" value="6"></div>
  `, async () => {
    await api('POST', '/api/batches/receive', {
      productId: $('#fProduct').value, lotNo: $('#fLot').value,
      expiresAt: new Date($('#fExp').value + 'T23:59:59').toISOString(), qty: Number($('#fQty').value),
    });
    toast('批次已入补货车'); render();
  });
  api('GET', '/api/products').then(({ products }) => {
    $('#fProduct').innerHTML = products.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
    $('#fExp').value = new Date(Date.now() + 180 * 86400000).toISOString().slice(0, 10);
  });
}

/* ---------------- 消费复核（时间线） ---------------- */
async function renderReview(c) {
  const { consumptions } = await api('GET', '/api/consumptions');
  consumptions.forEach((x) => { PID_NAME[x.product_id] = x.product_name; });
  c.innerHTML = '';

  const pending = consumptions.filter((x) => x.status === 'pending');
  const p1 = el('div', 'panel');
  p1.innerHTML = `<h3>待确认消费（盘点减少量）</h3>
    <table class="data"><thead><tr><th>客房</th><th>商品</th><th>数量</th><th>盘点版本</th><th class="right">金额</th><th></th></tr></thead>
    <tbody>${pending.map((x) => `
      <tr><td>${x.floor} 层 ${esc(x.room_label)}</td><td>${esc(x.product_name)}</td>
      <td class="mono">${x.qty}</td><td>v${x.count_version}</td>
      <td class="money pos">¥${yuan(x.amount_cents)}</td>
      <td class="right">
        <button class="btn sm" data-act="confirm" data-id="${x.id}">确认计费</button>
        <button class="btn sm ghost" data-act="deny" data-id="${x.id}">客人否认</button>
        <button class="btn sm danger" data-act="comp" data-id="${x.id}">免单</button>
      </td></tr>`).join('') || '<tr><td colspan="6" class="empty">没有待确认消费</td></tr>'}</tbody></table>`;
  p1.querySelectorAll('button').forEach((b) => b.onclick = () => {
    const id = b.dataset.id;
    if (b.dataset.act === 'confirm') quickAction('POST', `/api/consumptions/${id}/confirm`, {}, '已确认计费');
    if (b.dataset.act === 'deny') openDenyReview(id);
    if (b.dataset.act === 'comp') openCompModal(id);
  });
  c.appendChild(p1);

  // 争议/复核工单
  const { reviews } = await api('GET', '/api/reviews?state=open');
  const { floors } = await api('GET', '/api/floorboard');
  const roomByOrder = {};
  floors.forEach((f) => f.rooms.forEach((r) => { if (r.order_id) roomByOrder[r.order_id] = r; }));

  const p2 = el('div', 'panel');
  p2.innerHTML = `<h3>复核流程工单</h3>
    <table class="data"><thead><tr><th>流程</th><th>关联</th><th>说明</th><th>开立时间</th><th></th></tr></thead>
    <tbody>${reviews.map((rv) => `
      <tr><td><span class="tag flow">${FLOW_LABEL[rv.flow]}</span></td>
      <td class="mono">${rv.consumption_id ? esc(rv.consumption_id.slice(-8)) : '—'}</td>
      <td>${esc(rv.note || '')}</td><td class="muted">${fmtTime(rv.created_at)}</td>
      <td class="right">${rv.flow === 'post_checkout' || rv.flow === 'cross_room'
        ? `<button class="btn sm gold" data-act="resolve" data-id="${rv.id}" data-flow="${rv.flow}">处理</button>`
        : `<button class="btn sm" data-act="resolve" data-id="${rv.id}" data-flow="${rv.flow}">处理</button>`}</td></tr>`)
      .join('') || '<tr><td colspan="5" class="empty">没有开放工单</td></tr>'}</tbody></table>
    <div class="row" style="margin-top:12px">
      <span class="hint">可手动开立：</span>
      <button class="btn sm ghost" data-open="cross_room">跨房放错</button>
      <button class="btn sm ghost" data-open="post_checkout">退房后补录</button>
    </div>`;
  p2.querySelectorAll('button[data-act="resolve"]').forEach((b) => b.onclick = () =>
    resolveReviewModal(b.dataset.id, b.dataset.flow));
  p2.querySelectorAll('button[data-open]').forEach((b) => b.onclick = () =>
    openGenericReview(b.dataset.open, roomByOrder));
  c.appendChild(p2);

  // 已处理消费以账单时间线展开（每个订单一条时间线）
  const handled = consumptions.filter((x) => x.status !== 'pending');
  const orders = [...new Set(handled.map((x) => x.order_id))];
  if (orders.length) {
    const p3 = el('div', 'panel');
    p3.innerHTML = '<h3>争议消费 · 账单时间线</h3>';
    for (const oid of orders) {
      const data = await api('GET', `/api/orders/${oid}/bill`);
      p3.appendChild(billTimelineView(data));
    }
    c.appendChild(p3);
  }
}

function billTimelineView(data) {
  const wrap = el('div', 'screen-only');
  wrap.style.marginBottom = '18px';
  wrap.innerHTML = `
    <div class="row" style="margin-bottom:6px">
      <strong>${data.room.floor} 层 ${esc(data.room.label)}</strong>
      <span class="muted">${esc(data.order.guest_anon)} · ${esc(data.order.id.slice(-8))}</span>
      <span class="status-pill st-${data.room.status}">${STATUS_LABEL[data.order.status]}</span>
      <span class="spacer"></span><span class="money" style="font-weight:700;color:var(--navy)">合计 ¥${data.bill.totalYuan}</span>
    </div>
    <div class="timeline">${data.bill.charges.map((ch) => `
      <div class="tl-item kind-${ch.kind}">
        <div class="tl-top">
          <span class="tl-title">${chargeTitle(ch)}</span>
          <span class="tl-time">${fmtTime(ch.created_at)}</span>
          <span class="tl-amount money ${moneyClass(ch.amount_cents)}">${formatSigned(ch.amount_cents)}</span>
        </div>
        <div class="tl-detail">${esc(ch.reason || '')} · ${KIND_LABEL[ch.kind]} · 经办 ${esc(ch.created_by || '系统')}</div>
      </div>`).join('') || '<div class="hint">账单为空</div>'}
    </div>`;
  return wrap;
}

const KIND_LABEL = { minibar: '消费', comp: '免单', reversal: '冲销', adjustment: '调整' };

function chargeTitle(ch) {
  if (ch.kind === 'minibar') return `迷你吧 · ${esc(ch.product_name || '消费')}`;
  if (ch.kind === 'reversal') return '账单冲销';
  if (ch.kind === 'comp') return '经理免单';
  return '调整 / 补录';
}

function formatSigned(cents) {
  if (cents === 0) return '¥0.00（免单）';
  return (cents < 0 ? '-' : '+') + '¥' + yuan(Math.abs(cents));
}

async function quickAction(method, url, body, okMsg) {
  try { await api(method, url, body); toast(okMsg); render(); loadNotifs(); }
  catch (e) { toast(e.message, true); }
}

function openCompModal(id) {
  openModal('经理免单（需留存原因）', `
    <div class="field"><label>免单原因 <span class="req">*</span></label>
      <select id="fReason"><option value="">请选择</option></select></div>
    <div class="field"><label>补充说明</label><textarea id="fNote"></textarea></div>
    <p class="hint">免单需要经理权限，操作将记入审计。</p>
  `, async () => {
    const reason = $('#fReason').value + ($('#fNote').value ? `：${$('#fNote').value}` : '');
    await api('POST', `/api/consumptions/${id}/comp`, { reason });
    toast('已免单'); render(); loadNotifs();
  });
  loadReasonOptions();
}

function openDenyReview(id) {
  openModal('客人否认消费', `
    <div class="field"><label>否认说明 <span class="req">*</span></label>
      <textarea id="fNote" placeholder="客人称未消费的理由"></textarea></div>
    <p class="hint">先开立「客人否认」复核工单；经理在工单中核实后冲销。</p>
  `, async () => {
    // 找到该消费所属订单
    const { consumptions } = await api('GET', '/api/consumptions');
    const con = consumptions.find((x) => x.id === id);
    await api('POST', '/api/reviews', { flow: 'guest_deny', orderId: con.order_id, consumptionId: id, note: $('#fNote').value });
    toast('已开立客人否认复核工单'); render(); loadNotifs();
  });
}

function resolveReviewModal(id, flow) {
  const fields = {
    guest_deny: ['<label>处理</label><select id="fAction"><option value="deny">否认成立→冲销（经理）</option><option value="uphold">维持计费</option></select>',
      '<label>原因 <span class="req">*</span></label><textarea id="fReason" placeholder="核实结论与原因"></textarea>'],
    cross_room: ['<label>处理</label><select id="fAction"><option value="relocate">转入正确客房订单</option><option value="void">撤销</option></select>',
      '<label>计入订单</label><select id="fTarget"></select>',
      '<label>原因</label><textarea id="fReason"></textarea>'],
    near_expiry: ['<label>处理</label><select id="fAction"><option value="comp">临期免单（经理）</option><option value="uphold">维持计费</option></select>',
      '<label>原因 <span class="req">*</span></label><textarea id="fReason"></textarea>'],
    cart_shortage: ['<label>处理</label><select id="fAction"><option value="log">登记短缺待补货</option><option value="backfill">已补货回填</option></select>'],
    offline_reupload: ['<label>处理</label><select id="fAction"><option value="keep">核对无误保留</option><option value="discard">统重→冲销（经理）</option></select>',
      '<label>原因 <span class="req">*</span></label><textarea id="fReason"></textarea>'],
    post_checkout: ['<label>补录订单</label><select id="fTarget"></select>',
      '<label>金额（分）<span class="req">*</span></label><input id="fAmount" type="number" min="1">',
      '<label>原因 <span class="req">*</span></label><textarea id="fReason" placeholder="退房后补录事由"></textarea>'],
  }[flow] || [];
  openModal(`处理复核 · ${FLOW_LABEL[flow]}`,
    `<div class="field">${fields.join('</div><div class="field">')}</div>`,
    async () => {
      const body = {
        action: $('#fAction') ? $('#fAction').value : 'log',
        reason: $('#fReason') ? $('#fReason').value : undefined,
        targetOrderId: $('#fTarget') ? $('#fTarget').value : undefined,
        note: { amountCents: $('#fAmount') ? Number($('#fAmount').value) : 0 },
      };
      await api('POST', `/api/reviews/${id}/resolve`, body);
      toast('复核工单已处理'); render(); loadNotifs();
    });
  if (flow === 'cross_room' || flow === 'post_checkout') populateOrderSelect('#fTarget');
}

async function openGenericReview(flow, roomByOrder) {
  openModal(`开立复核 · ${FLOW_LABEL[flow]}`, `
    <div class="field"><label>订单</label><select id="fOrder"></select></div>
    <div class="field"><label>说明</label><textarea id="fNote"></textarea></div>
  `, async () => {
    await api('POST', '/api/reviews', { flow, orderId: $('#fOrder').value, note: $('#fNote').value });
    toast('已开立复核工单'); render(); loadNotifs();
  });
  populateOrderSelect('#fOrder');
}

async function populateOrderSelect(selector) {
  const { floors } = await api('GET', '/api/floorboard');
  const opts = floors.flatMap((f) => f.rooms.filter((r) => r.order_id).map((r) =>
    `<option value="${r.order_id}">${f.floor} 层 ${esc(r.label)} · ${STATUS_LABEL[r.order_status]} · ${esc(r.order_id.slice(-8))}</option>`));
  const sel = $(selector);
  if (sel) sel.innerHTML = opts.join('');
}

async function loadReasonOptions() {
  const { reasons } = await api('GET', '/api/comp-reasons');
  const sel = $('#fReason');
  if (sel) sel.innerHTML = '<option value="">请选择原因</option>' + reasons.map((r) => `<option value="${r.code}">${esc(r.label)}</option>`).join('');
}

/* ---------------- 效期报损 ---------------- */
async function renderExpiry(c) {
  const [{ batches }, { writeOffs }] = await Promise.all([api('GET', '/api/batches/near-expiry'), api('GET', '/api/write-offs')]);
  c.innerHTML = '';
  const p1 = el('div', 'panel');
  p1.innerHTML = `<h3>临期 / 过期批次（≤ 3 天）</h3>
    <table class="data"><thead><tr><th>商品</th><th>批次</th><th>效期</th><th>位置数量</th><th></th></tr></thead>
    <tbody>${batches.map((b) => `
      <tr><td>${esc(b.product_name)}</td><td class="mono">${esc(b.lot_no)}</td>
      <td>${fmtDate(b.expires_at)} <span class="tag ${b.days_left <= 0 ? 'expired' : 'near'}">${b.days_left <= 0 ? '已过期' : '剩 ' + b.days_left + ' 天'}</span></td>
      <td class="muted">车 ${b.cart_qty} / 房 ${b.room_qty} · ${b.status === 'active' ? '可上架' : b.status}</td>
      <td class="right">
        ${b.cart_qty > 0 && b.status === 'active' ? `<button class="btn sm danger" data-wo="cart" data-pid="${b.product_id}" data-bid="${b.id}" data-qty="${b.cart_qty}">车存报损</button>` : ''}
      </td></tr>`).join('') || '<tr><td colspan="5" class="empty">没有临期批次</td></tr>'}</tbody></table>
    <p class="hint" style="margin-top:8px">已过期或包装破损的商品只能转报损，系统不再将其列入上架批次。</p>`;
  p1.querySelectorAll('button[data-wo]').forEach((b) => b.onclick = () =>
    writeOffModal({ source: b.dataset.wo, productId: b.dataset.pid, batchId: b.dataset.bid, maxQty: Number(b.dataset.qty) }));
  c.appendChild(p1);

  const p2 = el('div', 'panel');
  p2.innerHTML = `<h3>报损记录</h3>
    <table class="data"><thead><tr><th>时间</th><th>商品/批次</th><th>原因</th><th>来源</th><th>照片</th><th class="right">数量</th></tr></thead>
    <tbody>${writeOffs.map((w) => `
      <tr><td class="muted">${fmtTime(w.at)}</td><td>${esc(w.product_name)} <span class="mono">${esc(w.lot_no || '')}</span></td>
      <td><span class="tag ${w.reason}">${w.reason === 'expired' ? '已过期' : '包装破损'}</span></td>
      <td>${w.source === 'cart' ? '补货车' : esc(w.room_label || '客房')}</td>
      <td>${w.photo ? `<a href="/photos/${encodeURIComponent(w.photo)}" target="_blank">查看</a>` : '<span class="muted">—</span>'}</td>
      <td class="money">${w.qty}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无报损</td></tr>'}</tbody></table>`;
  c.appendChild(p2);
}

function writeOffModal({ source, productId, batchId, maxQty }) {
  openModal('转报损（不可再次上架）', `
    <div class="field"><label>原因</label><select id="fReason"><option value="expired">已过期</option><option value="damaged">包装破损</option></select></div>
    <div class="field"><label>数量</label><input id="fQty" type="number" min="1" max="${maxQty}" value="${maxQty}"></div>
    <div class="field"><label>包装照片（可选）</label><input id="fPhoto" type="file" accept="image/*"></div>
    <div class="field"><label>备注</label><textarea id="fNote"></textarea></div>
  `, async () => {
    let photo;
    const file = $('#fPhoto').files[0];
    if (file) {
      const up = await api('POST', '/api/photos', file, {
        raw: true,
        headers: { 'X-Filename': file.name, 'X-Ref-Type': 'writeoff', 'Content-Type': file.type },
      });
      photo = up.filename;
    }
    await api('POST', '/api/write-offs', {
      source, productId, batchId, qty: Number($('#fQty').value),
      reason: $('#fReason').value, photo, note: $('#fNote').value,
    });
    toast('已转报损'); render();
  });
}

/* ---------------- 退房结算 ---------------- */
async function renderCheckout(c) {
  const { floors } = await api('GET', '/api/floorboard');
  const rooms = floors.flatMap((f) => f.rooms.filter((r) => r.order_id).map((r) => ({ ...r, floor: f.floor })));
  const frozen = rooms.filter((r) => r.order_status === 'frozen');
  c.innerHTML = '';

  const bar = el('div', 'panel');
  bar.innerHTML = `<h3>待退房订单</h3>
    <table class="data"><thead><tr><th>客房</th><th>客人</th><th>订单状态</th><th>待确认</th><th class="right">操作</th></tr></thead>
    <tbody>${frozen.length ? frozen.map((r) => `
      <tr><td>${r.floor} 层 ${esc(r.label)}</td><td>${esc(r.guest_anon)}</td>
      <td><span class="status-pill st-pending_checkout">已冻结（盘点锁定 v—）</span></td>
      <td>${r.pending_count ? `<span class="tag near">${r.pending_count} 笔</span>` : '<span class="tag ok">已清</span>'}</td>
      <td class="right"><button class="btn sm gold" data-order="${r.order_id}">前台确认账单并结算</button></td></tr>`)
      .join('') : '<tr><td colspan="5" class="empty">没有已冻结订单（请先在楼层房态执行退房冻结）</td></tr>'}</tbody></table>`;
  bar.querySelectorAll('button').forEach((b) => b.onclick = () => settle(b.dataset.order));
  c.appendChild(bar);

  if (state.selectedRoom) {
    const room = rooms.find((r) => r.id === state.selectedRoom);
    if (room) {
      const data = await api('GET', `/api/orders/${room.order_id}/bill`);
      c.appendChild(checkoutBillView(data));
    }
  } else {
    // 默认展开第一间冻结房账单
    if (frozen[0]) {
      const data = await api('GET', `/api/orders/${frozen[0].order_id}/bill`);
      c.appendChild(checkoutBillView(data));
    }
  }
}

function checkoutBillView(data) {
  const panel = el('div', 'panel');
  panel.innerHTML = `
    <div class="count-head">
      <h3 style="margin:0">${data.room.floor} 层 ${esc(data.room.label)} · 离店账单</h3>
      <span class="status-pill st-${data.room.status}">${STATUS_LABEL[data.order.status]}</span>
      <span class="spacer"></span>
      <button class="btn sm ghost" onclick="window.print()">🖨 打印账单</button>
    </div>
    <div class="total-bar">
      <span class="label">应付合计（CNY）</span><span class="amount">¥${data.bill.totalYuan}</span>
      <span class="spacer"></span>
      <span class="label">结算状态：${data.order.status === 'closed' ? `已结算 ${esc(data.order.settlement_ref || '')}` : '冻结待确认'}</span>
    </div>
    <table class="data"><thead><tr><th>时间</th><th>项目</th><th>类型</th><th>经办</th><th class="right">金额</th></tr></thead>
      <tbody>${data.bill.charges.map((ch) => `
        <tr><td class="muted">${fmtTime(ch.created_at)}</td>
        <td>${chargeTitle(ch)}</td>
        <td><span class="tag ${ch.kind === 'reversal' ? 'expired' : ch.kind === 'comp' ? 'near' : 'ok'}">${({ minibar: '消费', comp: '免单', reversal: '冲销', adjustment: '调整' })[ch.kind]}</span></td>
        <td>${esc(ch.created_by || '系统')}</td>
        <td class="money ${moneyClass(ch.amount_cents)}">${ch.amount_cents === 0 ? '¥0.00' : (ch.amount_cents < 0 ? '-' : '') + '¥' + yuan(Math.abs(ch.amount_cents))}</td></tr>`)
        .join('') || '<tr><td colspan="5" class="empty">账单为空</td></tr>'}</tbody>
      <tfoot><tr><td colspan="4" class="right"><strong>合计</strong></td>
        <td class="money" style="font-weight:700;color:var(--navy)">¥${data.bill.totalYuan}</td></tr></tfoot>
    </table>
    <p class="hint" style="margin-top:10px">退房冻结已锁定最后一次盘点；账单确认前争议须处理完毕。模拟结算网关返回参考号后订单关闭。</p>`;
  return panel;
}

async function settle(orderId) {
  try {
    const r = await api('POST', `/api/orders/${orderId}/settle`, { autoConfirm: true });
    toast(`结算成功：${r.settlement.reference}，订单已关闭`);
    render(); loadNotifs();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 通用模态 ---------------- */
function openModal(title, bodyHtml, onSubmit) {
  const root = $('#modalRoot');
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <div class="modal-head"><span>${esc(title)}</span><button class="btn sm ghost" id="mClose">×</button></div>
        <div class="modal-body">${bodyHtml}</div>
        <div class="modal-foot"><button class="btn ghost" id="mCancel">取消</button><button class="btn gold" id="mOk">确认</button></div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ''; };
  $('#mClose').onclick = close; $('#mCancel').onclick = close;
  root.querySelector('.modal-mask').addEventListener('click', (e) => { if (e.target.classList.contains('modal-mask')) close(); });
  $('#mOk').onclick = async () => {
    try { await onSubmit(); close(); } catch (e) { toast(e.message, true); }
  };
}

init();
