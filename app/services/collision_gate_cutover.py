"""撞键闸切流账册（甲路）：钉定物化包即当刻指纹与镜像次数，读侧只认物化包。"""
from __future__ import annotations

from app.db import get_db
from app.services import collision_gate as gate

_SLUG = "collision_gate"
_TITLE = "撞键闸"


def append_cutover(
    ref_id: int, note: str, expect_pin_id: int | None = None, db_path: str | None = None
) -> dict:
    """切流账册落笔等同钉定：提交即写完落库，不再只记软笔记。"""
    payload = {"ref_id": ref_id, "note": note or f"{_TITLE}切流"}
    if expect_pin_id is not None:
        payload["expect_pin_id"] = expect_pin_id
    return gate.commit_action(payload, db_path)


def cutover_rows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM collision_gate_pins ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def board(db_path: str | None = None) -> dict:
    dash = gate.dashboard(db_path)
    dash["cutover"] = cutover_rows(db_path)
    dash["fate"] = gate._CUTOVER
    return dash
