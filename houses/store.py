"""Auction lots, kept in their own table in the main SQLite file.

Closed lots are the valuable rows: a hammer price is what the lot actually cost
someone, which is the number the whole "is there money in it" question turns on.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from houses.simon_charles import Lot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auction_lots (
    house TEXT NOT NULL,
    lot_id INTEGER NOT NULL,
    auction_id INTEGER,
    title TEXT NOT NULL,
    hammer REAL,
    next_bid REAL,
    end_time TEXT,
    status INTEGER,
    sold INTEGER,
    postal INTEGER,
    vat_pct REAL,
    premium_pct REAL,
    internet_pct REAL,
    description TEXT,
    condition_report TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (house, lot_id)
);
CREATE TABLE IF NOT EXISTS auction_scanned (
    house TEXT NOT NULL,
    auction_id INTEGER NOT NULL,
    closed INTEGER NOT NULL,
    PRIMARY KEY (house, auction_id)
);
"""


class LotStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def upsert(self, house: str, lot: Lot) -> None:
        self.conn.execute(
            """INSERT INTO auction_lots VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(house, lot_id) DO UPDATE SET
                 hammer=excluded.hammer, next_bid=excluded.next_bid,
                 end_time=excluded.end_time, status=excluded.status,
                 sold=excluded.sold, updated_at=excluded.updated_at,
                 description=COALESCE(NULLIF(excluded.description, ''), description),
                 condition_report=COALESCE(NULLIF(excluded.condition_report, ''),
                                           condition_report)""",
            (
                house, lot.id, lot.auction_id, lot.title, lot.hammer, lot.next_bid,
                lot.end_time.isoformat() if lot.end_time else None, lot.status,
                int(lot.sold), int(lot.postal), lot.vat_pct, lot.premium_pct,
                lot.internet_pct, lot.description, lot.condition_report,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def has_detail(self, house: str, lot_id: int) -> bool:
        row = self.conn.execute(
            "SELECT description FROM auction_lots WHERE house=? AND lot_id=?", (house, lot_id)
        ).fetchone()
        return bool(row and row["description"])

    def auction_done(self, house: str, auction_id: int) -> bool:
        row = self.conn.execute(
            "SELECT closed FROM auction_scanned WHERE house=? AND auction_id=?",
            (house, auction_id),
        ).fetchone()
        return bool(row and row["closed"])

    def mark_auction(self, house: str, auction_id: int, closed: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO auction_scanned VALUES (?,?,?)",
            (house, auction_id, int(closed)),
        )
        self.conn.commit()

    def lots(self, house: str, *, sold: bool | None = None, open_only: bool = False) -> list[Lot]:
        sql = "SELECT * FROM auction_lots WHERE house=?"
        args: list = [house]
        if sold is not None:
            sql += " AND sold=?"
            args.append(int(sold))
        if open_only:
            sql += " AND status=0"
        return [_row_to_lot(r) for r in self.conn.execute(sql, args)]


def _row_to_lot(r: sqlite3.Row) -> Lot:
    return Lot(
        id=r["lot_id"],
        auction_id=r["auction_id"],
        title=r["title"],
        hammer=r["hammer"],
        next_bid=r["next_bid"],
        end_time=datetime.fromisoformat(r["end_time"]) if r["end_time"] else None,
        status=r["status"],
        has_winner=bool(r["sold"]),
        vat_pct=r["vat_pct"],
        premium_pct=r["premium_pct"],
        internet_pct=r["internet_pct"],
        postal=bool(r["postal"]),
        description=r["description"] or "",
        condition_report=r["condition_report"] or "",
    )
