"""HTTP 服务器：页面路由 + 渲染。仅用标准库，演示用途。

会话：cookie mb_uid 存员工 id（演示环境不做签名）；所有写操作的权限
仍在服务层按 staff 行重新校验（角色 + 楼层）。
"""
import html
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

from .. import config, services
from ..adapters import settlement_adapter
from ..clock import clock, days_between
from ..db import connect
from ..errors import MinibarError, PermissionDenied, IdempotentReplay, VersionConflict, OrderFrozen
from ..photos import save_photo
from .forms import parse_form


def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def money(v):
    if v is None:
        return ""
    return f"{v:,.2f}"


STATUS_LABEL = {
    "vacant": "空房", "occupied": "在住", "checkout_pending": "待退",
    "open": "在住", "frozen": "退房冻结", "closed": "已关闭",
}
CONS_LABEL = {
    "pending": "待确认", "confirmed": "已确认", "disputed": "争议中",
    "waived": "已免单", "reversed": "已冲销",
}
KIND_LABEL = {
    "guest_denial": "客人否认", "wrong_room": "跨房放错",
    "batch_expiring": "批次临期", "cart_shortage": "补货车短缺",
    "offline_resend": "离线重传", "post_checkout": "退房后补录",
}
CASE_STATUS_LABEL = {"open": "待处理", "in_review": "复核中", "resolved": "已结案", "rejected": "已驳回"}


# --------------------------------------------------------------------- #
# 页面片段
# --------------------------------------------------------------------- #
def page(title, body, staff, active="", flash=None):
    nav = [
        ("/", "楼层房态", "rooms"),
        ("/cart", "补货车", "cart"),
        ("/reviews", "消费复核", "reviews"),
        ("/expiry", "效期报损", "expiry"),
    ]
    links = "".join(
        f'<a href="{u}" class="{"active" if active == k else ""}">{t}</a>'
        for u, t, k in nav)
    who = ""
    if staff:
        role = {"manager": "经理", "front_desk": "前台", "attendant": "服务员"}[staff["role"]]
        scope = "全楼层" if staff["floor_scope"] == "*" else f"{staff['floor_scope']}F 权限"
        who = (f'<span>{esc(staff["name"])} · {role} · {scope}</span>'
               '<form method="post" action="/logout"><button class="btn-link">切换</button></form>')
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{esc(msg)}</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 迷你吧结算台</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
<header class="topbar no-print">
  <span class="brand">迷你吧补货与离店结算<small>匿名演示</small></span>
  <nav>{links}</nav>
  <span class="who">{who}</span>
