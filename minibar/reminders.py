"""进程内提醒任务：临期批次、在住客房未盘提醒。

简单守护线程，每隔 interval 扫描一次；发送动作走可注入 sink（默认打印），
并写 alert_log 去重（同一 ref_key 只提醒一次，结案/换日后重置由种子或手工清）。
"""
import threading
import time

from . import config, services
from .clock import clock


class InProcessReminder(threading.Thread):
    def __init__(self, conn_factory, interval=60, sink=None):
        super().__init__(daemon=True)
        self.conn_factory = conn_factory
        self.interval = interval
        self.sink = sink or (lambda kind, msg: print(f"[提醒:{kind}] {msg}"))
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.scan_once()
            except Exception as exc:  # 提醒失败不影响主进程
                self.sink("error", f"提醒扫描失败: {exc}")

    def stop(self):
        self._stop.set()

    def scan_once(self):
        conn = self.conn_factory()
        try:
            return self.scan_expiring(conn) + self.scan_not_stocktaken(conn)
        finally:
            conn.close()

    def scan_expiring(self, conn):
        sent = []
        for b in services.expiring_batches(conn):
            if b["tag"] == "expired":
                # 已过期只提醒转报损，不再开复核
                ref = f"batch:{b['id']}:expired"
                msg = f"批次 {b['batch_no']} {b['product_name']} 已过期 {-b['days_to_expiry']} 天，须报损"
            else:
                ref = f"batch:{b['id']}:expiring"
                msg = (f"批次 {b['batch_no']} {b['product_name']} 临期：{b['expiry_date']} "
                       f"到期（剩 {b['days_to_expiry']} 天），请优先 FEFO 上架或复核")
                self._ensure_expiring_case(conn, b)
            if self._emit(conn, "expiring", ref, msg):
                sent.append(msg)
        return sent

    def _ensure_expiring_case(self, conn, b):
        """补货车临期批次自动开 batch_expiring 复核（同批次已开则不重复）。

        房内拆批子批只发提醒、不开单——它们随盘点 FEFO 自然消耗，
        否则每次补货拆批都会刷出重复复核。
        """
        if b["loc"] != "cart":
            return
        exists = conn.execute(
            "SELECT 1 FROM review_case WHERE kind='batch_expiring' AND batch_id=? "
            "AND status IN ('open','in_review')", (b["id"],)).fetchone()
        if not exists:
            system = conn.execute("SELECT * FROM staff WHERE role='manager' ORDER BY id LIMIT 1").fetchone()
            conn.execute(
                "INSERT INTO review_case (case_no, kind, order_id, room_id, batch_id, "
                "product_id, qty, summary, status, opened_by, created_at) "
                "VALUES (?, 'batch_expiring', NULL, NULL, ?, ?, ?, ?, 'open', ?, ?)",
                (services._new_case_no(conn), b["id"], b["product_id"], b["qty"],
                 f"补货车批次临期（{b['days_to_expiry']} 天内到期），请优先 FEFO 发出或报损评估",
                 system["id"] if system else None, clock.iso()))
            conn.commit()

    def scan_not_stocktaken(self, conn):
        """在住客房今日无 accepted 盘点且已过截止时刻 -> 未盘提醒。"""
        sent = []
        if clock.now().hour < config.STOCKTAKE_DEADLINE_HOUR:
            return sent
        today = clock.today().isoformat()
        rows = conn.execute(
            "SELECT o.*, r.room_no, r.floor FROM stay_order o JOIN room r ON r.id=o.room_id "
            "WHERE o.state='open'").fetchall()
        for o in rows:
            done = conn.execute(
                "SELECT 1 FROM stocktake WHERE order_id=? AND status='accepted' "
                "AND date(submitted_at)=date(?)", (o["id"], today)).fetchone()
            if done:
                continue
            ref = f"room:{o['room_id']}:{today}"
            msg = f"房 {o['room_no']} 今日尚未盘点（在住，{o['guest_alias']}），请安排服务员盘点"
            if self._emit(conn, "not_stocktaken", ref, msg):
                sent.append(msg)
        return sent

    def _emit(self, conn, kind, ref_key, message):
        try:
            conn.execute(
                "INSERT INTO alert_log (kind, ref_key, message, created_at) VALUES (?, ?, ?, ?)",
                (kind, ref_key, message, clock.iso()))
            conn.commit()
        except Exception:
            return False
        self.sink(kind, message)
        return True
