"""Application configuration.

All tunable behaviour lives here. Values are read from the environment (or a
`.env` file) and validated on startup via pydantic-settings, so a
misconfigured deployment fails loudly instead of silently misbehaving.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime / storage ---
    env: str = Field(default="dev", description="'dev' or 'prod' — controls log formatting.")
    db_path: str = Field(default="data/arb.db", description="SQLite database file path.")
    log_level: str = Field(default="INFO")

    # --- eBay Browse / Marketplace Insights API ---
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    ebay_marketplace: str = Field(default="EBAY_GB")
    # Toggle if the account tier has access to Marketplace Insights (sold data).
    ebay_has_insights: bool = Field(default=False)
    # Comma-separated search terms and filters for the eBay source.
    ebay_queries: str = Field(default="", description="Comma-separated eBay search terms.")
    ebay_category_id: str | None = Field(default=None, description="eBay category id, e.g. 9355 for phones.")
    ebay_max_price: float | None = Field(default=None, description="Max BIN price to consider.")
    ebay_limit: int = Field(default=50, description="Max results per eBay query.")

    # --- Keepa (Amazon data) ---
    keepa_api_key: str | None = None
    # Keepa domain id: 1=US, 2=UK, 3=DE ... default UK to match EBAY_GB.
    keepa_domain: int = Field(default=2)

    # --- Telegram ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Optional separate low-priority chat for "too good to be true" scam flags.
    telegram_scam_chat_id: str | None = None

    # --- Deal thresholds ---
    min_profit: float = Field(default=25.0, description="Minimum £ profit to flag a deal.")
    min_roi: float = Field(default=30.0, description="Minimum ROI %.")
    min_sold_count: int = Field(default=3, description="Minimum eBay sold comps to trust a median.")
    max_amazon_rank: int = Field(default=50_000, description="Reject Amazon deals ranked worse than this.")
    tgtbt_ratio: float = Field(
        default=0.20,
        description="buy_cost below this fraction of resale => flag as likely scam.",
    )
    allow_for_parts: bool = Field(default=False, description="Allow 'for_parts' condition listings.")

    # --- Fee model (tune to your real seller fees) ---
    ebay_fvf_pct: float = Field(default=12.8, description="eBay final value fee %.")
    ebay_fixed_fee: float = Field(default=0.30, description="eBay per-order fixed fee (£).")
    ebay_payment_pct: float = Field(default=0.0, description="Extra payment processing %, if any.")
    amazon_referral_pct: float = Field(default=8.0, description="Amazon referral fee %.")
    amazon_fba_fee: float = Field(default=3.0, description="Flat FBA fulfilment estimate (£).")
    packaging_cost: float = Field(default=2.50, description="Packaging cost per item (£).")

    # --- Caching ---
    valuation_ttl_hours: int = Field(default=24, description="Re-query a valuation only if older than this.")

    # --- Scraping ---
    scrape_min_delay_sec: float = Field(default=4.0)
    scrape_max_delay_sec: float = Field(default=12.0)
    enable_gumtree: bool = Field(default=False)
    enable_fb_marketplace: bool = Field(default=False)
    scrape_location: str = Field(default="", description="Default location/postcode for scrapers.")
    scrape_default_shipping: float = Field(default=0.0, description="Assumed shipping for collection-only listings.")
    scrape_queries: str = Field(default="", description="Comma-separated search terms for scraper sources.")
    scrape_max_price: float | None = Field(default=None, description="Max price for scraper searches.")

    @property
    def ebay_query_list(self) -> list[str]:
        return [q.strip() for q in self.ebay_queries.split(",") if q.strip()]

    @property
    def scrape_query_list(self) -> list[str]:
        return [q.strip() for q in self.scrape_queries.split(",") if q.strip()]

    # --- Pipeline behaviour ---
    dry_run: bool = Field(default=False, description="Scan + evaluate but never send alerts.")

    @field_validator("scrape_max_delay_sec")
    @classmethod
    def _max_gte_min(cls, v: float, info) -> float:
        min_delay = info.data.get("scrape_min_delay_sec", 0.0)
        if v < min_delay:
            raise ValueError("scrape_max_delay_sec must be >= scrape_min_delay_sec")
        return v

    @field_validator("tgtbt_ratio")
    @classmethod
    def _ratio_bounds(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("tgtbt_ratio must be between 0 and 1")
        return v

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)

    def ensure_db_dir(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Call `get_settings.cache_clear()` in tests."""
    return Settings()