</header>
<main class="wrap">
<div class="print-head"><h1>{esc(title)}</h1><p class="sub">打印时间 {esc(clock.iso())}</p></div>
{flash_html}
{body}
</main></body></html>"""


def login_page():
    conn = connect()
    staff_rows = conn.execute("SELECT * FROM staff ORDER BY id").fetchall()
    conn.close()
    opts = "".join(
        f'<option value="{s["id"]}">{esc(s["code"])} · {esc(s["name"])}（'
        f'{{"manager":"经理","front_desk":"前台","attendant":"服务员"}}[{s["role"]}]'
        f'，{"全楼层" if s["floor_scope"]=="*" else s["floor_scope"]+"F"}）</option>'
        for s in staff_rows)
    body = f"""
    <div class="login-box">
      <h1>前台登录</h1>
      <p class="sub">选择演示员工进入（客人与账单均为匿名数据）</p>
      <form method="post" action="/login">
        <label>员工</label>
        <select name="uid">{opts}</select>
        <div style="margin-top:16px"><button class="btn gold">进入系统</button></div>
      </form>
    </div>"""
    return page("登录", body, None)


def rooms_page(staff, floor=None, flash=None):
    conn = connect()
    rows = services.floor_room_list(conn, staff, floor)
    floors = sorted({r["floor"] for r in rows})
    rooms_by_floor = {f: [r for r in rows if r["floor"] == f] for f in floors}
    conn.close()

    floor_tabs = '<a class="btn sm ghost" href="/">全部</a> ' + " ".join(
        f'<a class="btn sm {"gold" if str(f)==str(floor) else "ghost"}" href="/?floor={f}">{f}F</a>'
        for f in floors)

    blocks = []
    for f in floors:
        trs = []
        for r in rooms_by_floor[f]:
            if r["order_id"]:
                st = r["order_state"]
                badge = f'<span class="badge {st}">{STATUS_LABEL.get(st, st)}</span>'
                guest = esc(r["guest_alias"])
                ver = f'v{r["snap_version"]}'
                pend = (f'<span class="badge pending">待确认 {r["pending_consumptions"]}</span>'
                        if r["pending_consumptions"] else "")
                cases = (f'<span class="pill-count">{r["open_cases"]}</span>'
                         if r["open_cases"] else "")
                total = money(r["bill_total"])
                if st == "frozen":
                    action = f'<a href="/checkout?id={r["order_id"]}">退房结算</a>'
                else:
                    action = f'<a href="/room?id={r["order_id"]}">盘点 / 账单</a>'
                trs.append(f"""<tr>
                  <td class="room-no">{esc(r['room_no'])}</td>
                  <td>{badge}</td><td>{guest} <span class="muted">{esc(r['order_no'])}</span></td>
                  <td class="nowrap">{ver} {pend}{cases}</td>
                  <td class="num">¥ {total}</td><td>{action}</td></tr>""")
            else:
                trs.append(f"""<tr>
                  <td class="room-no">{esc(r['room_no'])}</td>
                  <td><span class="badge vacant">空房</span></td>
                  <td class="muted">—</td><td>—</td><td class="num muted">—</td><td>—</td></tr>""")
        blocks.append(f"""
        <section class="floor-block">
          <div class="floor-title">{f} F</div>
          <table class="grid">
            <colgroup><col style="width:90px"><col style="width:120px"><col>
                       <col style="width:210px"><col style="width:110px"><col style="width:130px"></colgroup>
            <thead><tr><th>房号</th><th>状态</th><th>客人 / 订单</th>
            <th>库存版本 / 待办</th><th class="num">当前账单</th><th>操作</th></tr></thead>
            <tbody>{"".join(trs)}</tbody>
          </table>
        </section>""")
    body = f"""
    <div class="page-head no-print"><h1>楼层房态</h1>
      <span class="sub">数据时间 {esc(clock.iso())}</span></div>
    <div class="toolbar no-print">{floor_tabs}</div>
    {"".join(blocks)}"""
    return page("楼层房态", body, staff, "rooms", flash)


def room_page(staff, order_id, flash=None):
    conn = connect()
    order = services._load_order(conn, order_id)
    room = conn.execute("SELECT * FROM room WHERE id=?", (order["room_id"],)).fetchone()
    services.can_access_floor(staff, room["floor"])
    snap, items = services.latest_snapshot_items(conn, order_id)
    caps = {r["product_id"]: r["cap_qty"] for r in conn.execute(
        "SELECT * FROM display_cap WHERE room_id=?", (room["id"],))}
    # 补货车可补量（未过期、未破损）
    today = clock.today()
    cart_qty = {}
    for r in conn.execute(
            "SELECT product_id, SUM(qty) q FROM batch WHERE loc='cart' AND cart_id=1 "
            "AND damaged=0 AND qty>0 AND expiry_date>=? GROUP BY product_id",
            (today.isoformat(),)):
        cart_qty[r["product_id"]] = r["q"]

    rows = []
    for it in items:
        pid = it["product_id"]
        cap = caps.get(pid, it["qty"])
        fill = max(0, cap - it["qty"])
        avail = cart_qty.get(pid, 0)
        fill_disp = fill
        cls = "zero" if fill == 0 else ("short" if fill > avail else "")
        short_note = (f' <span class="muted">（车存 {avail}，短缺 {fill-avail}）</span>'
                      if fill > avail else f' <span class="muted">车存 {avail}</span>')
        img = f'/static/img/{esc(it["image"])}' if it["image"] else ""
        disabled = "disabled" if order["state"] != "open" else ""
        rows.append(f"""<tr>
          <td><div class="prod-cell">
            <img src="{img}" alt="">
            <div><div class="pname">{esc(it['name'])}</div>
            <div class="pmeta">{esc(it['sku'])} · 上限 {cap} · 单价 ¥{money(it['unit_price'])}</div></div>
          </div></td>
          <td class="center"><span class="fill-cell">{it['qty']}</span></td>
          <td class="center">
            <input class="qty-input" type="number" min="0" max="{cap}" name="q_{pid}"
                   value="{it['qty']}" {disabled}>
            <div class="sub">{fill_disp} 件待补{short_note if fill else ''}</div>
          </td></tr>""")

    # 消费清单
    cons_rows = []
    for c in conn.execute(
            "SELECT c.*, p.name AS pname, p.image FROM consumption c "
            "JOIN product p ON p.id=c.product_id WHERE c.order_id=? ORDER BY c.id",
            (order_id,)):
        amt = c["qty"] * c["unit_price"]
        acts = []
        if order["state"] == "open":
            if c["status"] == "pending":
                acts.append(f'<button class="btn sm" formaction="/consumption/confirm" name="cid" value="{c["id"]}">确认</button>')
                acts.append(f'<button class="btn sm ghost" formaction="/consumption/deny" name="cid" value="{c["id"]}">客人否认</button>')
            if c["status"] in ("pending", "disputed") and staff["role"] in ("attendant", "manager", "front_desk"):
                acts.append(f'<button class="btn sm ghost" formaction="/consumption/wrongroom" name="cid" value="{c["id"]}">跨房放错</button>')
        if staff["role"] == "manager" and c["status"] in ("pending", "disputed"):
            acts.append(f'<button class="btn sm danger" formaction="/consumption/waive" name="cid" value="{c["id"]}">经理免单</button>')
        cons_rows.append(f"""<tr>
          <td>{esc(c['pname'])}</td><td class="num">{c['qty']}</td>
          <td class="num">¥{money(amt)}</td>
          <td><span class="badge {c['status']}">{CONS_LABEL[c['status']]}</span></td>
          <td class="nowrap">{" ".join(acts)}</td></tr>""")

    timeline = _timeline_html(conn, order_id)

    frozen_banner = ""
    if order["state"] == "frozen":
        frozen_banner = ('<div class="frozen-note">退房冻结中：最后一次盘点（v'
                         f'{order["snap_version"]}）已锁定，不能再盘点；请前往退房结算。</div>')
    elif order["state"] == "closed":
        frozen_banner = '<div class="frozen-note">订单已关闭。</div>'

    freeze_btn = ""
    if order["state"] == "open" and staff["role"] in ("front_desk", "manager"):
        freeze_btn = (f'<button class="btn gold" formaction="/order/freeze" name="order_id" '
                      f'value="{order_id}">办理退房冻结</button>')
    checkout_link = (f'<a class="btn ghost" href="/checkout?id={order_id}">前往退房结算</a>'
                     if order["state"] == "frozen" else "")

    body = f"""
    <div class="page-head no-print">
      <h1>房 {esc(room['room_no'])} · {esc(order['guest_alias'])}
        <span class="badge {order['state']}">{STATUS_LABEL[order['state']]}</span></h1>
      <span class="sub">{esc(order['order_no'])} · 入住 {esc(order['check_in_at'])}</span>
    </div>
    {frozen_banner}
    <h2>房内盘点 <span class="sub">基于快照 v{order['snap_version']}（{esc(snap['kind'])}，{esc(snap['created_at'])}）</span></h2>
    <form method="post" action="/stocktake">
      <input type="hidden" name="order_id" value="{order_id}">
      <input type="hidden" name="idem_key" value="take-{order_id}-{clock.now().strftime('%Y%m%d%H%M%S')}">
      <table class="grid stocktake">
        <colgroup><col class="c-img"><col class="c-cur"><col class="c-fill"></colgroup>
        <thead><tr><th>商品图片</th><th class="center">当前数（快照）</th><th class="center">盘点数 / 补货数</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      <div class="toolbar" style="margin-top:12px">
        <button class="btn" {'disabled' if order['state']!='open' else ''}>提交盘点</button>
        <span class="sub">数量较快照减少将形成待确认消费，补货按批次 FEFO 从补货车转入</span>
        {freeze_btn}{checkout_link}
      </div>
    </form>

    <h2>消费清单</h2>
    <form method="post" action="/consumption/waive">
    <table class="grid">
      <thead><tr><th>商品</th><th class="num">数量</th><th class="num">金额</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{"".join(cons_rows) or '<tr><td colspan="5" class="muted center">暂无消费记录</td></tr>'}</tbody>
    </table></form>

    <h2>账单时间线</h2>
    {timeline}
    """
    conn.close()
    return page(f"房 {room['room_no']} 盘点", body, staff, "rooms", flash)


def _timeline_html(conn, order_id):
    events = services.order_timeline(conn, order_id)
    total = services.bill_total(conn, order_id)
    lis = []
    for e in events:
        cls = ""
        if e["kind"].startswith("bill:waiver") or e["kind"].startswith("bill:reversal") or e["kind"] == "consumption:waived":
            cls = ""
        if e["kind"] == "consumption:disputed":
            cls = "danger"
        if e["kind"] in ("bill:charge", "settled", "consumption:confirmed"):
            cls = "ok"
        if e["amount"] is None:
            amt = '<span class="amt-zero">—</span>'
        else:
            cls_amt = "amt-neg" if e["amount"] <= 0 else "amt-pos"
            sign = "" if e["amount"] <= 0 else "+"
            amt = f'<span class="t-amt {cls_amt}">{sign}¥{money(e["amount"])}</span>'
        lis.append(f'<li class="{cls}"><span class="t-time">{esc(e["at"][5:16])}</span>'
                   f'{amt}<span class="t-title">{esc(e["title"])}</span></li>')
    return f"""
    <ul class="timeline">{"".join(lis) or '<li class="muted">暂无账单事件</li>'}</ul>
    <div class="bill-total"><span>账单合计</span><b>¥ {money(total)}</b></div>"""


def cart_page(staff, flash=None):
    conn = connect()
    view = services.cart_list_view(conn)
    photos = conn.execute(
        "SELECT p.*, s.name AS staff_name FROM photo p LEFT JOIN staff s ON s.id=p.uploaded_by "
        "ORDER BY p.id DESC LIMIT 8").fetchall()
    conn.close()
    trs = []
    for g in view["groups"]:
        batch_bits = []
        for b in g["batches"]:
            tag = ""
            if b["expired"]:
                tag = '<span class="badge expired">已过期</span>'
            elif b["expiring"]:
                tag = f'<span class="badge expiring">{b["days_to_expiry"]}天到期</span>'
            batch_bits.append(
                f'<span class="nowrap">{esc(b["batch_no"])} ×{b["qty"]} '
                f'<span class="muted">{b["expiry_date"]}</span> {tag}</span>')
        trs.append(f"""<tr>
          <td><b>{esc(g['name'])}</b> <span class="muted">{esc(g['sku'])}</span></td>
          <td class="num">¥{money(g['unit_price'])}</td>
          <td class="num"><b>{g['total_qty']}</b></td>
          <td>{"　｜　".join(batch_bits)}</td></tr>""")
    dmg = []
    for b in view["damaged"]:
        dmg.append(f"""<tr>
          <td>{esc(b['product_name'])}</td><td>{esc(b['batch_no'])}</td>
          <td class="num">{b['qty']}</td><td><span class="badge damaged">包装破损</span> {esc(b['expiry_date'])}</td>
          <td>
            <form class="inline" method="post" action="/batch/writeoff">
              <input type="hidden" name="batch_id" value="{b['id']}">
              <input type="hidden" name="reason" value="damaged">
              <button class="btn sm danger">报损（破损）</button>
            </form>
          </td></tr>""")
    photo_rows = "".join(
        f'<tr><td><img class="photo-thumb" src="/photos/{esc(p["filename"])}"></td>'
        f'<td>{esc(p["filename"])}</td><td>{esc(p["staff_name"] or "—")}</td>'
        f'<td>{esc(p["note"])}</td></tr>' for p in photos)
    can_photo = staff["role"] in ("attendant", "manager")
    body = f"""
    <div class="page-head no-print"><h1>补货车 #1 清单</h1>
      <span class="sub">补货按批次效期 FEFO 先进先出</span></div>
    <table class="grid">
      <thead><tr><th>商品</th><th class="num">单价</th><th class="num">可用数量</th><th>批次明细（效期）</th></tr></thead>
      <tbody>{"".join(trs) or '<tr><td colspan="4" class="muted center">空车</td></tr>'}</tbody>
    </table>
    <h2>包装破损 — 只能报损，不能再次上架</h2>
    <table class="grid">
      <thead><tr><th>商品</th><th>批次</th><th class="num">数量</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{"".join(dmg) or '<tr><td colspan="5" class="muted center">暂无破损登记</td></tr>'}</tbody>
    </table>
    {'<h2>上传包装照片</h2><form class="toolbar" method="post" action="/batch/damaged" enctype="multipart/form-data">'
     '<input type="text" name="batch_no" placeholder="批次号 如 CC-2609D" required style="width:180px">'
     '<input type="file" name="photo" accept="image/*" required>'
     '<input type="text" name="note" placeholder="破损说明" style="width:220px">'
     '<button class="btn">上传并标记破损</button></form>' if can_photo else ''}
    <h2>最近照片</h2>
    <table class="grid"><thead><tr><th>照片</th><th>文件</th><th>上传人</th><th>说明</th></tr></thead>
    <tbody>{photo_rows or '<tr><td colspan="4" class="muted center">暂无照片</td></tr>'}</tbody></table>
    """
    return page("补货车清单", body, staff, "cart", flash)


def reviews_page(staff, flash=None, kind=None):
    conn = connect()
    cases = services.list_cases(conn)
    conn.close()
    tabs = ["", *config.REVIEW_TYPES]
    tab_html = " ".join(
        f'<a class="btn sm {"gold" if k==kind else "ghost"}" href="/reviews?kind={quote(k)}">'
        f'{"全部" if k=="" else KIND_LABEL[k]}</a>' for k in tabs)
    trs = []
    for c in cases:
        if kind and c["kind"] != kind:
            continue
        acts = []
        cid = c["id"]
        if c["status"] in ("open", "in_review"):
            if c["kind"] == "guest_denial" and c["consumption_id"]:
                acts.append(f'<a class="btn sm" href="/review-deny?id={cid}">处理</a>')
            elif c["kind"] == "wrong_room":
                if staff["role"] == "manager":
                    acts.append(f'<a class="btn sm gold" href="/review-wrongroom?id={cid}">经理裁定</a>')
            elif c["kind"] == "post_checkout":
                if staff["role"] == "manager":
                    acts.append(
                        f'<form class="inline" method="post" action="/review/post-checkout">'
                        f'<button class="btn sm gold" name="case_id" value="{cid}">核准并入账</button></form>')
                else:
                    acts.append('<span class="muted">待经理核准</span>')
            elif c["kind"] == "batch_expiring" and c["batch_id"]:
                acts.append(f'<a class="btn sm" href="/expiry?case={cid}">去效期报损</a>')
            else:
                acts.append(f'<a class="btn sm ghost" href="/reviews">查看</a>')
            if staff["role"] == "manager":
                acts.append(
                    f'<form class="inline" method="post" action="/review/resolve">'
                    f'<input type="text" name="resolution" placeholder="结案说明" style="width:150px">'
                    f'<button class="btn sm" name="case_id" value="{cid}">结案</button></form>')
        trs.append(f"""<tr>
          <td class="nowrap">{esc(c['case_no'])}</td>
          <td><span class="kind-tag">{KIND_LABEL[c['kind']]}</span></td>
          <td>{esc(c['room_no'] or '—')}</td>
          <td>{esc(c['product_name'] or '—')}{f' ×{c["qty"]}' if c['qty'] else ''}</td>
          <td>{esc(c['summary'])}</td>
          <td><span class="badge {c['status']}">{CASE_STATUS_LABEL[c['status']]}</span></td>
          <td class="nowrap">{" ".join(acts)}</td></tr>""")
    body = f"""
    <div class="page-head no-print"><h1>消费复核</h1>
      <span class="sub">六类独立复核流程；免单/冲销/裁定需经理权限并留存原因</span></div>
    <div class="toolbar no-print">{tab_html}</div>
    <table class="grid">
      <thead><tr><th>复核单号</th><th>类型</th><th>客房</th><th>商品</th>
      <th>事由</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{"".join(trs) or '<tr><td colspan="7" class="muted center">没有匹配的复核单</td></tr>'}</tbody>
    </table>"""
    return page("消费复核", body, staff, "reviews", flash)


def review_deny_page(staff, case_id, flash=None):
    conn = connect()
    case = conn.execute("SELECT * FROM review_case WHERE id=?", (case_id,)).fetchone()
    c = conn.execute(
        "SELECT c.*, p.name AS pname FROM consumption c JOIN product p ON p.id=c.product_id "
        "WHERE c.id=?", (case["consumption_id"],)).fetchone()
    order = services._load_order(conn, c["order_id"])
    room = conn.execute("SELECT room_no FROM room WHERE id=?", (order["room_id"],)).fetchone()
    conn.close()
    amount = c["qty"] * c["unit_price"]
    reason_opts = "".join(
        f'<option value="{r}">{lbl}</option>' for r, lbl in (
            ("guest_denial", "客人否认"), ("wrong_room", "跨房放错"),
            ("product_quality", "商品质量"), ("service_recovery", "服务补偿")))
    body = f"""
    <div class="page-head no-print"><h1>复核：客人否认消费 {esc(case['case_no'])}</h1></div>
    <table class="grid" style="max-width:640px">
      <tr><th>客房</th><td>{esc(room['room_no'])} · {esc(order['guest_alias'])}</td></tr>
      <tr><th>商品</th><td>{esc(c['pname'])} ×{c['qty']}</td></tr>
      <tr><th>金额</th><td class="num">¥ {money(amount)}</td></tr>
      <tr><th>争议说明</th><td>{esc(case['summary'])}</td></tr>
    </table>
    <h2>经理免单（需原因，写入账单时间线）</h2>
    <form class="toolbar" method="post" action="/consumption/waive">
      <input type="hidden" name="cid" value="{c['id']}">
      <select name="reason">{reason_opts}</select>
      <input type="text" name="note" placeholder="核实说明（必填原因留存）" style="width:280px" required>
      <button class="btn danger">确认免单</button>
      <a class="btn ghost" href="/room?id={order['id']}">返回客房</a>
    </form>
    <p class="sub">若客人实际消费，可在客房页将该条消费改回“确认”（由前台操作）。</p>"""
    return page("客人否认复核", body, staff, "reviews", flash)


def review_wrongroom_page(staff, case_id, flash=None):
    conn = connect()
    case = conn.execute("SELECT * FROM review_case WHERE id=?", (case_id,)).fetchone()
    c = conn.execute(
        "SELECT c.*, p.name AS pname FROM consumption c JOIN product p ON p.id=c.product_id "
        "WHERE c.id=?", (case["consumption_id"],)).fetchone()
    src_order = services._load_order(conn, c["order_id"])
    target = conn.execute("SELECT * FROM room WHERE id=?", (case["room_id"],)).fetchone()
    target_order = conn.execute(
        "SELECT * FROM stay_order WHERE room_id=? AND state!='closed' ORDER BY id DESC LIMIT 1",
        (target["id"],)).fetchone()
    conn.close()
    body = f"""
    <div class="page-head no-print"><h1>复核：跨房放错 {esc(case['case_no'])}</h1></div>
    <table class="grid" style="max-width:640px">
      <tr><th>消费来源</th><td>订单 {esc(src_order['order_no'])} · {esc(c['pname'])} ×{c['qty']}</td></tr>
      <tr><th>实际归属</th><td>房 {esc(target['room_no'])} · 订单 {esc(target_order['order_no'] if target_order else '无在住单')}</td></tr>
      <tr><th>说明</th><td>{esc(case['summary'])}</td></tr>
    </table>
    <form class="toolbar" method="post" action="/review/wrongroom">
      <input type="hidden" name="case_id" value="{case_id}">
      <input type="text" name="note" placeholder="经理裁定说明（留存）" style="width:300px" required>
      <button class="btn gold">经理裁定：冲销原房并转入目标房</button>
    </form>"""
    return page("跨房放错复核", body, staff, "reviews", flash)


def expiry_page(staff, flash=None):
    conn = connect()
    batches = services.expiring_batches(conn)
    writeoffs = conn.execute(
        "SELECT b.*, p.name AS pname FROM batch b JOIN product p ON p.id=b.product_id "
        "WHERE b.loc='writeoff' ORDER BY b.written_off_at DESC LIMIT 20").fetchall()
    conn.close()
    trs = []
    for b in batches:
        loc = f"补货车 #{b['cart_id']}" if b["loc"] == "cart" else f"房 {b['room_no']}"
        if b["tag"] == "expired":
            tag = f'<span class="badge expired">已过期 {-b["days_to_expiry"]} 天</span>'
            btn = (f'<form class="inline" method="post" action="/batch/writeoff">'
                   f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                   f'<input type="hidden" name="reason" value="expired">'
                   f'<button class="btn sm danger">过期报损</button></form>')
        else:
            tag = f'<span class="badge expiring">{b["days_to_expiry"]} 天到期</span>'
            btn = (f'<form class="inline" method="post" action="/batch/writeoff">'
                   f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                   f'<input type="hidden" name="reason" value="expired">'
                   f'<button class="btn sm ghost" disabled title="未过期不能按过期报损">不可报损</button></form>'
                   f'<span class="sub">请 FEFO 优先消耗</span>')
        if b["damaged"]:
            tag += ' <span class="badge damaged">破损</span>'
            btn = (f'<form class="inline" method="post" action="/batch/writeoff">'
                   f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                   f'<input type="hidden" name="reason" value="damaged">'
                   f'<button class="btn sm danger">破损报损</button></form>')
        trs.append(f"""<tr>
          <td>{esc(b['batch_no'])}</td><td>{esc(b['product_name'])}</td>
          <td>{loc}</td><td class="num">{b['qty']}</td>
          <td class="nowrap">{esc(b['expiry_date'])} {tag}</td><td>{btn}</td></tr>""")
    wo_rows = "".join(
        f'<tr><td>{esc(b["batch_no"])}</td><td>{esc(b["pname"])}</td>'
        f'<td class="num">{b["qty"]}</td><td>'
        f'<span class="badge {b["writeoff_reason"]}">{"已过期" if b["writeoff_reason"]=="expired" else "包装破损"}</span></td>'
        f'<td>{esc(b["written_off_at"] or "")}</td></tr>' for b in writeoffs)
    body = f"""
    <div class="page-head no-print"><h1>效期与报损</h1>
      <span class="sub">已过期或包装破损只能转报损，不能再次上架（终态）</span></div>
    <table class="grid">
      <thead><tr><th>批次</th><th>商品</th><th>位置</th><th class="num">数量</th><th>效期状态</th><th>操作</th></tr></thead>
      <tbody>{"".join(trs) or '<tr><td colspan="6" class="muted center">暂无临期/过期批次</td></tr>'}</tbody>
    </table>
    <h2>报损记录</h2>
    <table class="grid">
      <thead><tr><th>批次</th><th>商品</th><th class="num">数量</th><th>原因</th><th>时间</th></tr></thead>
      <tbody>{wo_rows or '<tr><td colspan="5" class="muted center">暂无报损</td></tr>'}</tbody>
    </table>"""
    return page("效期报损", body, staff, "expiry", flash)


def checkout_page(staff, order_id, flash=None):
    conn = connect()
    order = services._load_order(conn, order_id)
    room = conn.execute("SELECT * FROM room WHERE id=?", (order["room_id"],)).fetchone()
    services.can_access_floor(staff, room["floor"])
    pending = conn.execute(
        "SELECT c.*, p.name AS pname FROM consumption c JOIN product p ON p.id=c.product_id "
        "WHERE c.order_id=? AND c.status IN ('pending','disputed') ORDER BY c.id",
        (order_id,)).fetchall()
    open_cases = conn.execute(
        "SELECT * FROM review_case WHERE order_id=? AND status IN ('open','in_review')",
        (order_id,)).fetchall()
    products = conn.execute("SELECT * FROM product ORDER BY category, name").fetchall()
    settlement = conn.execute(
        "SELECT * FROM settlement WHERE order_id=? ORDER BY id DESC LIMIT 1",
        (order_id,)).fetchone()
    timeline = _timeline_html(conn, order_id)
    conn.close()

    if order["state"] != "frozen":
        pass  # 结算表单仅冻结订单显示
    pending_rows = "".join(
        f'<tr><td>{esc(c["pname"])}</td><td class="num">{c["qty"]}</td>'
        f'<td class="num">¥{money(c["qty"]*c["unit_price"])}</td>'
        f'<td><span class="badge {c["status"]}">{CONS_LABEL[c["status"]]}</span></td>'
        f'<td class="nowrap"><form class="inline" method="post" action="/consumption/confirm">'
        f'<button class="btn sm" name="cid" value="{c["id"]}">确认入账</button></form> '
        f'<a class="btn sm ghost" href="/room?id={order_id}">争议处理</a></td></tr>'
        for c in pending)
    case_rows = "".join(
        f'<tr><td>{esc(c["case_no"])}</td><td><span class="kind-tag">{KIND_LABEL[c["kind"]]}</span></td>'
        f'<td>{esc(c["summary"])}</td>'
        f'<td>{"待经理核准" if c["kind"]=="post_checkout" else "前往复核处理"}</td></tr>'
        for c in open_cases)
    prod_opts = "".join(f'<option value="{p["id"]}">{esc(p["name"])} ¥{money(p["unit_price"])}</option>'
                        for p in products)
    blockers = ""
    can_settle = not pending and not open_cases and order["state"] == "frozen"
    if order["state"] == "frozen" and (pending or open_cases):
        blockers = ('<div class="frozen-note">尚有未决消费或复核单，前台确认账单前必须全部处理完毕。</div>')
    if order["state"] == "closed":
        blockers = '<div class="flash ok">订单已关闭并完成结算，本页为账单存档（可打印）。</div>'
    settle_form = ""
    if order["state"] == "frozen":
        settle_form = f"""
        <form class="toolbar no-print" method="post" action="/order/settle">
          <input type="hidden" name="order_id" value="{order_id}">
          <button class="btn gold" {'disabled' if not can_settle else ''}>前台确认账单并结算</button>
          <span class="sub">模拟结算通道 MOCK-PSC 返回结果后关闭订单、释放房态</span>
        </form>"""
    st_box = ""
    if settlement:
        st_box = (f'<div class="flash ok">结算完成：{esc(settlement["settlement_no"])} · '
                  f'¥{money(settlement["amount"])} · 通道 {esc(settlement["channel"])} · '
                  f'{esc(settlement["raw"])}</div>')
    body = f"""
    <div class="page-head">
      <h1>退房结算 · 房 {esc(room['room_no'])} · {esc(order['guest_alias'])}</h1>
      <span class="sub">{esc(order['order_no'])} · 冻结于 {esc(order['frozen_at'] or '')}</span>
    </div>
    {st_box}{blockers}
    <h2>未决消费</h2>
    <table class="grid"><thead><tr><th>商品</th><th class="num">数量</th><th class="num">金额</th>
      <th>状态</th><th>操作</th></tr></thead>
      <tbody>{pending_rows or '<tr><td colspan="5" class="muted center">无未决消费</td></tr>'}</tbody></table>
    <h2>未结复核</h2>
    <table class="grid"><thead><tr><th>单号</th><th>类型</th><th>事由</th><th>处理</th></tr></thead>
      <tbody>{case_rows or '<tr><td colspan="4" class="muted center">无未结复核</td></tr>'}</tbody></table>
    <h2>退房后补录（冻结期发现，需经理核准）</h2>
    <form class="toolbar no-print" method="post" action="/order/post-checkout">
      <input type="hidden" name="order_id" value="{order_id}">
      <select name="product_id">{prod_opts}</select>
      <input class="qty-input" type="number" name="qty" value="1" min="1">
      <input type="text" name="note" placeholder="补录说明" style="width:260px" required>
      <button class="btn ghost">提交补录复核</button>
    </form>
    {settle_form}
    <h2>账单时间线</h2>{timeline}
    <div class="no-print toolbar"><button class="btn ghost" onclick="window.print()">打印账单</button></div>
    """
    return page(f"退房结算 {room['room_no']}", body, staff, "rooms", flash)


# --------------------------------------------------------------------- #
# HTTP Handler
# --------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "MiniBar/1.0"

    # -- 基础设施 --
    def log_message(self, fmt, *args):
        if os.environ.get("MINIBAR_QUIET"):
            return
        super().log_message(fmt, *args)

    def _staff(self):
        cookie = self.headers.get("Cookie", "")
        uid = None
        for part in cookie.split(";"):
            if part.strip().startswith("mb_uid="):
                uid = part.split("=", 1)[1].strip()
        if not uid:
            return None
        conn = connect()
        row = conn.execute("SELECT * FROM staff WHERE id=?", (uid,)).fetchone()
        conn.close()
        return row

    def _render(self, html_text, code=200):
        data = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path, ok=None, err=None, info=None):
        qs = []
        if ok: qs.append("ok=" + quote(ok))
        if err: qs.append("err=" + quote(err))
        if info: qs.append("info=" + quote(info))
        loc = path + ("?" + "&".join(qs) if qs else "")
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", loc)
        self.end_headers()

    def _flash(self, q):
        if q.get("ok"): return ("ok", q["ok"][0])
        if q.get("err"): return ("err", q["err"][0])
        if q.get("info"): return ("info", q["info"][0])
        return None

    def _require(self):
        staff = self._staff()
        if staff is None:
            self._redirect("/login")
            return None
        return staff

    def _handle_error(self, exc, back):
        if isinstance(exc, PermissionDenied):
            self._redirect(back, err=f"权限不足：{exc}")
        elif isinstance(exc, IdempotentReplay):
            self._redirect(back, info=f"幂等拦截：{exc}")
        elif isinstance(exc, (MinibarError,)):
            self._redirect(back, err=str(exc))
        else:
            raise exc

    # -- GET --
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/login":
                return self._render(login_page())
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/photos/"):
                return self._photo(path[len("/photos/"):])
            staff = self._require()
            if not staff:
                return
            flash = self._flash(qs)
            if path == "/":
                floor = int(qs["floor"][0]) if qs.get("floor") else None
                return self._render(rooms_page(staff, floor, flash))
            if path == "/room":
                return self._render(room_page(staff, int(qs["id"][0]), flash))
            if path == "/cart":
                return self._render(cart_page(staff, flash))
            if path == "/reviews":
                kind = qs.get("kind", [""])[0] or None
                return self._render(reviews_page(staff, flash, kind))
            if path == "/review-deny":
                return self._render(review_deny_page(staff, int(qs["id"][0]), flash))
            if path == "/review-wrongroom":
                return self._render(review_wrongroom_page(staff, int(qs["id"][0]), flash))
            if path == "/expiry":
                return self._render(expiry_page(staff, flash))
            if path == "/checkout":
                return self._render(checkout_page(staff, int(qs["id"][0]), flash))
            self.send_error(404)
        except PermissionDenied as e:
            self._redirect("/", err=f"权限不足：{e}")
        except Exception as e:
            self._render(f"<h1>500</h1><pre>{esc(e)}</pre>", 500)

    # -- POST --
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        fields, files = parse_form(self)
        # 登录 / 登出无需会话
        if path == "/login":
            self.send_response(303)
            self.send_header("Set-Cookie", f'mb_uid={fields.get("uid")}; Path=/')
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", "mb_uid=; Path=/; Max-Age=0")
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        staff = self._require()
        if not staff:
            return
        conn = None
        back = "/"
        try:
            conn = connect()

            if path == "/stocktake":
                oid = int(fields["order_id"])
                order = services._load_order(conn, oid)
                back = f"/room?id={oid}"
                counts = {int(k[2:]): int(v) for k, v in fields.items()
                          if k.startswith("q_") and v != ""}
                _, status, short = services.submit_stocktake(
                    conn, oid, counts, staff, fields["idem_key"],
                    note=fields.get("note", ""))
                conn.close()
                if status == "accepted":
                    msg = "盘点已提交，新版本快照已生成"
                    if short:
                        msg += f"；{len(short)} 种商品补货车短缺，已开复核"
                    return self._redirect(back, ok=msg)
                if status == "post_checkout":
                    return self._redirect(back, info="订单已冻结，盘点转入退房后补录复核")
                return self._redirect("/reviews?kind=offline_resend",
                                      err="旧版本盘点被拒绝，已进入离线重传复核")

            if path == "/consumption/confirm":
                cid = int(fields["cid"])
                lid = services.confirm_consumption(conn, cid, staff)
                oid = conn.execute("SELECT order_id FROM consumption WHERE id=?", (cid,)).fetchone()[0]
                conn.close()
                return self._redirect(f"/room?id={oid}", ok=f"消费已确认并入账（账单行 #{lid}）")

            if path == "/consumption/deny":
                cid = int(fields["cid"])
                c = conn.execute("SELECT order_id FROM consumption WHERE id=?", (cid,)).fetchone()
                services.guest_deny_consumption(conn, cid, staff, fields.get("note", ""))
                conn.close()
                return self._redirect("/reviews?kind=guest_denial", ok="已登记客人否认，进入争议复核")

            if path == "/consumption/wrongroom":
                cid = int(fields["cid"])
                src = conn.execute(
                    "SELECT o.room_id, o.id FROM consumption c JOIN stay_order o ON o.id=c.order_id "
                    "WHERE c.id=?", (cid,)).fetchone()
                target = fields.get("target_room_id")
                if not target:
                    row = conn.execute(
                        "SELECT r.id FROM room r JOIN stay_order o ON o.room_id=r.id AND o.state='open' "
                        "WHERE r.id != ? AND r.floor = (SELECT floor FROM room WHERE id=?) "
                        "ORDER BY r.room_no LIMIT 1", (src["room_id"], src["room_id"])).fetchone()
                    if not row:
                        return self._redirect(f"/room?id={src['id']}", err="同层没有其他在住房可作为跨房目标")
                    target = row["id"]
                services.flag_wrong_room(conn, cid, staff, int(target), fields.get("note", ""))
                conn.close()
                return self._redirect("/reviews?kind=wrong_room", ok="已登记跨房放错复核")

            if path == "/consumption/waive":
                cid = int(fields["cid"])
                reason = fields.get("reason", "guest_denial")
                note = fields.get("note", "") or "客人否认，经理核准免单"
                c = conn.execute("SELECT order_id FROM consumption WHERE id=?", (cid,)).fetchone()
                services.waive_consumption(conn, cid, staff, reason, note)
                conn.close()
                return self._redirect(f"/room?id={c[0]}", ok="经理免单已写入账单（负向行）")

            if path == "/review/wrongroom":
                case_id = int(fields["case_id"])
                services.resolve_wrong_room(conn, case_id, staff, fields.get("note", ""))
                conn.close()
                return self._redirect("/reviews?kind=wrong_room", ok="已裁定：冲销原房并转入目标房")

            if path == "/review/post-checkout":
                services.approve_post_checkout(conn, int(fields["case_id"]), staff)
                conn.close()
                return self._redirect("/reviews?kind=post_checkout", ok="补录已核准并追加账单")

            if path == "/review/resolve":
                services.resolve_case_generic(
                    conn, int(fields["case_id"]), staff, fields.get("resolution", ""))
                conn.close()
                return self._redirect("/reviews", ok="复核单已结案")

            if path == "/order/freeze":
                oid = int(fields["order_id"])
                services.freeze_checkout(conn, oid, staff)
                conn.close()
                return self._redirect(f"/checkout?id={oid}", ok="已退房冻结，最后盘点已锁定")

            if path == "/order/post-checkout":
                oid = int(fields["order_id"])
                services.post_checkout_add_charge(
                    conn, oid, int(fields["product_id"]), int(fields["qty"]),
                    staff, fields.get("note", ""))
                conn.close()
                return self._redirect(f"/checkout?id={oid}", info="退房后补录已提交，待经理核准")

            if path == "/order/settle":
                oid = int(fields["order_id"])
                result = services.settle_and_close(conn, oid, staff, settlement_adapter)
                conn.close()
                if result["ok"]:
                    return self._redirect(f"/checkout?id={oid}",
                                          ok=f"结算成功 {result['settlement_no']}，订单已关闭")
                return self._redirect(f"/checkout?id={oid}", err="结算通道返回失败，请重试")

            if path == "/batch/writeoff":
                bid = int(fields["batch_id"])
                reason = fields["reason"]
                services.write_off_batch(conn, bid, reason, staff, fields.get("note", ""))
                conn.close()
                return self._redirect("/expiry", ok=f"批次已按「{'过期' if reason=='expired' else '包装破损'}」报损（终态）")

            if path == "/batch/damaged":
                bno = fields.get("batch_no", "").strip()
                b = conn.execute("SELECT * FROM batch WHERE batch_no=?", (bno,)).fetchone()
                if not b:
                    conn.close()
                    return self._redirect("/cart", err=f"批次 {bno} 不存在")
                if "photo" not in files:
                    conn.close()
                    return self._redirect("/cart", err="请选择包装照片")
                pid, name = save_photo(conn, files["photo"], staff, batch_id=b["id"],
                                       note=fields.get("note", ""))
                conn.close()
                return self._redirect("/cart", ok=f"照片 {name} 已保存，批次标记破损，只能报损")

            self.send_error(404)
        except (MinibarError,) as e:
            if conn is not None:
                conn.rollback(); conn.close()
            if path.startswith("/batch/"):
                back = "/expiry" if path.endswith("writeoff") else "/cart"
            elif not ("/consumption/" in path or path.startswith("/order/") or path.startswith("/review/")):
                back = "/"
            self._handle_error(e, back)
        except Exception:
            if conn is not None:
                conn.rollback(); conn.close()
            raise
        else:
            if conn is not None:
                conn.close()


def _static_file(root, name, content_types):
    base = os.path.join(root, name)
    if not os.path.isfile(base):
        return None
    with open(base, "rb") as f:
        return f.read(), content_types.get(os.path.splitext(name)[1], "application/octet-stream")


def make_server(host="127.0.0.1", port=8000):
    static_root = os.path.join(os.path.dirname(__file__), "static")
    photo_root = str(config.PHOTO_DIR)
    cts = {".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
           ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

    def _static(self, name):
        found = _static_file(static_root, name, cts)
        if not found:
            return self.send_error(404)
        data, ct = found
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _photo(self, name):
        found = _static_file(photo_root, name, cts)
        if not found:
            return self.send_error(404)
        data, ct = found
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    Handler._static = _static
    Handler._photo = _photo
    return ThreadingHTTPServer((host, port), Handler)
