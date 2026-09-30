"""领域异常与权限工具。"""
from . import config


class MinibarError(Exception):
    """业务规则错误（消息可直接展示给前台）。"""


class VersionConflict(MinibarError):
    """盘点提交基于旧版本，不能覆盖新快照。"""


class OrderFrozen(MinibarError):
    """退房冻结后禁止常规写操作。"""


class PermissionDenied(MinibarError):
    """角色或楼层权限不足。"""


class IdempotentReplay(MinibarError):
    """幂等重放：操作此前已处理过。"""


def require_role(staff, *roles):
    if staff is None or staff["role"] not in roles:
        raise PermissionDenied("当前角色无权执行该操作")
    return True


def can_access_floor(staff, floor):
    """楼层权限：'*' 全部，否则需在 floor_scope 列表内。"""
    if staff is None:
        raise PermissionDenied("未登录")
    scope = staff["floor_scope"]
    if scope == "*":
        return True
    allowed = {int(x) for x in scope.split(",") if x.strip()}
    if int(floor) not in allowed:
        raise PermissionDenied(f"无权访问 {floor} 层客房")
    return True


ROLE_LABELS = {
    config.ROLE_MANAGER: "经理",
    config.ROLE_FRONT_DESK: "前台",
    config.ROLE_ATTENDANT: "服务员",
}
