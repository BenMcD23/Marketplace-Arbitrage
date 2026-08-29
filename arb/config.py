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

    # --- Mini / micro PC profile (home-server & k3s node hunting) ---
    # Blank means "use the profile's built-in search terms" (profiles.mini_pc).
    mini_pc_queries: str = Field(default="", description="Comma-separated overrides for the mini-PC search terms.")
    mini_pc_limit: int = Field(default=50, description="Max results per mini-PC query.")
    mini_pc_max_price: float | None = Field(default=250.0, description="eBay-side price ceiling for the profile.")
    mini_pc_max_cpu_tdp_w: int = Field(default=35, description="Reject CPUs above this TDP (T-series only).")
    mini_pc_min_ram_gb: int = Field(default=16, description="Target RAM; below this needs an exceptional price.")
    mini_pc_hard_min_ram_gb: int = Field(default=8, description="Reject anything under this RAM outright.")
    mini_pc_min_storage_gb: int = Field(default=128, description="Reject smaller storage unless price is exceptional.")
    mini_pc_ram_upgrade_cost: float = Field(default=25.0, description="Estimated £ to fit a 16GB SO-DIMM.")
    mini_pc_assumed_delivery: float = Field(default=8.0, description="Assumed £ delivery when eBay quotes it at checkout.")
    mini_pc_strong_deal_max: float = Field(default=160.0, description="Total landed cost below this = 'strong deal'.")
    mini_pc_exceptional_deal_max: float = Field(default=130.0, description="Total landed cost below this = 'exceptional deal'.")
    mini_pc_include_broken: bool = Field(default=False, description="Include 'for parts or not working' listings.")

    # --- Keepa (Amazon data) ---
    keepa_api_key: str | None = None
    # Keepa domain id: 1=US, 2=UK, 3=DE ... default UK to match EBAY_GB.
    keepa_domain: int = Field(default=2)

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
    def mini_pc_query_list(self) -> list[str]:
        return [q.strip() for q in self.mini_pc_queries.split(",") if q.strip()]

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

    @field_validator("mini_pc_exceptional_deal_max")
    @classmethod
    def _exceptional_below_strong(cls, v: float, info) -> float:
        strong = info.data.get("mini_pc_strong_deal_max")
        if strong is not None and v > strong:
            raise ValueError("mini_pc_exceptional_deal_max must be <= mini_pc_strong_deal_max")
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
