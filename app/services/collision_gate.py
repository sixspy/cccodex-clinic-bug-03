"""撞键钉定（甲路·物化包）。

择定甲路：撞键闸钉定写入业务库物化包 ``collision_gate_pins``，当刻指纹与镜像次数
一并落行；读侧只认最新物化包，指纹被拧花或镜像次数漂移即判假绿；旧读轨回刷一律拒绝。
乙路（双写两轨）脚手架已拆除，读轨不并存。

镜像件口径：同一中心下同受试者 + 同访视编码出现多行时，先落库者为原件，
其后各行为镜像件（成对镜像件）。镜像件不得进入补采队列，也不得被对账计成真实缺口。
"""
from __future__ import annotations

import hashlib
import time

from app.db import _meta_get, _meta_set, get_db
from app.services.errors import ServiceError

_SLUG = "collision_gate"
_OK = "撞键已亮"
_BAD = "镜像次数旧"
_TITLE = "撞键闸"
_CUTOVER = "cutover_book"
_OLD = "old_track"

# r 为 visit_rows 别名；判断 r 是否为「后半件」镜像件（同中心同键已有更早落库行）。
MIRROR_COPY_SQL = """
EXISTS (
  SELECT 1
  FROM visit_rows m
  JOIN visit_batches mb ON mb.id = m.batch_id
  JOIN visit_batches rb ON rb.id = r.batch_id
  WHERE mb.site_id = rb.site_id
    AND m.subject_key = r.subject_key
    AND m.visit_code = r.visit_code
    AND m.id < r.id
)
"""


def _latest_pin(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM collision_gate_pins ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _pin_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM collision_gate_pins ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def mirror_rows(conn) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM visit_rows r WHERE {MIRROR_COPY_SQL}"
    ).fetchone()["c"]


def _row_is_mirror_copy(conn, row) -> bool:
    if not row:
        return False
    batch = conn.execute(
        "SELECT site_id FROM visit_batches WHERE id=?", (row["batch_id"],)
    ).fetchone()
    if not batch:
        return False
    twin = conn.execute(
        """
        SELECT 1 FROM visit_rows m
        JOIN visit_batches b ON b.id = m.batch_id
        WHERE b.site_id=? AND m.subject_key=? AND m.visit_code=? AND m.id<?
        LIMIT 1
        """,
        (batch["site_id"], row["subject_key"], row["visit_code"], row["id"]),
    ).fetchone()
    return twin is not None


def assert_backfill_allowed(conn, row) -> None:
    if _row_is_mirror_copy(conn, row):
        raise ServiceError(
            f"{_TITLE}未收镜像件：该行是后落库镜像件，不得进入补采队列",
            409,
        )


def assert_ingest_allowed(conn, row) -> None:
    if _row_is_mirror_copy(conn, row):
        raise ServiceError(
            f"{_TITLE}未收镜像件：镜像件不得入库，请先对账收口",
            409,
        )


