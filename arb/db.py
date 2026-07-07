"""SQLite persistence layer.

Four tables:
  - listings     : every listing we have ever normalised
  - valuations   : cached resale valuations keyed by model_number (with TTL)
  - deals        : flagged deals
  - seen         : dedup table so a listing never alerts twice across runs

The module exposes a small set of upsert/query helpers. Everything else in the
codebase talks to SQLite only through this module.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arb.models import Deal, Listing, Valuation

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    source_listing_id  TEXT NOT NULL,
    title              TEXT NOT NULL,
    model_number       TEXT,
    brand              TEXT,
    price              REAL NOT NULL,
    shipping           REAL NOT NULL DEFAULT 0,
    condition          TEXT NOT NULL,
    url                TEXT NOT NULL,
    image_url          TEXT,
    location           TEXT,
    seen_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS valuations (
    model_number       TEXT PRIMARY KEY,
    ebay_sold_median   REAL,
    ebay_sold_count    INTEGER NOT NULL DEFAULT 0,
    amazon_price       REAL,
    amazon_rank        INTEGER,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    listing_id         TEXT PRIMARY KEY,
    buy_cost           REAL NOT NULL,
    est_resale         REAL NOT NULL,
    est_fees           REAL NOT NULL,
    est_profit         REAL NOT NULL,
    margin_pct         REAL NOT NULL,
    roi_pct            REAL NOT NULL,
    sell_channel       TEXT NOT NULL,
    is_scam_flag       INTEGER NOT NULL DEFAULT 0,
    flagged_at         TEXT NOT NULL,
    FOREIGN KEY (listing_id) REFERENCES listings(id)
);

CREATE TABLE IF NOT EXISTS seen (
    listing_id         TEXT PRIMARY KEY,
    first_seen_at      TEXT NOT NULL,
    alerted            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_deals_flagged_at ON deals(flagged_at);
CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the scheduler run jobs on worker threads.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self.init_schema()

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ listings
    def upsert_listing(self, listing: Listing) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO listings
                    (id, source, source_listing_id, title, model_number, brand,
                     price, shipping, condition, url, image_url, location, seen_at)
                VALUES (:id, :source, :source_listing_id, :title, :model_number, :brand,
                        :price, :shipping, :condition, :url, :image_url, :location, :seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, model_number=excluded.model_number,
                    brand=excluded.brand, price=excluded.price, shipping=excluded.shipping,
                    condition=excluded.condition, url=excluded.url,
                    image_url=excluded.image_url, location=excluded.location,
                    seen_at=excluded.seen_at
                """,
                {
                    "id": listing.id,
                    "source": listing.source,
                    "source_listing_id": listing.source_listing_id,
                    "title": listing.title,
                    "model_number": listing.model_number,
                    "brand": listing.brand,
                    "price": listing.price,
                    "shipping": listing.shipping,
                    "condition": listing.condition.value,
                    "url": listing.url,
                    "image_url": listing.image_url,
                    "location": listing.location,
                    "seen_at": _iso(listing.seen_at),
                },
            )

    # ------------------------------------------------------------------ valuations
    def get_valuation(self, model_number: str, ttl_hours: int) -> Valuation | None:
        """Return a cached valuation only if it is fresh enough (within TTL)."""
        row = self._conn.execute(
            "SELECT * FROM valuations WHERE model_number = ?", (model_number,)
        ).fetchone()
        if row is None:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        if datetime.now(UTC) - updated > timedelta(hours=ttl_hours):
            return None
        return Valuation(
            model_number=row["model_number"],
            ebay_sold_median=row["ebay_sold_median"],
            ebay_sold_count=row["ebay_sold_count"],
            amazon_price=row["amazon_price"],
            amazon_rank=row["amazon_rank"],
            updated_at=updated,
        )

    def upsert_valuation(self, valuation: Valuation) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO valuations
                    (model_number, ebay_sold_median, ebay_sold_count,
                     amazon_price, amazon_rank, updated_at)
                VALUES (:model_number, :ebay_sold_median, :ebay_sold_count,
                        :amazon_price, :amazon_rank, :updated_at)
                ON CONFLICT(model_number) DO UPDATE SET
                    ebay_sold_median=excluded.ebay_sold_median,
                    ebay_sold_count=excluded.ebay_sold_count,
                    amazon_price=excluded.amazon_price,
                    amazon_rank=excluded.amazon_rank,
                    updated_at=excluded.updated_at
                """,
                {
                    "model_number": valuation.model_number,
                    "ebay_sold_median": valuation.ebay_sold_median,
                    "ebay_sold_count": valuation.ebay_sold_count,
                    "amazon_price": valuation.amazon_price,
                    "amazon_rank": valuation.amazon_rank,
                    "updated_at": _iso(valuation.updated_at),
                },
            )

    # ------------------------------------------------------------------ deals
    def upsert_deal(self, deal: Deal) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO deals
                    (listing_id, buy_cost, est_resale, est_fees, est_profit,
                     margin_pct, roi_pct, sell_channel, is_scam_flag, flagged_at)
                VALUES (:listing_id, :buy_cost, :est_resale, :est_fees, :est_profit,
                        :margin_pct, :roi_pct, :sell_channel, :is_scam_flag, :flagged_at)
                ON CONFLICT(listing_id) DO UPDATE SET
                    buy_cost=excluded.buy_cost, est_resale=excluded.est_resale,
                    est_fees=excluded.est_fees, est_profit=excluded.est_profit,
                    margin_pct=excluded.margin_pct, roi_pct=excluded.roi_pct,
                    sell_channel=excluded.sell_channel, is_scam_flag=excluded.is_scam_flag,
                    flagged_at=excluded.flagged_at
                """,
                {
                    "listing_id": deal.listing_id,
                    "buy_cost": deal.buy_cost,
                    "est_resale": deal.est_resale,
                    "est_fees": deal.est_fees,
                    "est_profit": deal.est_profit,
                    "margin_pct": deal.margin_pct,
                    "roi_pct": deal.roi_pct,
                    "sell_channel": deal.sell_channel.value,
                    "is_scam_flag": int(deal.is_scam_flag),
                    "flagged_at": _iso(deal.flagged_at),
                },
            )

    def deal_exists(self, listing_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM deals WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ dedup (seen)
    def is_seen(self, listing_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, listing_id: str) -> bool:
        """Record a listing as seen. Returns True if this is the first sighting."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO seen (listing_id, first_seen_at, alerted) VALUES (?, ?, 0)",
                (listing_id, _iso(datetime.now(UTC))),
            )
            return cur.rowcount > 0

    def mark_alerted(self, listing_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE seen SET alerted = 1 WHERE listing_id = ?", (listing_id,))

    def was_alerted(self, listing_id: str) -> bool:
        row = self._conn.execute(
            "SELECT alerted FROM seen WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return bool(row and row["alerted"])

    # ------------------------------------------------------------------ stats
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()
