from __future__ import annotations

from app.db import get_db
from app.services import audit as audit_svc
from app.services import collision_gate as gate
from app.services.errors import ServiceError

# 真实缺口行：排除同中心同键后半件镜像；镜像件不得把缺口与发现数顶虚。
_REAL_GAP_WHERE = """
  r.quality!='合格' AND r.status IN ('待检','待补采')
  AND NOT {mirror}
""".format(mirror=gate.MIRROR_COPY_SQL)


def dashboard(db_path: str | None = None) -> dict:
    from app.services.collision_gate_audit import audit_summary as _collision_gate_audit
    with get_db(db_path) as conn:
        sites = conn.execute("SELECT COUNT(*) AS c FROM trial_sites").fetchone()["c"]
        sites_enabled = conn.execute(
            "SELECT COUNT(*) AS c FROM trial_sites WHERE status='启用'"
        ).fetchone()["c"]
        protocols = conn.execute("SELECT COUNT(*) AS c FROM protocols").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM visit_batches").fetchone()["c"]
        rows = conn.execute("SELECT COUNT(*) AS c FROM visit_rows").fetchone()["c"]
        gaps = conn.execute(
            f"SELECT COUNT(*) AS c FROM visit_rows r WHERE {_REAL_GAP_WHERE}"
        ).fetchone()["c"]
        findings = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM qc_findings f
            JOIN visit_rows r ON r.id=f.row_id
            WHERE f.status='开放' AND NOT {gate.MIRROR_COPY_SQL}
            """
        ).fetchone()["c"]
        jobs = conn.execute(
            """
            SELECT COUNT(*) AS c FROM backfill_jobs
            WHERE status IN ('排队','执行中')
            """
        ).fetchone()["c"]
        audits = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
        by_batch_status = conn.execute(
            """
            SELECT status, COUNT(*) AS c FROM visit_batches GROUP BY status
            """
        ).fetchall()
        return {
            "sites": sites,
            "sites_enabled": sites_enabled,
            "protocols": protocols,
            "batches": batches,
            "rows": rows,
            "gaps": gaps,
            "open_findings": findings,
            "pending_jobs": jobs,
            "audits": audits,
            "batch_status": {r["status"]: r["c"] for r in by_batch_status},
            "collision_gate": gate.reconcile_block(db_path),
            "collision_gate_audit": _collision_gate_audit(db_path),
        }


def run_reconcile(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        gate_state = gate.reconcile_block(db_path)
        mirror_rows = conn.execute(
            f"SELECT COUNT(*) AS c FROM visit_rows r WHERE {gate.MIRROR_COPY_SQL}"
        ).fetchone()["c"]
        real_gap_rows = conn.execute(
            f"SELECT COUNT(*) AS c FROM visit_rows r WHERE {_REAL_GAP_WHERE}"
        ).fetchone()["c"]
        gap_rows_naive = conn.execute(
            """
            SELECT COUNT(*) AS c FROM visit_rows
            WHERE quality!='合格' AND status IN ('待检','待补采')
            """
        ).fetchone()["c"]
        open_findings_naive = conn.execute(
            "SELECT COUNT(*) AS c FROM qc_findings WHERE status='开放'"
        ).fetchone()["c"]
        mirror_findings = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM qc_findings f
            JOIN visit_rows r ON r.id=f.row_id
            WHERE f.status='开放' AND {gate.MIRROR_COPY_SQL}
            """
        ).fetchone()["c"]
        mirror_gap_rows = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM visit_rows r
            WHERE r.quality!='合格' AND r.status IN ('待检','待补采')
              AND {gate.MIRROR_COPY_SQL}
            """
        ).fetchone()["c"]
        open_findings = open_findings_naive - mirror_findings
        gap_rows = real_gap_rows
        pending_jobs = conn.execute(
            """
            SELECT COUNT(*) AS c FROM backfill_jobs
            WHERE status IN ('排队','执行中')
            """
        ).fetchone()["c"]
        # rows needing backfill but without open finding
        orphan_gaps = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM visit_rows r
            WHERE {_REAL_GAP_WHERE}
              AND NOT EXISTS (
                SELECT 1 FROM qc_findings f
                WHERE f.row_id=r.id AND f.status='开放'
              )
            """
        ).fetchone()["c"]
        # open findings whose row already qualified
        stale_findings = conn.execute(
            """
            SELECT COUNT(*) AS c FROM qc_findings f
            JOIN visit_rows r ON r.id=f.row_id
            WHERE f.status='开放' AND r.quality='合格'
            """
        ).fetchone()["c"]
        # gap rows without pending job
        uncovered = conn.execute(
            """
            SELECT COUNT(*) AS c FROM visit_rows r
            WHERE r.quality!='合格' AND r.status='待补采'
              AND NOT EXISTS (
                SELECT 1 FROM backfill_jobs j
                WHERE j.row_id=r.id AND j.status IN ('排队','执行中','成功')
              )
            """
        ).fetchone()["c"]
        mismatch = orphan_gaps + stale_findings + uncovered
        summary = (
            f"缺口行{gap_rows} · 开放发现{open_findings} · 待执行补采{pending_jobs} · "
            f"无发现缺口{orphan_gaps} · 陈旧发现{stale_findings} · 未覆盖补采{uncovered} · "
            f"镜像件{mirror_rows}"
        )
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              summary, gap_rows, open_findings, pending_jobs, mismatch
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (summary, gap_rows, open_findings, pending_jobs, mismatch),
        )
        rid = cur.lastrowid
        report = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (rid,)
        ).fetchone()
        detail = {
            "orphan_gaps": orphan_gaps,
            "stale_findings": stale_findings,
            "uncovered_backfill": uncovered,
            "real_gap_rows": real_gap_rows,
            "gap_rows_naive": gap_rows_naive,
            "mirror_rows": mirror_rows,
            "mirror_gap_rows": mirror_gap_rows,
            "mirror_rows_excluded": gap_rows + mirror_gap_rows == gap_rows_naive,
            "mirror_findings": mirror_findings,
            "open_findings_naive": open_findings_naive,
            "collision_gate": gate_state,
            "gap_by_quality": [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT r.quality, COUNT(*) AS c FROM visit_rows r
                    WHERE {_REAL_GAP_WHERE}
                    GROUP BY r.quality
                    """
                ).fetchall()
            ],
            "jobs_by_status": [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT status, COUNT(*) AS c FROM backfill_jobs
                    GROUP BY status
                    """
                ).fetchall()
            ],
        }
    audit_svc.write_event(
        "访视对账", "reconcile_report", rid, summary, db_path=db_path
    )
    data = dict(report)
    data["detail"] = detail
    return data


