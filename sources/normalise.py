"""Title / condition normalisation shared by all sources.

Extracts brand and a best-effort model number from free-text listing titles and
maps site-specific condition strings onto the canonical `Condition` enum.
"""

from __future__ import annotations

import re

from arb.models import Condition

# A small, extendable brand lookup. Keys are matched case-insensitively as whole
# words. Value is the canonical brand name.
BRANDS: dict[str, str] = {
    "apple": "Apple",
    "iphone": "Apple",
    "ipad": "Apple",
    "macbook": "Apple",
    "airpods": "Apple",
    "samsung": "Samsung",
    "galaxy": "Samsung",
    "sony": "Sony",
    "playstation": "Sony",
    "ps5": "Sony",
    "ps4": "Sony",
    "bose": "Bose",
    "dell": "Dell",
    "hp": "HP",
    "lenovo": "Lenovo",
    "thinkpad": "Lenovo",
    "asus": "Asus",
    "acer": "Acer",
    "microsoft": "Microsoft",
    "xbox": "Microsoft",
    "surface": "Microsoft",
    "nintendo": "Nintendo",
    "switch": "Nintendo",
    "google": "Google",
    "pixel": "Google",
    "lg": "LG",
    "canon": "Canon",
    "nikon": "Nikon",
    "gopro": "GoPro",
    "dyson": "Dyson",
    "razer": "Razer",
    "logitech": "Logitech",
    "sonos": "Sonos",
}

# Model-number-ish tokens: mixes of letters+digits (A2338, WH-1000XM4, RTX3080,
# SM-G991B), or well-known plain patterns. Ordered from most to least specific.
_MODEL_PATTERNS = [
    re.compile(r"\bWH-?1000XM\d\b", re.I),          # Sony headphones
    re.compile(r"\b[A-Z]{1,3}-?[A-Z]?\d{3,5}[A-Z]{0,2}\b"),  # SM-G991B, WF-1000
    re.compile(r"\bA\d{4}\b"),                        # Apple model ids e.g. A2338
    re.compile(r"\bRTX\s?\d{3,4}\s?(TI|SUPER)?\b", re.I),  # GPUs
    re.compile(r"\b\d{3,4}[A-Z]{1,3}\b"),            # 3080Ti-style
    re.compile(r"\b[A-Z]{2,}\d{2,}[A-Z0-9]*\b"),     # generic ALPHANUM
]

_CONDITION_MAP = {
    "new": Condition.NEW,
    "brand new": Condition.NEW,
    "new other": Condition.NEW,
    "new (other)": Condition.NEW,
    "open box": Condition.NEW,
    "seller refurbished": Condition.USED,
    "manufacturer refurbished": Condition.USED,
    "refurbished": Condition.USED,
    "used": Condition.USED,
    "very good": Condition.USED,
    "good": Condition.USED,
    "acceptable": Condition.USED,
    "pre-owned": Condition.USED,
    "for parts or not working": Condition.FOR_PARTS,
    "for parts": Condition.FOR_PARTS,
    "spares or repair": Condition.FOR_PARTS,
    "not working": Condition.FOR_PARTS,
    "faulty": Condition.FOR_PARTS,
    "spares": Condition.FOR_PARTS,
}


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def extract_brand(title: str) -> str | None:
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    wordset = set(words)
    for key, canonical in BRANDS.items():
        if key in wordset:
            return canonical
    return None


def extract_model_number(title: str) -> str | None:
    for pat in _MODEL_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(0).upper().replace(" ", "")
    return None


def normalise_condition(raw: str | None) -> Condition:
    if not raw:
        return Condition.UNKNOWN
    key = raw.strip().lower()
    if key in _CONDITION_MAP:
        return _CONDITION_MAP[key]
    # substring fallback for messy free-text conditions
    for phrase, cond in _CONDITION_MAP.items():
        if phrase in key:
            return cond
    return Condition.UNKNOWN
