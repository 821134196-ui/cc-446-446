"""楼层权限：2F 员工不得操作 3F 客房（页面与服务层双重校验）。"""
from minibar.errors import PermissionDenied
from minibar.tests.base import MiniBarTestCase


class FloorPermissionTest(MiniBarTestCase):
    def setUp(self):
        super().setUp()
        for no in ("201", "202"):
            self.room_batch(f"R{no}-W", "MB-W01", 4, 60, no)
        self.room_batch("R301-W", "MB-W01", 4, 60, "301")
        self.cart_batch("CW-X", "MB-W01", 12, 120)
        self.o201 = self.open_stay("201")
        self.o301 = self.open_stay("301", who="F1")

    def test_attendant_2f_cannot_stocktake_3f(self):
        from minibar import services
        with self.assertRaises(PermissionDenied):
            services.submit_stocktake(
                self.conn, self.o301, {self.pid["MB-W01"]: 3},
                self.staff["A2"], "take-crossfloor")
        # 3F 服务员可以
        _, status, _ = services.submit_stocktake(
            self.conn, self.o301, {self.pid["MB-W01"]: 3},
            self.staff["A3"], "take-301-ok")
        self.assertEqual(status, "accepted")

    def test_frontdesk_2f_cannot_freeze_or_confirm_3f(self):
        from minibar import services
        _, _, _ = services.submit_stocktake(
            self.conn, self.o301, {self.pid["MB-W01"]: 3},
            self.staff["A3"], "take-301-consume")
        cid = self.conn.execute(
            "SELECT id FROM consumption WHERE order_id=?", (self.o301,)).fetchone()["id"]
        with self.assertRaises(PermissionDenied):
            services.confirm_consumption(self.conn, cid, self.staff["F2"])
        with self.assertRaises(PermissionDenied):
            services.freeze_checkout(self.conn, self.o301, self.staff["F2"])

    def test_attendant_cannot_freeze_or_waive(self):
        from minibar import services
        with self.assertRaises(PermissionDenied):
            services.freeze_checkout(self.conn, self.o201, self.staff["A2"])
        cid = self.conn.execute(
            "SELECT id FROM consumption WHERE order_id=?", (self.o201,)).fetchone()
        # 先产生一笔消费
        services.submit_stocktake(
            self.conn, self.o201, {self.pid["MB-W01"]: 3}, self.staff["A2"], "take-201-x")
        cid = self.conn.execute(
            "SELECT id FROM consumption WHERE order_id=?", (self.o201,)).fetchone()["id"]
        with self.assertRaises(PermissionDenied):
            services.waive_consumption(self.conn, cid, self.staff["A2"],
                                       "guest_denial", "x")

    def test_floor_list_is_scoped(self):
        from minibar import services
        rows = services.floor_room_list(self.conn, self.staff["F2"])
        floors = {r["floor"] for r in rows}
        self.assertEqual(floors, {2})
        rows_all = services.floor_room_list(self.conn, self.staff["F1"])
        self.assertEqual({r["floor"] for r in rows_all}, {2, 3})

    def test_manager_bypasses_floor_scope(self):
        from minibar import services
        _, status, _ = services.submit_stocktake(
            self.conn, self.o301, {self.pid["MB-W01"]: 2},
            self.staff["M1"], "take-mgr-301")
        self.assertEqual(status, "accepted")