def list_reports(db_path: str | None = None, limit: int = 20) -> list[dict]:
    limit = max(1, min(100, int(limit or 20)))
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM reconcile_reports
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_report(report_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            raise ServiceError("对账报告不存在", 404)
        return dict(row)


def site_gap_matrix(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.site_code, s.name, s.status,
              (SELECT COUNT(*) FROM visit_batches b WHERE b.site_id=s.id) AS batch_count,
              (SELECT COUNT(*) FROM visit_rows r
                JOIN visit_batches b ON b.id=r.batch_id
                WHERE b.site_id=s.id) AS row_count,
              (SELECT COUNT(*) FROM visit_rows r
                JOIN visit_batches b ON b.id=r.batch_id
                WHERE b.site_id=s.id
                  AND r.quality!='合格' AND r.status IN ('待检','待补采')
                  AND NOT {gate.MIRROR_COPY_SQL}) AS gap_count,
              (SELECT COUNT(*) FROM qc_findings f
                JOIN visit_rows r ON r.id=f.row_id
                JOIN visit_batches b ON b.id=r.batch_id
                WHERE b.site_id=s.id AND f.status='开放'
                  AND NOT {gate.MIRROR_COPY_SQL}) AS open_findings,
              (SELECT COUNT(*) FROM visit_rows r
                JOIN visit_batches b ON b.id=r.batch_id
                WHERE b.site_id=s.id AND {gate.MIRROR_COPY_SQL}) AS mirror_count
            FROM trial_sites s
            ORDER BY s.id
            """
        ).fetchall()
        return [dict(r) for r in rows]
