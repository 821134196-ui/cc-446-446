"""模拟外部适配器：PMS 房态、支付结算通道。

不依赖真实系统；返回结构与真实适配器保持一致，方便日后替换。
"""
import hashlib
import json

from .clock import clock


class MockPMSAdapter:
    """模拟酒店物业管理系统房态。

    码表（与真实 PMS 习惯一致）：
      VC vacant clean   空房干净
      OC occupied       在住
      OD occupied dirty 住客待退（退房冻结）
      OO out of order   维修房
    """
    STATUS_MAP = {
        "vacant": "VC",
        "occupied": "OC",
        "checkout_pending": "OD",
        "out_of_order": "OO",
    }

    def fetch_room_states(self, floors=None):
        """演示适配器不真正推送；提供码表翻译与模拟报文。"""
        return {
            "source": "MOCK-PMS",
            "at": clock.iso(),
            "floors": floors or "*",
            "codes": dict(self.STATUS_MAP),
        }

    def describe(self, internal_status):
        return self.STATUS_MAP.get(internal_status, "??")


class MockSettlementAdapter:
    """模拟支付结算通道：按订单号稳定返回收单号与成功结果。"""

    channel = "MOCK-PSC"

    def settle(self, order_no, amount):
        digest = hashlib.sha1(f"{order_no}:{round(amount,2)}".encode()).hexdigest()[:8].upper()
        ok = not order_no.endswith("FAIL")  # 订单号以 FAIL 结尾可模拟失败
        return {
            "ok": ok,
            "settlement_no": f"ST-{digest}",
            "channel": self.channel,
            "amount": round(amount, 2),
            "raw": json.dumps({
                "order_no": order_no, "amount": round(amount, 2),
                "currency": "CNY", "result": "SUCCESS" if ok else "DECLINED",
            }, ensure_ascii=False),
        }


pms_adapter = MockPMSAdapter()
settlement_adapter = MockSettlementAdapter()
