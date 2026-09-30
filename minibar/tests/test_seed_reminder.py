"""种子与提醒的冒烟测试：演示样例可完整载入，六类复核齐全，提醒会去重。"""
from minibar import config, services
from minibar.clock import clock
from minibar.db import connect
from minibar.reminders import InProcessReminder
from minibar.tests.base import MiniBarTestCase


class SeedSmokeTest(MiniBarTestCase):
    def test_seed_loads_all_review_kinds_and_open_frozen_stays(self):
        # 夹具库上直接跑种子（init_db 幂等；种子检测 staff 非空会跳过，故清空 staff 相关）
        self.conn.executescript(
            "DELETE FROM stock_snapshot_item; DELETE FROM stock_snapshot; "
            "DELETE FROM stocktake_count; DELETE FROM stocktake; DELETE FROM consumption; "
            "DELETE FROM bill_line; DELETE FROM settlement; DELETE FROM review_case; "
            "DELETE FROM photo; DELETE FROM alert_log; DELETE FROM batch; "
            "DELETE FROM display_cap; DELETE FROM stay_order; DELETE FROM product; "
            "DELETE FROM room; DELETE FROM staff;")
        self.conn.commit()
        from minibar.seed import seed
        self.assertTrue(seed(reset=False))
        kinds = {r["kind"] for r in self.conn.execute(
            "SELECT DISTINCT kind FROM review_case").fetchall()}
        self.assertEqual(kinds, set(config.REVIEW_TYPES))
        states = {r["state"] for r in self.conn.execute(
            "SELECT DISTINCT state FROM stay_order").fetchall()}
        self.assertIn("open", states)
        self.assertIn("frozen", states)
        # 待退冻结单账单非空
        frozen_total = self.conn.execute(
            "SELECT o.id, (SELECT COALESCE(SUM(amount),0) FROM bill_line bl WHERE bl.order_id=o.id) t "
            "FROM stay_order o WHERE o.state='frozen'").fetchall()
        self.assertTrue(any(r["t"] > 0 for r in frozen_total))


class ReminderTest(MiniBarTestCase):
    def test_expiring_and_not_stocktaken_alerts_dedupe(self):
        from datetime import timedelta
        self.room_batch("R201-W", "MB-W01", 4, 60, "201")
        self.cart_batch("CW-SOON", "MB-W01", 3, 5)     # 临期
        self.cart_batch("CW-GONE", "MB-B01", 1, -1)    # 已过期
        self.open_stay("201")  # 今日未盘点
        self.conn.commit()     # 提醒任务使用独立连接，需先提交
        got = []
        rem = InProcessReminder(lambda: connect(config.DB_PATH),
                                interval=999, sink=lambda k, m: got.append((k, m)))
        clock.set_fixed(clock.now().replace(hour=21))  # 过截止时刻
        first = rem.scan_once()
        second = rem.scan_once()
        self.assertTrue(any("临期" in m for m in first))
        self.assertTrue(any("过期" in m for m in first))
        self.assertTrue(any("尚未盘点" in m for m in first))
        # 第二次扫描全部去重
        self.assertEqual(second, [])
        # 临期自动开复核
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM review_case WHERE kind='batch_expiring'").fetchone())
