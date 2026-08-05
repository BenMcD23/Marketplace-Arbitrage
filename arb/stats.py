"""Reporting: read the deals table and summarise performance.

Feeds both `arb stats` on the command line and the dashboard's overview cards,
so the two can never disagree about what the numbers mean.
"""

from __future__ import annotations

from arb.db import Database


def compute_stats(db: Database, days: int = 30) -> dict:
    since = f"-{days} days"

    total = db.query(
        "SELECT COUNT(*) AS n, AVG(roi_pct) AS avg_roi, AVG(est_profit) AS avg_profit, "
        "       AVG(expected_profit) AS avg_expected, SUM(expected_profit) AS sum_expected, "
        "       AVG(confidence) AS avg_confidence, AVG(score) AS avg_score "
        "FROM deals WHERE is_scam_flag = 0 AND flagged_at >= datetime('now', ?)",
        (since,),
    )[0]

    per_day = db.query(
        "SELECT date(flagged_at) AS d, COUNT(*) AS n, SUM(expected_profit) AS profit "
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

    # Data-health counters: how much of its own sold history the system has built.
    coverage = db.query(
        "SELECT "
        " (SELECT COUNT(*) FROM sold_observations) AS sold_observations,"
        " (SELECT COUNT(DISTINCT product_key) FROM sold_observations) AS sold_keys,"
        " (SELECT COUNT(*) FROM comp_watch WHERE resolved = 0) AS comps_watched,"
        " (SELECT COUNT(*) FROM valuations) AS valuations,"
        " (SELECT COUNT(*) FROM valuations WHERE basis = 'sold') AS valuations_from_sold,"
        " (SELECT COUNT(*) FROM listings) AS listings"
    )[0]

    return {
        "window_days": days,
        "total_deals": total["n"] or 0,
        "avg_roi": _round(total["avg_roi"], 1),
        "avg_profit": _round(total["avg_profit"], 2),
        "avg_expected_profit": _round(total["avg_expected"], 2),
        "total_expected_profit": _round(total["sum_expected"], 2),
        "avg_confidence": _round(total["avg_confidence"], 3),
        "avg_score": _round(total["avg_score"], 1),
        "scam_flags": scams["n"] or 0,
        "per_day": [
            {"date": r["d"], "deals": r["n"], "expected_profit": _round(r["profit"], 2)}
            for r in per_day
        ],
        "per_source": [
            {"source": r["source"], "deals": r["deals"], "avg_roi": _round(r["avg_roi"], 1)}
            for r in per_source
        ],
        "per_channel": [
            {
                "channel": r["channel"],
                "deals": r["deals"],
                "avg_profit": _round(r["avg_profit"], 2),
            }
            for r in per_channel
        ],
        "data_health": {
            "sold_observations": coverage["sold_observations"],
            "sold_product_keys": coverage["sold_keys"],
            "comps_watched": coverage["comps_watched"],
            "valuations": coverage["valuations"],
            "valuations_from_sold": coverage["valuations_from_sold"],
            "listings": coverage["listings"],
        },
    }


def _round(value, places: int):
    return round(value, places) if value is not None else None


def format_stats(stats: dict) -> str:
    def fmt(label: str, value, prefix: str = "") -> str:
        shown = "n/a" if value is None else f"{prefix}{value}"
        return f"{label:<22}: {shown}"

    lines = [
        f"=== Arbitrage stats (last {stats['window_days']} days) ===",
        fmt("Total deals flagged", stats["total_deals"]),
        fmt("Avg ROI", stats["avg_roi"], ""),
        fmt("Avg profit", stats["avg_profit"], "£"),
        fmt("Avg expected profit", stats["avg_expected_profit"], "£"),
        fmt("Avg confidence", stats["avg_confidence"]),
        fmt("Avg score", stats["avg_score"]),
        fmt("Scam flags", stats["scam_flags"]),
        "",
        "Data health:",
    ]
    health = stats["data_health"]
    lines += [
        f"  observed sales      : {health['sold_observations']} "
        f"across {health['sold_product_keys']} products",
        f"  comps being watched : {health['comps_watched']}",
        f"  valuations          : {health['valuations']} "
        f"({health['valuations_from_sold']} from observed sales)",
    ]
    lines += ["", "By source:"]
    for r in stats["per_source"]:
        lines.append(f"  {r['source']:<16} {r['deals']:>4} deals   avg ROI {r['avg_roi']}%")
    lines += ["", "By sell channel:"]
    for r in stats["per_channel"]:
        lines.append(f"  {r['channel']:<16} {r['deals']:>4} deals   avg profit £{r['avg_profit']}")
    lines += ["", "Deals per day:"]
    for r in stats["per_day"]:
        lines.append(f"  {r['date']}   {r['deals']:>3}   £{r['expected_profit']}")
    return "\n".join(lines)
