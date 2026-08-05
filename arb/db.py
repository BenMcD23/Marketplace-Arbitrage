"""SQLite persistence layer.

Tables:
  - listings          : every listing we have ever normalised
  - valuations        : cached resale valuations keyed by product_key (with TTL)
  - deals             : flagged deals
  - seen              : dedup table so a listing is never processed twice
  - comp_watch        : active comps we are tracking the lifecycle of
  - sold_observations : comps we watched end — our own free sold-price history
  - watch_queries     : the search terms the pipeline scans
  - runs              : pipeline run history

The module exposes a small set of upsert/query helpers. Everything else in the
codebase talks to SQLite only through this module.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arb.models import (
    CompRef,
    Condition,
    Deal,
    Listing,
    PriceBasis,
    Run,
    RunStatus,
    SellChannel,
    SoldObservation,
    Valuation,
    WatchQuery,
)

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
    product_key        TEXT PRIMARY KEY,
    resale_price       REAL,
    basis              TEXT NOT NULL DEFAULT 'none',
    comp_count         INTEGER NOT NULL DEFAULT 0,
    comps_rejected     INTEGER NOT NULL DEFAULT 0,
    dispersion_cv      REAL NOT NULL DEFAULT 0,
    price_p10          REAL,
    price_p90          REAL,
    confidence         REAL NOT NULL DEFAULT 0,
    sell_through_pct   REAL,
    est_days_to_sell   INTEGER,
    amazon_price       REAL,
    amazon_rank        INTEGER,
    sample_json        TEXT NOT NULL DEFAULT '[]',
    reject_json        TEXT NOT NULL DEFAULT '{}',
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
    p_sale             REAL NOT NULL DEFAULT 1,
    est_days_to_sell   INTEGER,
    holding_cost       REAL NOT NULL DEFAULT 0,
    expected_profit    REAL NOT NULL DEFAULT 0,
    confidence         REAL NOT NULL DEFAULT 0,
    score              REAL NOT NULL DEFAULT 0,
    worst_case_profit  REAL NOT NULL DEFAULT 0,
    is_scam_flag       INTEGER NOT NULL DEFAULT 0,
    reasons_json       TEXT NOT NULL DEFAULT '[]',
    flagged_at         TEXT NOT NULL,
    FOREIGN KEY (listing_id) REFERENCES listings(id)
);

CREATE TABLE IF NOT EXISTS seen (
    listing_id         TEXT PRIMARY KEY,
    first_seen_at      TEXT NOT NULL,
    alerted            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comp_watch (
    item_id            TEXT PRIMARY KEY,
    product_key        TEXT NOT NULL,
    title              TEXT NOT NULL,
    price              REAL NOT NULL,
    condition          TEXT NOT NULL DEFAULT 'unknown',
    url                TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    checked_at         TEXT,
    resolved           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sold_observations (
    item_id            TEXT PRIMARY KEY,
    product_key        TEXT NOT NULL,
    title              TEXT NOT NULL,
    price              REAL NOT NULL,
    condition          TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at      TEXT NOT NULL,
    sold_at            TEXT NOT NULL,
    days_listed        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch_queries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    query              TEXT NOT NULL UNIQUE,
    category_id        TEXT,
    max_price          REAL,
    min_price          REAL,
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    last_run_at        TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    listings_scanned   INTEGER NOT NULL DEFAULT 0,
    new_listings       INTEGER NOT NULL DEFAULT 0,
    valuations_fetched INTEGER NOT NULL DEFAULT 0,
    deals_found        INTEGER NOT NULL DEFAULT 0,
    scam_flags         INTEGER NOT NULL DEFAULT 0,
    sold_observed      INTEGER NOT NULL DEFAULT 0,
    api_calls          INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    by_source_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_deals_flagged_at ON deals(flagged_at);
CREATE INDEX IF NOT EXISTS idx_deals_score ON deals(score);
CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
CREATE INDEX IF NOT EXISTS idx_comp_watch_key ON comp_watch(product_key);
CREATE INDEX IF NOT EXISTS idx_comp_watch_stale ON comp_watch(resolved, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_sold_key ON sold_observations(product_key, sold_at);
"""

