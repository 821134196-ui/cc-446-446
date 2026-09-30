"""批次先进先出（FEFO）：补货与房内消耗都按效期升序；过期/破损不参与。"""
from minibar.errors import MinibarError, IdempotentReplay
from minibar.tests.base import MiniBarTestCase


class FefoTest(MiniBarTestCase):
    def setUp(self):
        super().setUp()
        self.room_batch("R201-W", "MB-W01", 4, 60, "201")
        self.room_batch("R201-B", "MB-B01", 2, 90, "201")

    def test_cart_replenishment_uses_nearest_expiry_first(self):
        # 车上两批：近效期 3 天、远效期 120 天
        near = self.cart_batch("CW-NEAR", "MB-W01", 2, 3)
        far = self.cart_batch("CW-FAR", "MB-W01", 10, 120)
        oid = self.open_stay("201")
        # 消耗 3 瓶 -> 需补 3
        self._take(oid, {self.pid["MB-W01"]: 1})
        near_row = self.conn.execute("SELECT * FROM batch WHERE id=?", (near,)).fetchone()
        far_row = self.conn.execute("SELECT * FROM batch WHERE id=?", (far,)).fetchone()
        # 近效期 2 件全部转走；再从远效期拆 1 件
        self.assertEqual(near_row["loc"], "room")
        self.assertEqual(near_row["qty"], 2)
        self.assertEqual(far_row["loc"], "cart")
        self.assertEqual(far_row["qty"], 9)
        child = self.conn.execute(
            "SELECT * FROM batch WHERE batch_no LIKE 'CW-FAR#S%'").fetchone()
        self.assertIsNotNone(child)
        self.assertEqual(child["loc"], "room")
        self.assertEqual(child["qty"], 1)
        self.assertEqual(child["expiry_date"], far_row["expiry_date"])
        # 补回后快照仍为陈列上限 4
        _, items = __import__("minibar.services", fromlist=["x"]).latest_snapshot_items(
            self.conn, oid)
        w = [i for i in items if i["product_id"] == self.pid["MB-W01"]][0]
        self.assertEqual(w["qty"], 4)

    def test_expired_and_damaged_cart_batches_never_move_to_room(self):
        self.cart_batch("CW-OLD", "MB-W01", 5, -1)       # 已过期
        self.cart_batch("CW-BAD", "MB-W01", 5, 30, damaged=1)  # 包装破损
        self.cart_batch("CW-OK", "MB-W01", 1, 40)
        oid = self.open_stay("201")
        _, _, shortages = self._take(oid, {self.pid["MB-W01"]: 2})  # 需补 2，可用仅 1
        self.assertEqual(shortages, [(self.pid["MB-W01"], 1)])
        old = self.conn.execute("SELECT loc FROM batch WHERE batch_no='CW-OLD'").fetchone()[0]
        bad = self.conn.execute("SELECT loc FROM batch WHERE batch_no='CW-BAD'").fetchone()[0]
        self.assertEqual(old, "cart")
        self.assertEqual(bad, "cart")

    def test_room_consumption_fefo_nearest_expiry(self):
        # 房内额外放入一批更近效期的水；消耗应先扣近批
        near = self.room_batch("R201-W2", "MB-W01", 2, 5, "201")
        far = self.conn.execute("SELECT id FROM batch WHERE batch_no='R201-W'").fetchone()["id"]
        self.cart_batch("CW-X", "MB-W01", 10, 200)
        oid = self.open_stay("201")
        self._take(oid, {self.pid["MB-W01"]: 4})  # 6 -> 4，消耗 2
        self.assertEqual(self.conn.execute("SELECT qty FROM batch WHERE id=?", (near,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT qty FROM batch WHERE id=?", (far,)).fetchone()[0], 4)

    def test_writeoff_is_terminal_and_cannot_restock(self):
        bid = self.cart_batch("CW-OLD", "MB-W01", 3, -2)
        from minibar import services
        services.write_off_batch(self.conn, bid, "expired", self.staff["M1"])
        row = self.conn.execute("SELECT * FROM batch WHERE id=?", (bid,)).fetchone()
        self.assertEqual(row["loc"], "writeoff")
        self.assertEqual(row["writeoff_reason"], "expired")
        with self.assertRaises(IdempotentReplay):
            services.write_off_batch(self.conn, bid, "expired", self.staff["M1"])

    def test_unexpired_cannot_be_written_off_as_expired(self):
        bid = self.cart_batch("CW-FRESH", "MB-W01", 3, 10)
        from minibar import services
        with self.assertRaises(MinibarError):
            services.write_off_batch(self.conn, bid, "expired", self.staff["M1"])

    def test_damaged_batch_can_only_be_written_off(self):
        bid = self.cart_batch("CW-BAD", "MB-W01", 2, 30, damaged=1)
        from minibar import services
        # 标记破损后 FEFO 不会取用它（补别的房也不允许）
        oid = self.open_stay("202")
        _, _, shortages = self._take(oid, {self.pid["MB-W01"]: 3})
        # 全车仅有这一批破损 -> 全部短缺
        self.assertEqual(shortages, [(self.pid["MB-W01"], 1)])
        services.write_off_batch(self.conn, bid, "damaged", self.staff["A2"])
        self.assertEqual(self.conn.execute(
            "SELECT loc FROM batch WHERE id=?", (bid,)).fetchone()[0], "writeoff")

    def _take(self, oid, counted):
        from minibar import services
        return services.submit_stocktake(
            self.conn, oid, counted, self.staff["A2"],
            f"take-{oid}-{len(self.conn.execute('SELECT id FROM stocktake').fetchall())+1}")
