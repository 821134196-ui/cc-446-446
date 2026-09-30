"""全局配置：路径、主题、业务常量。均为演示默认值，可用环境变量覆盖。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MINIBAR_DATA", BASE_DIR.parent / "data"))
DB_PATH = DATA_DIR / "minibar.db"
PHOTO_DIR = DATA_DIR / "photos"

# 主题：象牙白 / 藏青 / 香槟金
THEME = {
    "ivory": "#FBF8F1",
    "ivory_2": "#F4EFE3",
    "navy": "#1F2A44",
    "navy_2": "#2E3D63",
    "champagne": "#C9A86A",
    "champagne_2": "#E3CEA0",
    "ink": "#26282E",
    "muted": "#6B7280",
    "danger": "#9B3A3A",
    "ok": "#3E7A4F",
}

# 效期阈值（天）
EXPIRY_WARN_DAYS = 14          # <=14 天视为临期提醒
SHELF_LIFE_WARNING_DAYS = 14

# 盘点提醒：入住期间每日截至时刻仍未盘点则提醒
STOCKTAKE_DEADLINE_HOUR = 20

# 角色
ROLE_MANAGER = "manager"
ROLE_FRONT_DESK = "front_desk"
ROLE_ATTENDANT = "attendant"

# 库存批次位置
LOC_CART = "cart"          # 补货车
LOC_ROOM = "room"          # 客房
LOC_WRITEOFF = "writeoff"  # 报损（终态，不可再次上架）

# 复核类型
REV_GUEST_DENIAL = "guest_denial"        # 客人否认消费
REV_WRONG_ROOM = "wrong_room"            # 跨房放错
REV_BATCH_EXPIRING = "batch_expiring"    # 批次临期
REV_CART_SHORTAGE = "cart_shortage"      # 补货车短缺
REV_OFFLINE_RESEND = "offline_resend"    # 盘点离线重传（版本冲突）
REV_POST_CHECKOUT = "post_checkout"      # 退房后补录
REVIEW_TYPES = (
    REV_GUEST_DENIAL,
    REV_WRONG_ROOM,
    REV_BATCH_EXPIRING,
    REV_CART_SHORTAGE,
    REV_OFFLINE_RESEND,
    REV_POST_CHECKOUT,
)

# 免单原因（可扩展）
WAIVER_REASONS = (
    "guest_denial",       # 客人否认
    "wrong_room",         # 跨房放错
    "product_quality",    # 商品质量
    "service_recovery",   # 服务补偿
)

# 报损原因
WRITEOFF_REASONS = (
    "expired",            # 已过期
    "damaged",            # 包装破损
)
