"""一条命令启动：初始化 SQLite、载入演示样例（库空时）、启动提醒任务与 Web。

    python -m minibar.app            # 默认 127.0.0.1:8000
    MINIBAR_PORT=8080 python -m minibar.app
    python -m minibar.app --reset    # 重建演示数据
"""
import argparse
import os
import sys
import time

from . import config
from .clock import clock
from .db import init_db
from .reminders import InProcessReminder
from .web.server import make_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="迷你吧补货与离店结算平台（匿名演示）")
    parser.add_argument("--host", default=os.environ.get("MINIBAR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MINIBAR_PORT", "8000")))
    parser.add_argument("--reset", action="store_true", help="删除并重建演示数据库")
    parser.add_argument("--no-reminders", action="store_true", help="不启动进程内提醒任务")
    parser.add_argument("--reminder-interval", type=int, default=60)
    args = parser.parse_args(argv)

    if args.reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()

    # 库为空时自动载入在住 / 待退 / 争议样例
    from .db import connect
    conn = connect()
    empty = conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"] == 0
    conn.close()
    if empty:
        from .seed import seed
        seed(reset=False)
        print("已自动载入演示样例。")

    reminders = None
    if not args.no_reminders:
        reminders = InProcessReminder(connect, interval=args.reminder_interval)
        # 启动即扫描一次，再周期扫描
        reminders.scan_once()
        reminders.start()

    httpd = make_server(args.host, args.port)
    print(f"迷你吧平台已启动： http://{args.host}:{args.port}")
    print(f"数据库 {config.DB_PATH}　照片目录 {config.PHOTO_DIR}　当前演示时间 {clock.iso()}")
    print("演示账号：M01 经理(全楼层) / F01 前台(全楼层) / F02 前台(2F) / A01 服务员(2F) / A02 服务员(3F)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        if reminders:
            reminders.stop()
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
