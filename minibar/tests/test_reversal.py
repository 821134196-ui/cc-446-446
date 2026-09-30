"""账单冲销幂等：经理 + 原因；重复冲销只产生一条 reversal；免单/冲销均幂等。"""
from minibar.errors import PermissionDenied, MinibarError
from minibar.tests.base import MiniBarTestCase


class ReversalIdempotencyTest(MiniBarTestCase):
    def setUp(self):
        super().setUp()
        self.room_batch("R201-W", "MB-W01", 4, 60, "201")
        self.cart_batch("CW-X", "MB-W01", 8, 120)
        self.oid = self.open_stay("201")
        self._take({self.pid["MB-W01"]: 2})
        self.cid = self.conn.execute(
            "SELECT id FROM consumption WHERE order_id=?", (self.oid,)).fetchone()["id"]
        self.line_id = self.__confirm()

    def test_reverse_requires_manager_and_reason(self):
        from minibar import services
        with self.assertRaises(PermissionDenied):
            services.reverse_bill_line(self.conn, self.line_id, self.staff["F1"], "更正")
        with self.assertRaises(MinibarError):
            services.reverse_bill_line(self.conn, self.line_id, self.staff["M1"], "  ")

    def test_reverse_is_idempotent(self):
        from minibar import services
        rid1 = services.reverse_bill_line(self.conn, self.line_id, self.staff["M1"], "误收更正")
        rid2 = services.reverse_bill_line(self.conn, self.line_id, self.staff["M1"], "再次点击")
        self.assertEqual(rid1, rid2)
        reversals = self.conn.execute(
            "SELECT * FROM bill_line WHERE line_type='reversal' AND reversal_of=?",
            (self.line_id,)).fetchall()
        self.assertEqual(len(reversals), 1)
        self.assertEqual(reversals[0]["amount"], -6.0)
        self.assertTrue(reversals[0]["reason"].startswith("冲销："))
        # 消费状态被置为 reversed
        self.assertEqual(self.conn.execute(
            "SELECT status FROM consumption WHERE id=?", (self.cid,)).fetchone()[0], "reversed")
        # 账单净额归零
        self.assertEqual(services.bill_total(self.conn, self.oid), 0.0)

    def test_cannot_reverse_a_reversal(self):
        from minibar import services
        rid = services.reverse_bill_line(self.conn, self.line_id, self.staff["M1"], "更正")
        with self.assertRaises(MinibarError):
            services.reverse_bill_line(self.conn, rid, self.staff["M1"], "连环冲销")

    def test_waiver_is_idempotent_and_requires_manager(self):
        from minibar import services
        with self.assertRaises(PermissionDenied):
            services.waive_consumption(self.conn, self.cid, self.staff["F1"],
                                       "guest_denial", "客人否认")
        wid1 = services.waive_consumption(self.conn, self.cid, self.staff["M1"],
                                          "guest_denial", "监控核实")
        # 再免单一次走 idem_key 幂等分支
        from minibar.errors import IdempotentReplay
        with self.assertRaises(IdempotentReplay):
            services.waive_consumption(self.conn, self.cid, self.staff["M1"],
                                       "guest_denial", "重复提交")
        waivers = self.conn.execute(
            "SELECT COUNT(*) c FROM bill_line WHERE line_type='waiver'").fetchone()["c"]
        self.assertEqual(waivers, 1)
        self.assertEqual(wid1, self.conn.execute(
            "SELECT bill_line_id FROM consumption WHERE id=?", (self.cid,)).fetchone()[0])
        # charge +6 与 waiver -6 冲抵，净额为 0
        self.assertEqual(services.bill_total(self.conn, self.oid), 0.0)

    def test_waiver_of_pending_consumption_records_zero_amount_reason(self):
        from minibar import services
        # 新建一笔未确认消费再免单：无 charge 可冲抵，金额 0 但原因留存
        self._take({self.pid["MB-W01"]: 1})
        cid2 = self.conn.execute(
            "SELECT id FROM consumption WHERE id != ? ORDER BY id DESC LIMIT 1",
            (self.cid,)).fetchone()["id"]
        services.waive_consumption(self.conn, cid2, self.staff["M1"],
                                   "product_quality", "临期口感异常")
        line = self.conn.execute(
            "SELECT * FROM bill_line WHERE ref_id=? AND line_type='waiver'",
            (cid2,)).fetchone()
        self.assertEqual(line["amount"], 0.0)
        self.assertIn("product_quality", line["reason"])

    def test_invalid_waiver_reason_rejected(self):
        from minibar import services
        with self.assertRaises(MinibarError):
            services.waive_consumption(self.conn, self.cid, self.staff["M1"],
                                       "because_i_say_so", "x")

    def __confirm(self):
        from minibar import services
        return services.confirm_consumption(self.conn, self.cid, self.staff["F1"])

    def _take(self, counted):
        from minibar import services
        n = self.conn.execute("SELECT COUNT(*) c FROM stocktake").fetchone()["c"]
        return services.submit_stocktake(
            self.conn, self.oid, counted, self.staff["A2"], f"take-{n+1}")
