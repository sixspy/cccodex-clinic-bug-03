"""撞键闸假绿回归：落点入业务库、读轨切换、指纹校验、镜像件拦截与争用。"""
from __future__ import annotations

import importlib
import threading

import pytest

from app.db import get_db
from app.services import backfill, collision_gate as gate, reconcile, visits
from app.services.errors import ServiceError


def _pin_rows(db_path):
    with get_db(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM collision_gate_pins ORDER BY id"
            ).fetchall()
        ]


def _first_batch_id(db_path):
    return visits.list_batches(db_path)[0]["id"]


def test_initial_state_is_false_green(db_path):
    snap = gate.snapshot(db_path)
    assert snap["panel_chip"] == gate._OK
    assert snap["truth"] == gate._BAD
    assert snap["aligned"] is False
    assert snap["active_read"] == "old_track"
    assert snap["landing_row"] is None
    assert _pin_rows(db_path) == []
    assert gate.gate_allows_pipeline(db_path) is False


def test_snapshot_does_not_reseed_skew(db_path):
    gate.commit_action({"ref_id": 1, "note": "钉定"}, db_path)
    gate.snapshot(db_path)
    snap = gate.snapshot(db_path)
    assert snap["aligned"] is True
    assert snap["active_read"] == "cutover_book"
    assert len(_pin_rows(db_path)) == 1


def test_commit_writes_landing_row_and_switches_read(db_path):
    result = gate.commit_action({"ref_id": 7, "note": "钉定撞键闸"}, db_path)
    assert result["ok"] is True
    assert result["aligned"] is True
    pins = _pin_rows(db_path)
    assert len(pins) == 1
    assert pins[0]["fingerprint"] == result["cutover_fp"]
    assert pins[0]["mode"] == "cutover_book"
    assert pins[0]["ref_id"] == 7
    assert pins[0]["mirror_count"] == result["mirror_count"]

    snap = gate.snapshot(db_path)
    assert snap["aligned"] is True
    assert snap["active_read"] == "cutover_book"
    assert snap["reject_refresh"] is True
    assert snap["landing_row"] == pins[0]["id"]
    assert snap["mirror_blocked"] is False
    assert gate.gate_allows_pipeline(db_path) is True
    # 旧镜像件残留必须随钉定清理
    assert snap["decoy_count"] == 0


def test_chip_alone_without_landing_row_is_rejected(db_path):
    gate.paint_ok(db_path)
    snap = gate.snapshot(db_path)
    assert snap["panel_chip"] == gate._OK
    assert snap["truth"] == gate._BAD
    assert snap["aligned"] is False
    assert snap["landing_row"] is None
    assert snap["mirror_blocked"] is True
    with pytest.raises(ServiceError):
        gate.require_pack_or_raise(db_path)


def test_tampered_fingerprint_still_blocks(db_path):
    gate.commit_action({"ref_id": 1}, db_path)
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE meta SET value='tampered' WHERE key='collision_gate_cutover_fp'"
        )
    snap = gate.snapshot(db_path)
    assert snap["aligned"] is False
    assert snap["truth"] == gate._BAD
    assert gate.gate_allows_pipeline(db_path) is False


def test_mirror_count_drift_blocks_mirror_items(db_path):
    gate.commit_action({"ref_id": 1}, db_path)
    batch_id = _first_batch_id(db_path)
    site_id = visits.get_batch(batch_id, db_path)["site_id"]
    other = next(
        b
        for b in visits.list_batches(db_path)
        if b["site_id"] == site_id and b["id"] != batch_id
    )
    with get_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO visit_rows(batch_id, subject_key, visit_code, payload, quality, status)
            VALUES (?, 'MIRROR-9001', 'V9', '{}', '合格', '待检')
            """,
            (batch_id,),
        )
        origin_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO visit_rows(batch_id, subject_key, visit_code, payload, quality, status)
            VALUES (?, 'MIRROR-9001', 'V9', '{}', '合格', '待检')
            """,
            (other["id"],),
        )
        mirror_row_id = cur.lastrowid
    assert origin_id != mirror_row_id
    snap = gate.snapshot(db_path)
    assert snap["mirror_count"] == snap["pinned_mirror_count"] + 1
    assert snap["aligned"] is False
    assert snap["mirror_blocked"] is True
    assert gate.gate_allows_pipeline(db_path) is False
    with pytest.raises(ServiceError):
        backfill.enqueue_from_row(mirror_row_id, db_path=db_path)


def test_origin_row_still_enqueues_while_mirror_blocked(db_path):
    rows = visits.list_rows(db_path=db_path)
    pair = sorted(
        (r for r in rows if r["subject_key"] == "S01-0003"), key=lambda r: r["id"]
    )
    origin, mirror = pair[0], pair[1]
    assert origin["id"] < mirror["id"]
    # 原件先落库，仍可补采；后落库镜像件被拦
    job = backfill.enqueue_from_row(origin["id"], db_path=db_path)
    assert job["status"] == "排队"
    with pytest.raises(ServiceError):
        backfill.enqueue_from_row(mirror["id"], db_path=db_path)


def test_concurrent_commit_has_single_winner(db_path):
    results: list[tuple[str, object]] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            out = gate.commit_action({"ref_id": 1, "expect_pin_id": 0}, db_path)
            results.append(("ok", out))
        except ServiceError as exc:  # 争用失败须讲明白
            results.append(("err", exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    errors = [v for kind, v in results if kind == "err"]
    assert errors and "争用" in str(errors[0])
    assert len(_pin_rows(db_path)) == 1


def test_batch_enqueue_does_not_falsely_pin_gate(db_path):
    batch_id = _first_batch_id(db_path)
    backfill.enqueue_gaps_for_batch(batch_id, db_path=db_path)
    snap = gate.snapshot(db_path)
    assert snap["aligned"] is False
    assert _pin_rows(db_path) == []


def test_quota_update_does_not_falsely_pin_gate(db_path):
    from app.services import sites

    site = sites.list_sites(db_path)[0]
    sites.update_quota(site["id"], 77, db_path=db_path)
    assert gate.snapshot(db_path)["aligned"] is False
    assert _pin_rows(db_path) == []


def test_reconcile_dashboard_carries_gate_landing_row(db_path):
    dash = reconcile.dashboard(db_path)
    block = dash["collision_gate"]
    for key in (
        "chip",
        "truth",
        "mirror_count",
        "pinned_mirror_count",
        "pin_row",
        "landing_row",
        "aligned",
        "mirror_blocked",
        "active_read",
        "fingerprint",
        "reject_refresh",
    ):
        assert key in block
    assert block["pin_row"] is None
    gate.commit_action({"ref_id": 3, "note": "对账页钉定"}, db_path)
    block = reconcile.dashboard(db_path)["collision_gate"]
    assert block["pin_row"]["id"] >= 1
    assert block["landing_row"] == block["pin_row"]["id"]
    assert block["aligned"] is True


def test_reconcile_report_does_not_count_mirror_as_gap(db_path):
    report = reconcile.run_reconcile(db_path)
    detail = report["detail"]
    assert detail["mirror_rows"] >= 1
    assert detail["mirror_rows_excluded"] is True
    # 镜像件不得被当成真实缺口行
    assert report["gap_rows"] == detail["real_gap_rows"]


def test_dualwrite_branch_scaffolding_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.collision_gate_dualwrite")
