"""退房冻结：锁住最后一次盘点；前台确认账单后才可关闭；冻结后补录走复核。"""
from minibar.errors import OrderFrozen, IdempotentReplay, MinibarError
from minibar.tests.base import MiniBarTestCase


class CheckoutFreezeTest(MiniBarTestCase):
    def setUp(self):
        super().setUp()
        self.room_batch("R201-W", "MB-W01", 4, 60, "201")
        self.cart_batch("CW-X", "MB-W01", 8, 120)
        self.oid = self.open_stay("201")
        self._take({self.pid["MB-W01"]: 2})  # 消费 2，补回 2 -> v1
        self.cid = self.conn.execute(
            "SELECT id FROM consumption WHERE order_id=? AND status='pending'",
            (self.oid,)).fetchone()["id"]

    def test_freeze_locks_snapshot_and_blocks_stocktake(self):
        from minibar import services
        services.freeze_checkout(self.conn, self.oid, self.staff["F1"])
        v = self.conn.execute("SELECT snap_version FROM stay_order WHERE id=?",
                              (self.oid,)).fetchone()["snap_version"]
        # 冻结后盘点不能改库存，转退房后补录复核
        _, status, _ = services.submit_stocktake(
            self.conn, self.oid, {self.pid["MB-W01"]: 1}, self.staff["A2"], "take-frozen")
        self.assertEqual(status, "post_checkout")
        self.assertEqual(self.conn.execute(
            "SELECT snap_version FROM stay_order WHERE id=?", (self.oid,)).fetchone()[0], v)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM review_case WHERE kind='post_checkout'").fetchone())

    def test_cannot_close_until_front_desk_confirms_all_consumption(self):
        from minibar import services
        services.freeze_checkout(self.conn, self.oid, self.staff["F1"])
        with self.assertRaises(MinibarError):  # 仍有 pending 消费
            services.settle_and_close(self.conn, self.oid, self.staff["F1"], _FakeAdapter())
        services.confirm_consumption(self.conn, self.cid, self.staff["F1"])
        result = services.settle_and_close(self.conn, self.oid, self.staff["F1"], _FakeAdapter())
        self.assertTrue(result["ok"])
        # 时间线含冻结与结算事件，且无未决项时不抛异常
        kinds = {e["kind"] for e in services.order_timeline(self.conn, self.oid)}
        self.assertIn("frozen", kinds)
        self.assertIn("settled", kinds)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM stay_order WHERE id=?", (self.oid,)).fetchone()[0], "closed")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM room WHERE id=?", (self.rooms["201"],)).fetchone()[0], "vacant")
        # 关闭后再盘点被拒
        with self.assertRaises(OrderFrozen):
            services.submit_stocktake(
                self.conn, self.oid, {self.pid["MB-W01"]: 4}, self.staff["A2"], "take-closed")

    def test_open_case_blocks_close(self):
        from minibar import services
        services.freeze_checkout(self.conn, self.oid, self.staff["F1"])
        services.confirm_consumption(self.conn, self.cid, self.staff["F1"])
        # 冻结后补录产生未结复核 -> 不能关单
        services.post_checkout_add_charge(
            self.conn, self.oid, self.pid["MB-B01"], 1, self.staff["F1"], "查房少货")
        with self.assertRaises(MinibarError):
            services.settle_and_close(self.conn, self.oid, self.staff["F1"], _FakeAdapter())
        case = self.conn.execute(
            "SELECT id FROM review_case WHERE kind='post_checkout'").fetchone()["id"]
        services.approve_post_checkout(self.conn, case, self.staff["M1"])
        result = services.settle_and_close(self.conn, self.oid, self.staff["F1"], _FakeAdapter())
        self.assertTrue(result["ok"])

    def test_double_freeze_is_idempotent_play(self):
        from minibar import services
        services.freeze_checkout(self.conn, self.oid, self.staff["F1"])
        with self.assertRaises(IdempotentReplay):
            services.freeze_checkout(self.conn, self.oid, self.staff["F1"])

    def _take(self, counted):
        from minibar import services
        n = self.conn.execute("SELECT COUNT(*) c FROM stocktake").fetchone()["c"]
        return services.submit_stocktake(
            self.conn, self.oid, counted, self.staff["A2"], f"take-{n+1}")


class _FakeAdapter:
    def settle(self, order_no, amount):
        return {"ok": True, "settlement_no": "ST-TEST", "channel": "MOCK-PSC",
                "amount": amount, "raw": "{}"}