# Tables holding derived/cached data. If an older database has an incompatible
# shape, these are rebuilt rather than migrated column by column — everything in
# them is recomputable from a scan.
_REBUILDABLE = {
    "valuations": "product_key",
    "deals": "listing_id",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the scheduler and the API's background
        # tasks run against the same connection.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate()
        self.init_schema()

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """Drop cache tables whose shape predates the current schema."""
        for table, expected_pk in _REBUILDABLE.items():
            row = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None:
                continue
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if expected_pk not in cols or "confidence" not in cols:
                self._conn.execute(f"DROP TABLE {table}")
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

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

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

    def get_listing(self, listing_id: str) -> Listing | None:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return _row_to_listing(row) if row else None

    # ------------------------------------------------------------------ valuations
    def get_valuation(self, product_key: str, ttl_hours: int) -> Valuation | None:
        """Return a cached valuation only if it is fresh enough (within TTL)."""
        row = self._conn.execute(
            "SELECT * FROM valuations WHERE product_key = ?", (product_key,)
        ).fetchone()
        if row is None:
            return None
        updated = _parse_dt(row["updated_at"])
        if updated is None or datetime.now(UTC) - updated > timedelta(hours=ttl_hours):
            return None
        return _row_to_valuation(row)

    def get_valuation_any_age(self, product_key: str) -> Valuation | None:
        row = self._conn.execute(
            "SELECT * FROM valuations WHERE product_key = ?", (product_key,)
        ).fetchone()
        return _row_to_valuation(row) if row else None

    def upsert_valuation(self, valuation: Valuation) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO valuations
                    (product_key, resale_price, basis, comp_count, comps_rejected,
                     dispersion_cv, price_p10, price_p90, confidence, sell_through_pct,
                     est_days_to_sell, amazon_price, amazon_rank, sample_json,
                     reject_json, updated_at)
                VALUES (:product_key, :resale_price, :basis, :comp_count, :comps_rejected,
                        :dispersion_cv, :price_p10, :price_p90, :confidence, :sell_through_pct,
                        :est_days_to_sell, :amazon_price, :amazon_rank, :sample_json,
                        :reject_json, :updated_at)
                ON CONFLICT(product_key) DO UPDATE SET
                    resale_price=excluded.resale_price, basis=excluded.basis,
                    comp_count=excluded.comp_count, comps_rejected=excluded.comps_rejected,
                    dispersion_cv=excluded.dispersion_cv, price_p10=excluded.price_p10,
                    price_p90=excluded.price_p90, confidence=excluded.confidence,
                    sell_through_pct=excluded.sell_through_pct,
                    est_days_to_sell=excluded.est_days_to_sell,
                    amazon_price=excluded.amazon_price, amazon_rank=excluded.amazon_rank,
                    sample_json=excluded.sample_json, reject_json=excluded.reject_json,
                    updated_at=excluded.updated_at
                """,
                {
                    "product_key": valuation.product_key,
                    "resale_price": valuation.resale_price,
                    "basis": valuation.basis.value,
                    "comp_count": valuation.comp_count,
                    "comps_rejected": valuation.comps_rejected,
                    "dispersion_cv": valuation.dispersion_cv,
                    "price_p10": valuation.price_p10,
                    "price_p90": valuation.price_p90,
                    "confidence": valuation.confidence,
                    "sell_through_pct": valuation.sell_through_pct,
                    "est_days_to_sell": valuation.est_days_to_sell,
                    "amazon_price": valuation.amazon_price,
                    "amazon_rank": valuation.amazon_rank,
                    "sample_json": json.dumps([c.model_dump(mode="json") for c in valuation.sample]),
                    "reject_json": json.dumps(valuation.reject_reasons),
                    "updated_at": _iso(valuation.updated_at),
                },
            )

    # ------------------------------------------------------------------ deals
    def upsert_deal(self, deal: Deal) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO deals
                    (listing_id, buy_cost, est_resale, est_fees, est_profit, margin_pct,
                     roi_pct, sell_channel, p_sale, est_days_to_sell, holding_cost,
                     expected_profit, confidence, score, worst_case_profit,
                     is_scam_flag, reasons_json, flagged_at)
                VALUES (:listing_id, :buy_cost, :est_resale, :est_fees, :est_profit, :margin_pct,
                        :roi_pct, :sell_channel, :p_sale, :est_days_to_sell, :holding_cost,
                        :expected_profit, :confidence, :score, :worst_case_profit,
                        :is_scam_flag, :reasons_json, :flagged_at)
                ON CONFLICT(listing_id) DO UPDATE SET
                    buy_cost=excluded.buy_cost, est_resale=excluded.est_resale,
                    est_fees=excluded.est_fees, est_profit=excluded.est_profit,
                    margin_pct=excluded.margin_pct, roi_pct=excluded.roi_pct,
                    sell_channel=excluded.sell_channel, p_sale=excluded.p_sale,
                    est_days_to_sell=excluded.est_days_to_sell,
                    holding_cost=excluded.holding_cost,
                    expected_profit=excluded.expected_profit,
                    confidence=excluded.confidence, score=excluded.score,
                    worst_case_profit=excluded.worst_case_profit,
                    is_scam_flag=excluded.is_scam_flag, reasons_json=excluded.reasons_json,
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
                    "p_sale": deal.p_sale,
                    "est_days_to_sell": deal.est_days_to_sell,
                    "holding_cost": deal.holding_cost,
                    "expected_profit": deal.expected_profit,
                    "confidence": deal.confidence,
                    "score": deal.score,
                    "worst_case_profit": deal.worst_case_profit,
                    "is_scam_flag": int(deal.is_scam_flag),
                    "reasons_json": json.dumps(deal.reasons),
                    "flagged_at": _iso(deal.flagged_at),
                },
            )

    def deal_exists(self, listing_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM deals WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    def get_deal(self, listing_id: str) -> Deal | None:
        row = self._conn.execute(
            "SELECT * FROM deals WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return _row_to_deal(row) if row else None

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

    # ------------------------------------------------------------------ comp watch
    def record_comp_sighting(
        self,
        item_id: str,
        product_key: str,
        title: str,
        price: float,
        condition: Condition,
        url: str | None,
        now: datetime | None = None,
    ) -> None:
        """Note that a comp is currently live. First sighting starts its clock."""
        stamp = _iso(now or datetime.now(UTC))
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO comp_watch
                    (item_id, product_key, title, price, condition, url,
                     first_seen_at, last_seen_at, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(item_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    price=excluded.price,
                    title=excluded.title
                """,
                (item_id, product_key, title, price, condition.value, url, stamp, stamp),
            )

    def stale_comps(self, not_seen_for_hours: int, limit: int) -> list[dict]:
        """Watched comps that have dropped out of search results.

        Ordered oldest-sighting-first so the ones most likely to have ended get
        the scarce API calls.
        """
        cutoff = _iso(datetime.now(UTC) - timedelta(hours=not_seen_for_hours))
        rows = self._conn.execute(
            """
            SELECT * FROM comp_watch
            WHERE resolved = 0 AND last_seen_at < ?
            ORDER BY last_seen_at ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_comp_ended(self, item_id: str) -> None:
        """Retire a comp: it has ended, and its sale is now on record."""
        with self._tx() as c:
            c.execute(
                "UPDATE comp_watch SET resolved = 1, checked_at = ? WHERE item_id = ?",
                (_iso(datetime.now(UTC)), item_id),
            )

    def mark_comp_still_live(self, item_id: str) -> None:
        """A comp that fell out of search but is still listed.

        Its clock is restarted rather than retired: it remains live inventory,
        so it belongs in the sell-through denominator, and it may still sell
        later — which is a sold observation we would otherwise never record.
        """
        with self._tx() as c:
            c.execute(
                "UPDATE comp_watch SET last_seen_at = ?, checked_at = ? WHERE item_id = ?",
                (_iso(datetime.now(UTC)), _iso(datetime.now(UTC)), item_id),
            )

    def touch_comp_check(self, item_id: str) -> None:
        """Record a check that was inconclusive, without resolving the comp."""
        with self._tx() as c:
            c.execute(
                "UPDATE comp_watch SET checked_at = ? WHERE item_id = ?",
                (_iso(datetime.now(UTC)), item_id),
            )

    def count_live_comps(self, product_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM comp_watch WHERE product_key = ? AND resolved = 0",
            (product_key,),
        ).fetchone()
        return row["n"] or 0

    # ------------------------------------------------------------------ sold observations
    def record_sold(self, obs: SoldObservation) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO sold_observations
                    (item_id, product_key, title, price, condition,
                     first_seen_at, sold_at, days_listed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (
                    obs.item_id,
                    obs.product_key,
                    obs.title,
                    obs.price,
                    obs.condition.value,
                    _iso(obs.first_seen_at),
                    _iso(obs.sold_at),
                    obs.days_listed,
                ),
            )

    def sold_observations(self, product_key: str, days: int = 90) -> list[SoldObservation]:
        cutoff = _iso(datetime.now(UTC) - timedelta(days=days))
        rows = self._conn.execute(
            "SELECT * FROM sold_observations WHERE product_key = ? AND sold_at >= ? "
            "ORDER BY sold_at DESC",
            (product_key, cutoff),
        ).fetchall()
        return [
            SoldObservation(
                item_id=r["item_id"],
                product_key=r["product_key"],
                title=r["title"],
                price=r["price"],
                condition=Condition(r["condition"]),
                first_seen_at=_parse_dt(r["first_seen_at"]) or datetime.now(UTC),
                sold_at=_parse_dt(r["sold_at"]) or datetime.now(UTC),
                days_listed=r["days_listed"],
            )
            for r in rows
        ]

    def sold_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM sold_observations"
        ).fetchone()["n"]

    # ------------------------------------------------------------------ watch queries
    def list_queries(self, enabled_only: bool = False) -> list[WatchQuery]:
        sql = "SELECT * FROM watch_queries"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [_row_to_query(r) for r in self._conn.execute(sql).fetchall()]

    def add_query(self, q: WatchQuery) -> WatchQuery:
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO watch_queries
                    (query, category_id, max_price, min_price, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    category_id=excluded.category_id, max_price=excluded.max_price,
                    min_price=excluded.min_price, enabled=excluded.enabled
                """,
                (
                    q.query,
                    q.category_id,
                    q.max_price,
                    q.min_price,
                    int(q.enabled),
                    _iso(q.created_at),
                ),
            )
            new_id = cur.lastrowid
        row = self._conn.execute(
            "SELECT * FROM watch_queries WHERE id = ? OR query = ?", (new_id, q.query)
        ).fetchone()
        return _row_to_query(row)

    def update_query(self, query_id: int, **fields) -> WatchQuery | None:
        allowed = {"query", "category_id", "max_price", "min_price", "enabled"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            if "enabled" in sets:
                sets["enabled"] = int(sets["enabled"])
            clause = ", ".join(f"{k} = ?" for k in sets)
            with self._tx() as c:
                c.execute(
                    f"UPDATE watch_queries SET {clause} WHERE id = ?",
                    (*sets.values(), query_id),
                )
        row = self._conn.execute(
            "SELECT * FROM watch_queries WHERE id = ?", (query_id,)
        ).fetchone()
        return _row_to_query(row) if row else None

    def delete_query(self, query_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM watch_queries WHERE id = ?", (query_id,))
            return cur.rowcount > 0

    def mark_query_run(self, query: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE watch_queries SET last_run_at = ? WHERE query = ?",
                (_iso(datetime.now(UTC)), query),
            )

    # ------------------------------------------------------------------ runs
    def start_run(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs (status, started_at) VALUES (?, ?)",
                (RunStatus.RUNNING.value, _iso(datetime.now(UTC))),
            )
            return cur.lastrowid

    def finish_run(self, run: Run) -> None:
        with self._tx() as c:
            c.execute(
                """
                UPDATE runs SET
                    status = ?, finished_at = ?, listings_scanned = ?, new_listings = ?,
                    valuations_fetched = ?, deals_found = ?, scam_flags = ?,
                    sold_observed = ?, api_calls = ?, error = ?, by_source_json = ?
                WHERE id = ?
                """,
                (
                    run.status.value,
                    _iso(run.finished_at or datetime.now(UTC)),
                    run.listings_scanned,
                    run.new_listings,
                    run.valuations_fetched,
                    run.deals_found,
                    run.scam_flags,
                    run.sold_observed,
                    run.api_calls,
                    run.error,
                    json.dumps(run.by_source),
                    run.id,
                ),
            )

    def list_runs(self, limit: int = 20) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    def get_run(self, run_id: int) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None


# ---------------------------------------------------------------------------
# Row -> model mappers
# ---------------------------------------------------------------------------


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        id=row["id"],
        source=row["source"],
        source_listing_id=row["source_listing_id"],
        title=row["title"],
        model_number=row["model_number"],
        brand=row["brand"],
        price=row["price"],
        shipping=row["shipping"],
        condition=Condition(row["condition"]),
        url=row["url"],
        image_url=row["image_url"],
        location=row["location"],
        seen_at=_parse_dt(row["seen_at"]) or datetime.now(UTC),
    )


def _row_to_valuation(row: sqlite3.Row) -> Valuation:
    return Valuation(
        product_key=row["product_key"],
        resale_price=row["resale_price"],
        basis=PriceBasis(row["basis"]),
        comp_count=row["comp_count"],
        comps_rejected=row["comps_rejected"],
        dispersion_cv=row["dispersion_cv"],
        price_p10=row["price_p10"],
        price_p90=row["price_p90"],
        confidence=row["confidence"],
        sell_through_pct=row["sell_through_pct"],
        est_days_to_sell=row["est_days_to_sell"],
        amazon_price=row["amazon_price"],
        amazon_rank=row["amazon_rank"],
        sample=[CompRef(**c) for c in json.loads(row["sample_json"] or "[]")],
        reject_reasons=json.loads(row["reject_json"] or "{}"),
        updated_at=_parse_dt(row["updated_at"]) or datetime.now(UTC),
    )


def _row_to_deal(row: sqlite3.Row) -> Deal:
    return Deal(
        listing_id=row["listing_id"],
        buy_cost=row["buy_cost"],
        est_resale=row["est_resale"],
        est_fees=row["est_fees"],
        est_profit=row["est_profit"],
        margin_pct=row["margin_pct"],
        roi_pct=row["roi_pct"],
        sell_channel=SellChannel(row["sell_channel"]),
        p_sale=row["p_sale"],
        est_days_to_sell=row["est_days_to_sell"],
        holding_cost=row["holding_cost"],
        expected_profit=row["expected_profit"],
        confidence=row["confidence"],
        score=row["score"],
        worst_case_profit=row["worst_case_profit"],
        is_scam_flag=bool(row["is_scam_flag"]),
        reasons=json.loads(row["reasons_json"] or "[]"),
        flagged_at=_parse_dt(row["flagged_at"]) or datetime.now(UTC),
    )


def _row_to_query(row: sqlite3.Row) -> WatchQuery:
    return WatchQuery(
        id=row["id"],
        query=row["query"],
        category_id=row["category_id"],
        max_price=row["max_price"],
        min_price=row["min_price"],
        enabled=bool(row["enabled"]),
        created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
        last_run_at=_parse_dt(row["last_run_at"]),
    )


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        status=RunStatus(row["status"]),
        started_at=_parse_dt(row["started_at"]) or datetime.now(UTC),
        finished_at=_parse_dt(row["finished_at"]),
        listings_scanned=row["listings_scanned"],
        new_listings=row["new_listings"],
        valuations_fetched=row["valuations_fetched"],
        deals_found=row["deals_found"],
        scam_flags=row["scam_flags"],
        sold_observed=row["sold_observed"],
        api_calls=row["api_calls"],
        error=row["error"],
        by_source=json.loads(row["by_source_json"] or "{}"),
    )
