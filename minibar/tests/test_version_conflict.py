"""版本冲突：同一客房两次盘点必须按版本提交，旧版本不得覆盖新快照。"""
from minibar.errors import IdempotentReplay
from minibar.tests.base import MiniBarTestCase


class VersionConflictTest(MiniBarTestCase):
    def setUp(self):
        super().setUp()
        self.room_batch("R201-W", "MB-W01", 4, 60, "201")
        self.room_batch("R201-B", "MB-B01", 2, 90, "201")
        self.cart_batch("CW-FAR", "MB-W01", 8, 120)
        self.order_id = self.open_stay("201")

    def test_chain_advances_one_version_per_accepted_take(self):
        v0 = self.conn.execute("SELECT snap_version FROM stay_order WHERE id=?",
                               (self.order_id,)).fetchone()["snap_version"]
        self.assertEqual(v0, 0)
        _, status, _ = self.__take("take-1", {self.pid["MB-W01"]: 3}, v0)
        self.assertEqual(status, "accepted")
        v1 = self.conn.execute("SELECT snap_version FROM stay_order WHERE id=?",
                               (self.order_id,)).fetchone()["snap_version"]
        self.assertEqual(v1, 1)
        # 快照不可变且父子相连
        snap = self.conn.execute(
            "SELECT * FROM stock_snapshot WHERE order_id=? ORDER BY version",
            (self.order_id,)).fetchall()
        self.assertEqual([(s["version"], s["parent_version"], s["kind"]) for s in snap],
                         [(0, None, "check_in"), (1, 0, "stocktake")])

    def test_stale_version_rejected_and_never_overwrites(self):
        _, _, _ = self.__take("take-v1", {self.pid["MB-W01"]: 3}, 0)
        # v1 中矿泉水补货后回到 4；旧版本 v0 的离线表单声称数量 1（想记 3 瓶消费）
        _, status, _ = self.__take_ver("take-stale", {self.pid["MB-W01"]: 1}, 0)
        self.assertEqual(status, "rejected_stale")
        # 当前版本仍是 v1，不能被旧表单覆盖
        self.assertEqual(self.conn.execute(
            "SELECT snap_version FROM stay_order WHERE id=?", (self.order_id,)).fetchone()[0], 1)
        # 旧表单的减少量没有形成消费
        cons = self.conn.execute("SELECT COUNT(*) c FROM consumption WHERE order_id=?",
                                 (self.order_id,)).fetchone()["c"]
        self.assertEqual(cons, 1)  # 只有 v1 那笔 1 瓶
        # 落离线重传复核
        case = self.conn.execute(
            "SELECT * FROM review_case WHERE kind='offline_resend'").fetchone()
        self.assertIsNotNone(case)
        self.assertEqual(case["stocktake_id"], self.conn.execute(
            "SELECT id FROM stocktake WHERE idem_key='take-stale'").fetchone()["id"])

    def test_offline_resend_same_idem_key_is_idempotent(self):
        self.__take("take-dup", {self.pid["MB-W01"]: 3}, 0)
        with self.assertRaises(IdempotentReplay):
            self.__take("take-dup", {self.pid["MB-W01"]: 2}, 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM stocktake WHERE idem_key='take-dup'").fetchone()["c"], 1)

    def test_take_on_current_version_after_conflict_succeeds(self):
        self.__take("take-v1", {self.pid["MB-W01"]: 3}, 0)
        _, status, _ = self.__take_ver("take-v2", {self.pid["MB-W01"]: 2}, 1)
        self.assertEqual(status, "accepted")
        self.assertEqual(self.conn.execute(
            "SELECT snap_version FROM stay_order WHERE id=?", (self.order_id,)).fetchone()[0], 2)

    def __take(self, key, counts, ver):
        return self.__take_ver(key, counts, ver)

    def __take_ver(self, key, counts, ver):
        from minibar import services
        return services.submit_stocktake_with_version(
            self.conn, self.order_id, counts, self.staff["A2"],
            key, expected_version=ver)
