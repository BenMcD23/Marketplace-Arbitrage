"""Mini / micro form-factor PC profile — hunting k3s / home-server nodes.

This is a *buy-to-keep* profile, not a resale-arbitrage one, so it never goes
near the pricing oracle. A listing is worth surfacing when the silicon is right
and the price is low, so the rules are:

  * CPU  — a low-power (T-suffix, <= 35W) chip from `CPU_TIERS`. Ordinary
           desktop parts (i5-10500, i5-9400) run 65W and are rejected outright:
           they are the wrong shape for a cluster node that idles 24/7.
  * RAM  — 16GB or better. 8-15GB survives only when the price is exceptional
           *and* the memory is socketed, and is flagged with the upgrade cost.
           Under 8GB is always rejected.
  * Cost — everything is judged on **total landed cost** (item + delivery).
           Item price alone is never a comparison key.

Ranking is CPU tier first, landed cost second: a faster node for a few pounds
more beats a slower one, but within a tier the cheapest wins.

Every function here is pure and offline-testable; the eBay I/O lives in
`sources/ebay_mini_pc.py` and the orchestration in `arb.pipeline`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Condition, DeliveryKind, Listing, ListingType

log = get_logger("profiles.mini_pc")

#: Search terms, run as separate queries. Combining them into one OR-query
#: makes eBay's relevance ranking drop the long tail, so each gets its own call.
SEARCH_TERMS: list[str] = [
    "OptiPlex Micro i5-10500T",
    "OptiPlex Micro i5-10400T",
    "ProDesk 400 G6 i5-10500T",
    "ProDesk 400 G6 i7-10700T",
    "EliteDesk 800 G4 i5-8500T",
    "OptiPlex 7060 Micro i5-8500T",
    "ThinkCentre M70q i5-10400T",
    "ThinkCentre Tiny i5-8500T",
]


# --------------------------------------------------------------------------- CPU
@dataclass(frozen=True)
class Cpu:
    """A CPU we are willing to buy, with its rank inside the profile."""

    name: str
    #: Higher is better. Drives the primary sort.
    tier: int
    tdp_w: int


#: The only CPUs this profile scores, best first. `tier` is the primary ranking
#: key; `tdp_w` is enforced against `Settings.mini_pc_max_cpu_tdp_w` so a 65W
#: part can never sneak in by being added to this table by mistake.
CPU_TIERS: dict[str, Cpu] = {
    "i7-10700T": Cpu("i7-10700T", tier=5, tdp_w=35),
    "i5-10500T": Cpu("i5-10500T", tier=4, tdp_w=35),
    "i5-10400T": Cpu("i5-10400T", tier=3, tdp_w=35),
    "i5-9500T": Cpu("i5-9500T", tier=2, tdp_w=35),
    "i5-8500T": Cpu("i5-8500T", tier=1, tdp_w=35),
}

MAX_TIER = max(c.tier for c in CPU_TIERS.values())

#: Case-insensitive lookup — listing titles write part numbers every which way
#: ("I5-10500T", "i5 10500t"), while `CPU_TIERS` keeps the readable spelling.
_CPU_BY_KEY = {name.upper(): cpu for name, cpu in CPU_TIERS.items()}

# "i5-10500T", "i5 10500t", "I7-10700T". The suffix group is what separates a
# 35W T part from its 65W namesake.
_CPU_RE = re.compile(r"\bi([3579])[\s\-_]?(\d{4,5})\s?([a-z]{0,2})\b", re.I)


@dataclass(frozen=True)
class CpuMatch:
    family: str  # "i5"
    model: str  # "10500"
    suffix: str  # "T" (empty for a plain desktop part)

    @property
    def key(self) -> str:
        return f"{self.family}-{self.model}{self.suffix}".upper()

    @property
    def is_low_power(self) -> bool:
        return "T" in self.suffix.upper()


def find_cpus(text: str) -> list[CpuMatch]:
    """Every Intel Core part number mentioned in the text, in order."""
    return [
        CpuMatch(family=f"i{m.group(1)}", model=m.group(2), suffix=m.group(3).upper())
        for m in _CPU_RE.finditer(text)
    ]


# ------------------------------------------------------------------------ specs
_STORAGE_WORD = r"(?:ssd|hdd|nvme|m\.?\s?2|hard\s*(?:disk|drive)|storage|emmc|sata|pcie)"

# RAM has to be told apart from storage: "256GB SSD" is not 256GB of memory, so
# a GB figure only counts as RAM when a memory word sits next to it. The gap
# between the two allows a couple of plain words ("8GB soldered RAM") but never
# another size token, so "512GB NVMe 16GB DDR4" reads as 16GB and not 512GB.
_RAM_WORD = r"(?:ram|ddr[2345]|memory|so-?dimm|dimm)"
_RAM_GAP = rf"(?:(?!\d)(?!{_STORAGE_WORD}\b)[a-z]+\s+){{0,2}}?"
_RAM_KIT_RE = re.compile(rf"(\d{{1,2}})\s*[x×]\s*(\d{{1,3}})\s*gb[^.,;|]{{0,20}}?{_RAM_WORD}", re.I)
_RAM_VALUE_RE = re.compile(rf"(\d{{1,3}})\s*gb\s*(?:pc\d+\s*)?{_RAM_GAP}{_RAM_WORD}", re.I)
# "RAM: 32GB" — but not "RAM 256GB SSD", where the figure belongs to the drive.
_RAM_LABEL_RE = re.compile(rf"{_RAM_WORD}\W{{0,10}}?(\d{{1,3}})\s*gb(?!\s*{_STORAGE_WORD})", re.I)
_NO_STORAGE_RE = re.compile(
    r"\b(?:no|without|zero|missing)\s+(?:ssd|hdd|nvme|storage|hard\s*(?:disk|drive)|drive|os)\b",
    re.I,
)
_STORAGE_TB_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*tb(?:\s*{_STORAGE_WORD})?", re.I)
_STORAGE_GB_RE = re.compile(rf"(\d{{2,4}})\s*gb\s*{_STORAGE_WORD}", re.I)
_STORAGE_LABEL_RE = re.compile(rf"{_STORAGE_WORD}\W{{0,10}}?(\d{{2,4}})\s*gb", re.I)

#: Soldered memory cannot be upgraded, so a low-RAM unit is a dead end.
_SOLDERED_RE = re.compile(r"\bsolder(?:ed|able)?\b|\bnon[- ]upgrad", re.I)


def parse_ram_gb(text: str) -> int | None:
    """Total RAM in GB, or None when the listing never says.

    Kit notation ("2x8GB DDR4") is multiplied out. A bare "16GB" with no memory
    word next to it is ignored — it is far more likely to be an SSD.
    """
    kits = [int(a) * int(b) for a, b in _RAM_KIT_RE.findall(text)]
    if kits:
        return max(kits)
    for pattern in (_RAM_VALUE_RE, _RAM_LABEL_RE):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def parse_storage_gb(text: str) -> int | None:
    """Largest drive in GB. 0 when the listing says there is no drive."""
    if _NO_STORAGE_RE.search(text):
        return 0
    sizes: list[int] = [int(float(tb) * 1024) for tb in _STORAGE_TB_RE.findall(text)]
    for pattern in (_STORAGE_GB_RE, _STORAGE_LABEL_RE):
        sizes.extend(int(gb) for gb in pattern.findall(text))
    return max(sizes) if sizes else None


def ram_is_swappable(text: str) -> bool:
    """Micro-form-factor PCs use SO-DIMM slots unless they say otherwise."""
    return not _SOLDERED_RE.search(text)


def spec_text(listing: Listing) -> str:
    """Title plus description — specs hide in whichever one had room."""
    return " ".join(filter(None, [listing.title, listing.description]))


# ------------------------------------------------------------------- evaluation
class DealBand(str, Enum):
    EXCEPTIONAL = "exceptional"
    STRONG = "strong"
    STANDARD = "standard"


class Reject(str, Enum):
    """Why a listing was dropped. Counted per-run so the filters stay honest."""

    CONDITION_FOR_PARTS = "condition_for_parts"
    CONDITION_NOT_USED = "condition_not_used"
    CPU_MISSING = "cpu_missing"
    CPU_NOT_LOW_POWER = "cpu_not_low_power"
    CPU_NOT_IN_TIER_TABLE = "cpu_not_in_tier_table"
    CPU_TDP_TOO_HIGH = "cpu_tdp_too_high"
    RAM_UNKNOWN = "ram_unknown"
    RAM_BELOW_FLOOR = "ram_below_floor"
    RAM_BELOW_TARGET = "ram_below_target"
    STORAGE_BELOW_FLOOR = "storage_below_floor"
    AUCTION_NO_BID = "auction_no_bid"


@dataclass
class MiniPcCandidate:
    """A listing that passed every filter, with everything the alert needs."""

    listing: Listing
    cpu: Cpu
    ram_gb: int | None
    storage_gb: int | None
    band: DealBand
    needs_ram_upgrade: bool = False
    ram_upgrade_cost: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def item_price(self) -> float:
        return round(self.listing.price, 2)

    @property
    def delivery_cost(self) -> float:
        return round(self.listing.shipping, 2)

    @property
    def total_landed(self) -> float:
        """Item + delivery. The only figure this profile ever ranks on."""
        return self.listing.buy_cost

    @property
    def all_in_cost(self) -> float:
        """Landed cost plus the RAM upgrade, when one is needed."""
        return round(self.total_landed + (self.ram_upgrade_cost or 0.0), 2)

    @property
    def sort_key(self) -> tuple[int, float]:
        # CPU tier descending (negated), then total landed cost ascending.
        return (-self.cpu.tier, self.total_landed)

    def time_remaining(self) -> timedelta | None:
        return self.listing.time_remaining()


@dataclass
class Verdict:
    """The outcome of evaluating one listing — a candidate or a reason why not."""

    candidate: MiniPcCandidate | None = None
    reason: Reject | None = None

    @property
    def accepted(self) -> bool:
        return self.candidate is not None


def select_cpu(text: str, settings: Settings) -> tuple[Cpu | None, Reject | None]:
    """Pick the best table CPU in the text, or say why none qualifies."""
    matches = find_cpus(text)
    if not matches:
        return None, Reject.CPU_MISSING

    low_power = [m for m in matches if m.is_low_power]
    if not low_power:
        # e.g. "OptiPlex 7060 i5-10500" — the 65W desktop part, wrong machine.
        return None, Reject.CPU_NOT_LOW_POWER

    known = [_CPU_BY_KEY[m.key] for m in low_power if m.key in _CPU_BY_KEY]
    if not known:
        return None, Reject.CPU_NOT_IN_TIER_TABLE

    best = max(known, key=lambda c: c.tier)
    if best.tdp_w > settings.mini_pc_max_cpu_tdp_w:
        return None, Reject.CPU_TDP_TOO_HIGH
    return best, None


def band_for(total_landed: float, settings: Settings) -> DealBand:
    if total_landed < settings.mini_pc_exceptional_deal_max:
        return DealBand.EXCEPTIONAL
    if total_landed < settings.mini_pc_strong_deal_max:
        return DealBand.STRONG
    return DealBand.STANDARD


def evaluate(listing: Listing, settings: Settings, include_broken: bool = False) -> Verdict:
    """Decide whether a listing is a mini-PC candidate worth surfacing."""
    text = spec_text(listing)

    # --- Condition --------------------------------------------------------
    if listing.condition == Condition.FOR_PARTS and not include_broken:
        return Verdict(reason=Reject.CONDITION_FOR_PARTS)
    if listing.condition == Condition.NEW:
        # This profile hunts used/refurbished stock; new units never clear the
        # price bands anyway. UNKNOWN is kept — the eBay-side condition filter
        # has already narrowed the search, eBay just did not label the item.
        return Verdict(reason=Reject.CONDITION_NOT_USED)

    # --- CPU tier ---------------------------------------------------------
    cpu, cpu_reject = select_cpu(text, settings)
    if cpu is None:
        return Verdict(reason=cpu_reject)

    # --- Cost (needed before RAM/storage: both have an "exceptional" escape) --
    total_landed = listing.buy_cost
    band = band_for(total_landed, settings)
    exceptional = band == DealBand.EXCEPTIONAL

    notes: list[str] = []
    needs_ram_upgrade = False
    ram_upgrade_cost: float | None = None

    # --- RAM --------------------------------------------------------------
    ram_gb = parse_ram_gb(text)
    if ram_gb is None:
        # We cannot prove >= 16GB, and an unverified 4GB unit is worse than a
        # missed deal, so unknown RAM is rejected rather than surfaced.
        return Verdict(reason=Reject.RAM_UNKNOWN)
    if ram_gb < settings.mini_pc_hard_min_ram_gb:
        return Verdict(reason=Reject.RAM_BELOW_FLOOR)
    if ram_gb < settings.mini_pc_min_ram_gb:
        # Under target but above the floor: worth it only if the price is
        # exceptional *and* the memory can actually be swapped.
        if not (exceptional and ram_is_swappable(text)):
            return Verdict(reason=Reject.RAM_BELOW_TARGET)
        needs_ram_upgrade = True
        ram_upgrade_cost = settings.mini_pc_ram_upgrade_cost
        notes.append(
            f"Needs RAM upgrade: {ram_gb}GB fitted, ~£{ram_upgrade_cost:.0f} "
            f"for {settings.mini_pc_min_ram_gb}GB SO-DIMM"
        )

    # --- Storage ----------------------------------------------------------
    storage_gb = parse_storage_gb(text)
    if storage_gb is not None and storage_gb < settings.mini_pc_min_storage_gb:
        if not exceptional:
            return Verdict(reason=Reject.STORAGE_BELOW_FLOOR)
        notes.append(
            f"Only {storage_gb}GB storage — priced low enough to be worth a drive swap"
        )
    if storage_gb is None:
        notes.append("Storage not stated in the listing")

    # --- Buying route -----------------------------------------------------
    if listing.listing_type == ListingType.AUCTION_WITH_BIN:
        notes.append("Auction with Buy It Now — landed cost is on the current bid")
    if listing.best_offer:
        notes.append("Best Offer accepted — landed cost is negotiable downwards")
    if listing.delivery_kind == DeliveryKind.ESTIMATED:
        notes.append(
            f"Delivery quoted at checkout — landed cost assumes £{listing.shipping:.2f}"
        )
    elif listing.delivery_kind == DeliveryKind.COLLECTION:
        notes.append("Collection only — no delivery cost, but you have to fetch it")

    return Verdict(
        candidate=MiniPcCandidate(
            listing=listing,
            cpu=cpu,
            ram_gb=ram_gb,
            storage_gb=storage_gb,
            band=band,
            needs_ram_upgrade=needs_ram_upgrade,
            ram_upgrade_cost=ram_upgrade_cost,
            notes=notes,
        )
    )


def rank(candidates: list[MiniPcCandidate]) -> list[MiniPcCandidate]:
    """CPU tier descending, then total landed cost ascending."""
    return sorted(candidates, key=lambda c: c.sort_key)
