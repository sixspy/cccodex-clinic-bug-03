"""撞键闸读轨审计（甲路）：汇总物化包落点行与对账页读轨块。

乙路双写两轨脚手架已按择边拆除，此处只审计唯一读轨 cutover_book。
"""
from __future__ import annotations

from app.db import get_db

from app.services import collision_gate as gate
from app.services.collision_gate_cutover import board as cutover_board


def audit_list(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM collision_gate_pins ORDER BY id
            """
        ).fetchall()
        return [dict(r) for r in rows]


def audit_summary(db_path: str | None = None) -> dict:
    return {
        "title": "撞键闸审计",
        "fate": gate._CUTOVER,
        "pins": audit_list(db_path),
        "cutover": cutover_board(db_path),
        "reconcile_block": gate.reconcile_block(db_path),
    }