def panel_chip(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return _meta_get(conn, f"{_SLUG}_panel_chip", _OK)


def truth_flag(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return _OK if _evaluate(conn)["aligned"] else _BAD


def active_read_ref(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return _meta_get(conn, f"{_SLUG}_active_read", _OLD)


def cutover_fp(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return _meta_get(conn, f"{_SLUG}_cutover_fp", "")


def reject_refresh(db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        return _meta_get(conn, f"{_SLUG}_reject_refresh", "0") == "1"


def _evaluate(conn) -> dict:
    """读轨判定：只认物化包，不再看旁路开关。"""
    pin = _latest_pin(conn)
    chip = _meta_get(conn, f"{_SLUG}_panel_chip", _OK)
    ref = _meta_get(conn, f"{_SLUG}_active_read", _OLD)
    meta_fp = _meta_get(conn, f"{_SLUG}_cutover_fp", "")
    reject = _meta_get(conn, f"{_SLUG}_reject_refresh", "0") == "1"
    current_mirrors = mirror_rows(conn)
    pinned_mirrors = pin["mirror_count"] if pin else 0
    fp_ok = (
        bool(pin)
        and pin["fingerprint"] == meta_fp
        and meta_fp not in ("", "stale-fp")
    )
    mirror_stable = bool(pin) and current_mirrors == pinned_mirrors
    aligned = (
        bool(pin)
        and chip == _OK
        and ref == _CUTOVER
        and reject
        and fp_ok
        and mirror_stable
    )
    return {
        "pin": pin,
        "chip": chip,
        "active_read": ref,
        "fingerprint": pin["fingerprint"] if pin else "",
        "meta_fingerprint": meta_fp,
        "reject_refresh": reject,
        "mirror_count": current_mirrors,
        "pinned_mirror_count": pinned_mirrors,
        "landing_row": pin["id"] if pin else None,
        "fingerprint_ok": fp_ok,
        "mirror_stable": mirror_stable,
        "aligned": aligned,
        "truth": _OK if aligned else _BAD,
        "mode": pin["mode"] if pin else "",
        "mirror_blocked": not aligned,
    }


def seed_skew(db_path: str | None = None) -> None:
    """初始假绿底样：徽标亮、读轨仍旧。已有钉点时不动已收口读轨。"""
    with get_db(db_path) as conn:
        if _latest_pin(conn):
            return
        _meta_set(conn, f"{_SLUG}_panel_chip", _OK)
        _meta_set(conn, f"{_SLUG}_truth", _BAD)
        _meta_set(conn, f"{_SLUG}_counter", "0")
        _meta_set(conn, f"{_SLUG}_active_read", _OLD)
        _meta_set(conn, f"{_SLUG}_cutover_fp", "")
        _meta_set(conn, f"{_SLUG}_reject_refresh", "0")
        _meta_set(conn, f"{_SLUG}_mode", "")


def paint_ok(db_path: str | None = None) -> str:
    """只点徽标、不落钉点行；读轨判定会将其驳回为假绿。"""
    with get_db(db_path) as conn:
        _meta_set(conn, f"{_SLUG}_panel_chip", _OK)
    return _OK


def commit_action(payload: dict | None = None, db_path: str | None = None) -> dict:
    """撞键钉定：当刻物化包落行 + 读轨切到 cutover_book，提交即刻写完落库。"""
    payload = payload or {}
    ref_id = int(payload.get("ref_id") or payload.get("ref") or 0)
    note = str(payload.get("note") or _TITLE)[:200]
    expect = payload.get("expect_pin_id")

    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pins = _pin_rows(conn)
        current_pin_id = pins[-1]["id"] if pins else 0
        if expect is not None and int(expect) != current_pin_id:
            raise ServiceError(
                f"{_TITLE}争用：钉点已被并发写入抢占，本次钉定失败", 409
            )
        counter = int(_meta_get(conn, f"{_SLUG}_counter", "0") or "0") + 1
        mirrors = mirror_rows(conn)
        fingerprint = hashlib.sha256(
            f"{ref_id}|{counter}|{mirrors}|{note}|{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:24]
        cur = conn.execute(
            """
            INSERT INTO collision_gate_pins(
              mode, fingerprint, mirror_count, ref_id, note
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_CUTOVER, fingerprint, mirrors, ref_id, note),
        )
        pin_id = cur.lastrowid
        _meta_set(conn, f"{_SLUG}_counter", str(counter))
        _meta_set(conn, f"{_SLUG}_panel_chip", _OK)
        _meta_set(conn, f"{_SLUG}_truth", _OK)
        _meta_set(conn, f"{_SLUG}_active_read", _CUTOVER)
        _meta_set(conn, f"{_SLUG}_cutover_fp", fingerprint)
        _meta_set(conn, f"{_SLUG}_reject_refresh", "1")
        _meta_set(conn, f"{_SLUG}_mode", _CUTOVER)
        # 旧镜像件/旁路包一律清理，避免残件继续被读轨读到
        conn.execute(
            "DELETE FROM meta WHERE key LIKE ?", (f"{_SLUG}_pack_%",)
        )
        conn.execute(
            "DELETE FROM meta WHERE key LIKE ?", (f"{_SLUG}_pack_decoy%",)
        )

    from app.services import audit as audit_svc

    audit_svc.write_event(
        "撞键闸钉定",
        "collision_gate_pin",
        pin_id,
        f"ref={ref_id} fp={fingerprint} mirrors={mirrors}",
        db_path=db_path,
    )
    return {
        "ok": True,
        "title": _TITLE,
        "chip": _OK,
        "truth": _OK,
        "counter": counter,
        "mode": _CUTOVER,
        "landing_row": pin_id,
        "cutover_fp": fingerprint,
        "fingerprint": fingerprint,
        "active_read": _CUTOVER,
        "reject_refresh": True,
        "mirror_count": mirrors,
        "pinned_mirror_count": mirrors,
        "aligned": True,
        "message": f"{_TITLE}已钉定落点行 #{pin_id}",
    }


def refresh_old_track(db_path: str | None = None) -> dict:
    """回刷旧读轨：钉定后一律拒绝，防止假绿回潮。"""
    with get_db(db_path) as conn:
        state = _evaluate(conn)
        if state["reject_refresh"]:
            raise ServiceError(f"{_TITLE}拒绝回刷：读轨已钉到物化包", 409)
        _meta_set(conn, f"{_SLUG}_active_read", _OLD)
    return snapshot(db_path)


def snapshot(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        state = _evaluate(conn)
        decoys = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE ?",
            (f"{_SLUG}_pack_%",),
        ).fetchone()["c"]
        pinned = _pin_rows(conn)
    return {
        "title": _TITLE,
        "panel_chip": state["chip"],
        "truth": state["truth"],
        "active_read": state["active_read"],
        "cutover_fp": state["meta_fingerprint"],
        "fingerprint": state["fingerprint"],
        "reject_refresh": state["reject_refresh"],
        "mirror_count": state["mirror_count"],
        "pinned_mirror_count": state["pinned_mirror_count"],
        "landing_row": state["landing_row"],
        "pin_count": len(pinned),
        "decoy_count": decoys,
        "mode": state["mode"],
        "aligned": state["aligned"],
        "mirror_blocked": state["mirror_blocked"],
        "packs": pinned,
    }


def dashboard(db_path: str | None = None) -> dict:
    snap = snapshot(db_path)
    return {
        **snap,
        "chip": snap["panel_chip"],
        "pin_rows": snap["packs"],
        "panel_pack_mismatch": snap["panel_chip"] == _OK and not snap["aligned"],
        "ok_chip": _OK,
        "bad_chip": _BAD,
        "label": "撞键钉定",
        "read_tracks": {_CUTOVER: _CUTOVER},
    }


def reconcile_block(db_path: str | None = None) -> dict:
    """对账页撞键闸读轨块：徽标对照镜像次数与落点行。"""
    snap = snapshot(db_path)
    pin = snap["packs"][-1] if snap["packs"] else None
    return {
        "title": _TITLE,
        "chip": snap["panel_chip"],
        "truth": snap["truth"],
        "mirror_count": snap["mirror_count"],
        "pinned_mirror_count": snap["pinned_mirror_count"],
        "pin_row": pin,
        "landing_row": snap["landing_row"],
        "pin_count": snap["pin_count"],
        "aligned": snap["aligned"],
        "mirror_blocked": snap["mirror_blocked"],
        "active_read": snap["active_read"],
        "fingerprint": snap["fingerprint"],
        "cutover_fp": snap["cutover_fp"],
        "reject_refresh": snap["reject_refresh"],
        "mode": snap["mode"],
        "panel_pack_mismatch": snap["panel_chip"] == _OK and not snap["aligned"],
    }


def gate_allows_pipeline(db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        return _evaluate(conn)["aligned"]


def require_pack_or_raise(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        if not _evaluate(conn)["aligned"]:
            raise ServiceError(f"{_TITLE}读轨未对齐，禁止放行", 409)
