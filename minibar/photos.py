"""包装照片：落本地文件目录（PHOTO_DIR），元数据入库。"""
import re
from pathlib import Path

from . import config
from .clock import clock
from .errors import require_role

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def save_photo(conn, upload, staff, batch_id=None, room_id=None, note=""):
    """upload 为 (filename, bytes)。返回 photo 记录 id。"""
    require_role(staff, "attendant", "manager")
    filename, data = upload
    safe = _SAFE.sub("_", filename)
    stem = Path(safe).stem or "photo"
    final_name = f"{clock.now().strftime('%Y%m%d%H%M%S')}-{staff['code']}-{stem}"
    if Path(safe).suffix:
        final_name += Path(safe).suffix
    else:
        final_name += ".jpg"
    path = config.PHOTO_DIR / final_name
    path.write_bytes(data)
    cur = conn.execute(
        "INSERT INTO photo (filename, batch_id, room_id, uploaded_by, created_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (final_name, batch_id, room_id, staff["id"], clock.iso(), note))
    if batch_id:
        # 上传破损照片即标记批次破损，禁止再上架
        conn.execute("UPDATE batch SET damaged=1 WHERE id=?", (batch_id,))
    conn.commit()
    return cur.lastrowid, final_name


def list_photos(conn):
    return [dict(r) for r in conn.execute(
        "SELECT p.*, s.name AS staff_name FROM photo p LEFT JOIN staff s ON s.id=p.uploaded_by "
        "ORDER BY p.id DESC").fetchall()]
