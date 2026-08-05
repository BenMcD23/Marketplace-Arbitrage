"""Comparable-listing selection.

The oracle's accuracy is decided here, not in the statistics. A search for
"iPhone 12 128GB" comes back with genuine handsets, but also cases, screen
protectors, charging cables, cracked-screen salvage, "empty box only" listings
and job lots of ten. Averaging those together produces a resale figure that is
confidently wrong, and a confidently wrong resale figure is exactly how an
arbitrage bot talks itself into losing money.

So every candidate comp is scored against the listing being valued and must
earn its place:

  1. Hard rejects — accessory / part / bundle / box-only listings.
  2. Variant match — capacity and model-line tokens must not contradict.
  3. Relevance — enough of the listing's distinguishing words must appear.

Pure functions over titles and prices; no network, no config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from arb.models import Condition

# ---------------------------------------------------------------------------
# Accessory / non-device detection
# ---------------------------------------------------------------------------

# Phrases that almost always mean "this is not the device itself". Matched as
# whole words against the lowercased title. Kept deliberately conservative:
# a false reject only costs us one comp, but a false accept poisons the median.
_ACCESSORY_TERMS = {
    # protection & carry
    "case", "cases", "cover", "covers", "sleeve", "pouch", "holster",
    "screen protector", "tempered glass", "skin", "decal", "sticker",
    # cables & power
    "cable", "cables", "charger", "chargers", "charging dock", "dock",
    "adapter", "adaptor", "power supply", "psu", "usb cable", "lead",
    # spares & salvage
    "replacement screen", "lcd", "digitizer", "digitiser", "back glass",
    "battery replacement", "motherboard", "logic board", "housing",
    "flex cable", "camera lens", "rear glass", "spare parts", "repair kit",
    # packaging & documentation
    "empty box", "box only", "boxed empty", "manual only", "instructions only",
    "receipt", "packaging only",
    # not the product
    "poster", "sticker set", "keyring", "mug", "t shirt", "tshirt",
    # listings that price several units
    "job lot", "joblot", "bundle of", "wholesale", "pallet",
}

# Multi-unit patterns: "x10", "10x", "lot of 5", "set of 3", "pack of 4".
_MULTI_UNIT_RE = re.compile(
    r"\b(?:x\s?\d{2,}|\d{2,}\s?x|lot of \d+|set of \d+|pack of \d+|bulk)\b", re.I
)

# Capacity tokens — 64GB, 1TB, 512 GB.
_CAPACITY_RE = re.compile(r"\b(\d+)\s?(gb|tb)\b", re.I)

# Words carrying no distinguishing information, so they should not count
# towards relevance.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "in", "on", "of", "to",
    "new", "used", "boxed", "unlocked", "sim", "free", "genuine", "original",
    "official", "uk", "gb", "fast", "shipping", "delivery", "sealed", "mint",
    "excellent", "good", "condition", "grade", "very", "great", "perfect",
    "working", "tested", "fully", "vgc", "pristine", "immaculate",
}


@dataclass
class Comp:
    """A single comparable listing used to value something."""

    title: str
    price: float
    condition: Condition = Condition.UNKNOWN
    item_id: str | None = None
    url: str | None = None
    #: Set when this comp is an observed sale rather than an asking price.
    sold: bool = False
    #: Populated by `select_comps`.
    relevance: float = 0.0


@dataclass
class CompSelection:
    """The outcome of filtering a candidate comp set, with reasons."""

    kept: list[Comp] = field(default_factory=list)
    #: (comp, reason) pairs — surfaced in the UI so a valuation can be audited.
    rejected: list[tuple[Comp, str]] = field(default_factory=list)

    @property
    def prices(self) -> list[float]:
        return [c.price for c in self.kept]

    def reject_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.rejected:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def tokenize(title: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords removed, order preserved."""
    raw = re.findall(r"[a-z0-9]+", title.lower())
    return [t for t in raw if t not in _STOPWORDS and len(t) > 1]


def is_accessory(title: str) -> bool:
    """True when the title looks like an accessory, spare part or multi-pack."""
    low = title.lower()
    if _MULTI_UNIT_RE.search(low):
        return True
    words = set(re.findall(r"[a-z]+", low))
    for term in _ACCESSORY_TERMS:
        if " " in term:
            if term in low:
                return True
        elif term in words:
            return True
    return False


def extract_capacity(title: str) -> str | None:
    """Normalised storage capacity, e.g. '128gb'. None when unstated.

    Capacities are normalised to GB so 1TB and 1024GB compare equal.
    """
    match = _CAPACITY_RE.search(title)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    gb = amount * 1024 if unit == "tb" else amount
    return f"{gb}gb"


def relevance(target_tokens: list[str], comp_title: str) -> float:
    """Fraction of the target's distinguishing tokens present in the comp.

    Deliberately asymmetric: a comp with *extra* words ("iPhone 12 128GB with
    charger and case") is fine, a comp *missing* the target's words ("iPhone
    11") is not. Numeric tokens are weighted double because in electronics the
    numbers are the product — the gap between an iPhone 12 and an iPhone 13 is
    one character and about £150.
    """
    if not target_tokens:
        return 0.0
    comp_tokens = set(tokenize(comp_title))

    matched = 0.0
    total = 0.0
    for token in target_tokens:
        weight = 2.0 if any(ch.isdigit() for ch in token) else 1.0
        total += weight
        if token in comp_tokens:
            matched += weight
    return round(matched / total, 3) if total else 0.0


def select_comps(
    target_title: str,
    candidates: list[Comp],
    min_relevance: float = 0.6,
    target_condition: Condition = Condition.UNKNOWN,
) -> CompSelection:
    """Filter and score candidate comps against the listing being valued.

    `target_condition` is not used to reject — condition is handled as a price
    adjustment downstream, where a small same-condition sample can still beat a
    large mixed one — but parts/salvage comps are always dropped because their
    prices describe a different market.
    """
    selection = CompSelection()
    target_tokens = tokenize(target_title)
    target_capacity = extract_capacity(target_title)

    for comp in candidates:
        if comp.price is None or comp.price <= 0:
            selection.rejected.append((comp, "no_price"))
            continue

        if is_accessory(comp.title):
            selection.rejected.append((comp, "accessory_or_lot"))
            continue

        if comp.condition == Condition.FOR_PARTS:
            selection.rejected.append((comp, "for_parts"))
            continue

        # A stated capacity that contradicts the target is a different product.
        # A comp that states no capacity is allowed through — plenty of genuine
        # listings omit it — and the price statistics can absorb the noise.
        if target_capacity is not None:
            comp_capacity = extract_capacity(comp.title)
            if comp_capacity is not None and comp_capacity != target_capacity:
                selection.rejected.append((comp, "capacity_mismatch"))
                continue

        score = relevance(target_tokens, comp.title)
        if score < min_relevance:
            selection.rejected.append((comp, "low_relevance"))
            continue

        comp.relevance = score
        selection.kept.append(comp)

    selection.kept.sort(key=lambda c: c.relevance, reverse=True)
    return selection
