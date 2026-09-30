"""测试夹具：临时库 + 固定时钟 + 最小主数据。"""
import tempfile
import unittest
from pathlib import Path

from minibar import config, services
from minibar.clock import clock
from minibar.db import connect, init_db


class MiniBarTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config.DB_PATH = root / "test.db"
        config.PHOTO_DIR = root / "photos"
        clock.set_fixed("2026-09-30T10:00:00")
        init_db(config.DB_PATH)
        self.conn = connect(config.DB_PATH)
        self._bootstrap()

    def tearDown(self):
        self.conn.close()
        clock.reset()
        self.tmp.cleanup()

    def _bootstrap(self):
        c = self.conn
        self.staff = {}
        for code, name, role, scope in [
                ("M1", "经理", "manager", "*"),
                ("F1", "前台全楼", "front_desk", "*"),
                ("F2", "前台二楼", "front_desk", "2"),
                ("A2", "服务员二楼", "attendant", "2"),
                ("A3", "服务员三楼", "attendant", "3")]:
            cur = c.execute("INSERT INTO staff (code,name,role,floor_scope) VALUES (?,?,?,?)",
                            (code, name, role, scope))
            self.staff[code] = c.execute("SELECT * FROM staff WHERE id=?",
                                         (cur.lastrowid,)).fetchone()
        self.rooms = {}
        for no, floor in (("201", 2), ("202", 2), ("301", 3)):
            cur = c.execute("INSERT INTO room (room_no,floor,status,pms_status) VALUES (?,?, 'vacant','VC')",
                            (no, floor))
            self.rooms[no] = cur.lastrowid
        self.pid = {}
        for sku, name, price in (("MB-W01", "矿泉水", 3.0),
                                 ("MB-B01", "啤酒", 12.0)):
            cur = c.execute(
                "INSERT INTO product (sku,name,category,unit_price,cost_price,image) "
                "VALUES (?,?, '饮品', ?, 1, '')", (sku, name, price))
            self.pid[sku] = cur.lastrowid
        for rid in self.rooms.values():
            c.execute("INSERT INTO display_cap (room_id,product_id,cap_qty) VALUES (?,?,4)",
                      (rid, self.pid["MB-W01"]))
            c.execute("INSERT INTO display_cap (room_id,product_id,cap_qty) VALUES (?,?,2)",
                      (rid, self.pid["MB-B01"]))
        c.commit()

    # -- 辅助 --
    def cart_batch(self, batch_no, sku, qty, expiry_offset, damaged=0):
        cur = self.conn.execute(
            "INSERT INTO batch (batch_no,product_id,qty,initial_qty,expiry_date,loc,cart_id,"
            "damaged,created_at) VALUES (?,?,?,?,?, 'cart',1,?,?)",
            (batch_no, self.pid[sku], qty, qty, _d(expiry_offset), damaged, clock.iso()))
        return cur.lastrowid

    def room_batch(self, batch_no, sku, qty, expiry_offset, room_no, damaged=0):
        cur = self.conn.execute(
            "INSERT INTO batch (batch_no,product_id,qty,initial_qty,expiry_date,loc,room_id,"
            "damaged,created_at) VALUES (?,?,?,?,?, 'room',?, ?, ?)",
            (batch_no, self.pid[sku], qty, qty, _d(expiry_offset),
             self.rooms[room_no], damaged, clock.iso()))
        return cur.lastrowid

    def open_stay(self, room_no, who="F1"):
        return services.open_stay(self.conn, self.rooms[room_no],
                                  f"GUEST-{room_no}", self.staff[who])

    def caps(self, room_no, **overrides):
        base = {self.pid["MB-W01"]: 4, self.pid["MB-B01"]: 2}
        base.update({self.pid[k]: v for k, v in overrides.items()})
        return base


def _d(offset):
    from datetime import timedelta
    return (clock.today() + timedelta(days=offset)).isoformat()
