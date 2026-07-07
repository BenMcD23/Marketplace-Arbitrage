"""Reporting: read the deals table and summarise performance."""

from __future__ import annotations

from arb.db import Database


def compute_stats(db: Database, days: int = 30) -> dict:
    since = f"-{days} days"

    total = db.query(
        "SELECT COUNT(*) AS n, AVG(roi_pct) AS avg_roi, AVG(est_profit) AS avg_profit "
        "FROM deals WHERE is_scam_flag = 0 AND flagged_at >= datetime('now', ?)",
        (since,),
    )[0]

    per_day = db.query(
        "SELECT date(flagged_at) AS d, COUNT(*) AS n "
        "FROM deals WHERE is_scam_flag = 0 AND flagged_at >= datetime('now', ?) "
        "GROUP BY d ORDER BY d DESC",
        (since,),
    )

    per_source = db.query(
        "SELECT l.source AS source, COUNT(*) AS deals, AVG(d.roi_pct) AS avg_roi "
        "FROM deals d JOIN listings l ON l.id = d.listing_id "
        "WHERE d.is_scam_flag = 0 AND d.flagged_at >= datetime('now', ?) "
        "GROUP BY l.source ORDER BY deals DESC",
        (since,),
    )

    per_channel = db.query(
        "SELECT sell_channel AS channel, COUNT(*) AS deals, AVG(est_profit) AS avg_profit "
        "FROM deals WHERE is_scam_flag = 0 AND flagged_at >= datetime('now', ?) "
        "GROUP BY sell_channel",
        (since,),
    )

    scams = db.query(
        "SELECT COUNT(*) AS n FROM deals WHERE is_scam_flag = 1 AND flagged_at >= datetime('now', ?)",
        (since,),
    )[0]

    return {
        "window_days": days,
        "total_deals": total["n"] or 0,
        "avg_roi": round(total["avg_roi"], 1) if total["avg_roi"] is not None else None,
        "avg_profit": round(total["avg_profit"], 2) if total["avg_profit"] is not None else None,
        "scam_flags": scams["n"] or 0,
        "per_day": [{"date": r["d"], "deals": r["n"]} for r in per_day],
        "per_source": [
            {"source": r["source"], "deals": r["deals"], "avg_roi": round(r["avg_roi"], 1) if r["avg_roi"] else None}
            for r in per_source
        ],
        "per_channel": [
            {"channel": r["channel"], "deals": r["deals"], "avg_profit": round(r["avg_profit"], 2) if r["avg_profit"] else None}
            for r in per_channel
        ],
    }


def format_stats(stats: dict) -> str:
    lines = [
        f"=== Arbitrage stats (last {stats['window_days']} days) ===",
        f"Total deals flagged : {stats['total_deals']}",
        f"Avg ROI             : {stats['avg_roi']}%" if stats["avg_roi"] is not None else "Avg ROI             : n/a",
        f"Avg profit          : £{stats['avg_profit']}" if stats["avg_profit"] is not None else "Avg profit          : n/a",
        f"Scam flags          : {stats['scam_flags']}",
        "",
        "By source:",
    ]
    for r in stats["per_source"]:
        lines.append(f"  {r['source']:<16} {r['deals']:>4} deals   avg ROI {r['avg_roi']}%")
    lines.append("")
    lines.append("By sell channel:")
    for r in stats["per_channel"]:
        lines.append(f"  {r['channel']:<16} {r['deals']:>4} deals   avg profit £{r['avg_profit']}")
    lines.append("")
    lines.append("Deals per day:")
    for r in stats["per_day"]:
        lines.append(f"  {r['date']}   {r['deals']}")
    return "\n".join(lines)
