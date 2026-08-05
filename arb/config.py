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

    # --- eBay Browse API (free tier: 5,000 calls/day) ---
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    ebay_marketplace: str = Field(default="EBAY_GB")
    #: Marketplace Insights (sold data) is a limited release that eBay no longer
    #: grants to new applicants. Leave false unless your app was approved.
    ebay_has_insights: bool = Field(default=False)
    ebay_daily_call_limit: int = Field(
        default=5000, description="Daily eBay API call budget (free tier is 5000)."
    )
    #: Seed search terms. Once the app has run, queries are managed in the
    #: database (and the UI); this list is only used to populate an empty table.
    ebay_queries: str = Field(default="", description="Comma-separated seed search terms.")
    ebay_category_id: str | None = Field(default=None, description="eBay category id, e.g. 9355.")
    ebay_max_price: float | None = Field(default=None, description="Max BIN price to consider.")
    ebay_limit: int = Field(default=50, description="Max results per eBay search query.")

    # --- Comp selection -------------------------------------------------
    comp_search_limit: int = Field(
        default=100, description="Comps requested per valuation lookup."
    )
    min_comp_relevance: float = Field(
        default=0.6, description="Fraction of the listing's key tokens a comp must match."
    )
    min_comps: int = Field(default=4, description="Minimum kept comps to price at all.")
    min_condition_comps: int = Field(
        default=3, description="Same-condition comps needed before pricing off them."
    )
    valuation_sample_size: int = Field(
        default=8, description="Comps retained on a valuation for the UI audit panel."
    )

    # --- Valuation model ------------------------------------------------
    active_to_sold_ratio: float = Field(
        default=0.88,
        description="Default asking->sold discount, used until enough data to calibrate.",
    )
    calibration_min_keys: int = Field(
        default=15, description="Product keys with paired data needed to trust a learned ratio."
    )
    used_to_new_ratio: float = Field(
        default=0.75, description="Default used/new price ratio when comps are one-sided."
    )
    min_sold_comps: int = Field(
        default=5, description="Observed sales needed to prefer the sold basis over asking prices."
    )
    sold_window_days: int = Field(default=90, description="Look-back window for observed sales.")
    confidence_target_comps: int = Field(
        default=12, description="Comp count at which sample-size confidence saturates."
    )
    confidence_max_cv: float = Field(
        default=0.5, description="Dispersion (sigma/median) at which spread confidence hits zero."
    )

    # --- Sold-price tracking (free alternative to Marketplace Insights) --
    comp_stale_hours: int = Field(
        default=36, description="Hours a comp must be missing from search before it is checked."
    )
    sold_sweep_max_checks: int = Field(
        default=150, description="Max ended-listing checks per run (1 API call each)."
    )
    #: While sold history is thin the daily API allowance is mostly idle, and
    #: every idle call is a day added to the cold start. Below this many
    #: observations the sweep is allowed a much bigger budget.
    sold_bootstrap_threshold: int = Field(
        default=500, description="Observed sales below which the sweep runs in bootstrap mode."
    )
    sold_sweep_bootstrap_checks: int = Field(
        default=1500, description="Max ended-listing checks per run while bootstrapping."
    )

    # --- CeX (free used-electronics prices, no key needed) --------------
    #: CeX gives a real used-retail price and, more usefully, a guaranteed cash
    #: offer that bounds the downside. Works from the first scan, so it covers
    #: the cold start while the sold-price tracker builds up history.
    enable_cex: bool = Field(default=True, description="Use CeX as a price source.")
    cex_country: str = Field(default="uk", description="CeX country code: uk, es, ie ...")
    cex_to_ebay_ratio: float = Field(
        default=0.85,
        description="CeX retail price -> private-sale eBay price. CeX sells with a shop's margin.",
    )
    cex_trade_in_cost: float = Field(
        default=0.0,
        description="Cost of realising a CeX cash sale (postage for online trade-in; 0 in store).",
    )
    cex_min_relevance: float = Field(
        default=0.6, description="How well a CeX product must match the listing title."
    )

    # --- Terapeak (real sold data via your own eBay login) --------------
    # NOTE: automating the eBay site outside its APIs is contrary to eBay's User
    # Agreement, and the account at risk is the one you sell on. Off by default.
    enable_terapeak: bool = Field(
        default=False, description="Scrape Terapeak via a saved logged-in session."
    )
    terapeak_day_range: int = Field(default=90, description="Look-back window in days.")
    terapeak_render_wait_ms: int = Field(
        default=4000, description="How long to wait for the metrics to render."
    )

    # --- Keepa (Amazon data) — optional, paid ---------------------------
    keepa_api_key: str | None = None
    keepa_domain: int = Field(default=2, description="1=US, 2=UK, 3=DE ...")

    # --- Deal thresholds ------------------------------------------------
    min_profit: float = Field(default=25.0, description="Minimum £ profit to flag a deal.")
    min_roi: float = Field(default=30.0, description="Minimum ROI %.")
    min_expected_profit: float = Field(
        default=15.0, description="Minimum risk-adjusted £ profit to flag a deal."
    )
    min_confidence: float = Field(
        default=0.35, description="Minimum valuation confidence (0-1) to trust a deal."
    )
    min_score: float = Field(default=0.0, description="Minimum composite score (0-100) to flag.")
    max_amazon_rank: int = Field(default=50_000, description="Reject Amazon deals ranked worse.")
    tgtbt_ratio: float = Field(
        default=0.20,
        description="buy_cost below this fraction of resale => flag as likely scam.",
    )
    allow_for_parts: bool = Field(default=False, description="Allow 'for_parts' condition listings.")

    # --- Liquidity / risk model -----------------------------------------
    base_sell_probability: float = Field(
        default=0.75, description="Assumed sale probability when liquidity data is missing."
    )
    default_days_to_sell: int = Field(
        default=21, description="Assumed days to sell when no observed velocity exists."
    )
    capital_annual_cost_pct: float = Field(
        default=12.0, description="Annualised cost of capital tied up in stock (%)."
    )

    # --- Fee model (tune to your real seller fees) ----------------------
    ebay_fvf_pct: float = Field(default=12.8, description="eBay final value fee %.")
    ebay_fixed_fee: float = Field(default=0.30, description="eBay per-order fixed fee (£).")
    ebay_payment_pct: float = Field(default=0.0, description="Extra payment processing %, if any.")
    ebay_ad_rate_pct: float = Field(
        default=0.0, description="Promoted Listings ad rate %, if you run them."
    )
    ebay_fvf_cap: float | None = Field(
        default=None, description="Optional cap on the eBay FVF portion (£)."
    )
    postage_cost: float = Field(
        default=3.50, description="What it costs you to post an item (£)."
    )
    amazon_referral_pct: float = Field(default=8.0, description="Amazon referral fee %.")
    amazon_fba_fee: float = Field(default=3.0, description="Flat FBA fulfilment estimate (£).")
    packaging_cost: float = Field(default=2.50, description="Packaging cost per item (£).")

    # --- Caching --------------------------------------------------------
    valuation_ttl_hours: int = Field(default=24, description="Re-query a valuation after this.")

    # --- API server -----------------------------------------------------
    api_cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="Comma-separated origins allowed to call the API.",
    )

    # --- Scraping -------------------------------------------------------
    scrape_min_delay_sec: float = Field(default=4.0)
    scrape_max_delay_sec: float = Field(default=12.0)
    enable_gumtree: bool = Field(default=False)
    enable_fb_marketplace: bool = Field(default=False)
    scrape_location: str = Field(default="", description="Default location/postcode for scrapers.")
    scrape_default_shipping: float = Field(default=0.0, description="Assumed collection shipping.")
    scrape_queries: str = Field(default="", description="Comma-separated scraper search terms.")
    scrape_max_price: float | None = Field(default=None, description="Max price for scrapers.")

    # --- Pipeline behaviour ---------------------------------------------
    dry_run: bool = Field(default=False, description="Scan + evaluate but never send alerts.")

    @property
    def ebay_query_list(self) -> list[str]:
        return [q.strip() for q in self.ebay_queries.split(",") if q.strip()]

    @property
    def scrape_query_list(self) -> list[str]:
        return [q.strip() for q in self.scrape_queries.split(",") if q.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def daily_capital_cost_pct(self) -> float:
        """Cost of capital per day, as a fraction."""
        return self.capital_annual_cost_pct / 100.0 / 365.0

    @field_validator("scrape_max_delay_sec")
    @classmethod
    def _max_gte_min(cls, v: float, info) -> float:
        min_delay = info.data.get("scrape_min_delay_sec", 0.0)
        if v < min_delay:
            raise ValueError("scrape_max_delay_sec must be >= scrape_min_delay_sec")
        return v

    @field_validator("tgtbt_ratio", "active_to_sold_ratio", "used_to_new_ratio")
    @classmethod
    def _ratio_bounds(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("ratio must be between 0 and 2")
        return v

    @field_validator("min_comp_relevance", "min_confidence", "base_sell_probability")
    @classmethod
    def _unit_bounds(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("value must be between 0 and 1")
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
