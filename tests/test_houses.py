from __future__ import annotations

import json

from arb.models import PriceBasis, SellChannel, Valuation
from engine.fees import profit_at
from houses.analysis import is_faulty, is_it_lot, landed_cost, max_hammer, search_title
from houses.simon_charles import Lot, parse_auction_ids, parse_lots
from houses.store import LotStore


def _rsc_page(payload: str) -> str:
    """Wrap a payload the way Next.js streams it: JSON-escaped, split in chunks."""
    half = len(payload) // 2
    chunks = [payload[:half], payload[half:]]
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks
    )
    return f'<html><a href="/auctions/13395/x">a</a><a href="/auctions/13297/y">b</a>{pushes}'


_LIST_LOT = {
    "id": 5902205, "auctionId": 13395, "lotNo": 13,
    "title": "UNBOXED HP ELITEBOOK X360 1040 6G INTEL I-7 8TH GEN LAPTOP IN SILVER ",
    "hammerPrice": 40, "askingPrice": 42, "endTime": "2026-10-01T19:00:00.000Z",
    "winningUserId": 1, "status": 2, "postalBinId": 24, "vat": 20,
    "auctionBuyersPremium": 20, "auctionInternetPremium": 5,
}


def _lot(**kw) -> Lot:
    base = dict(
        id=1, auction_id=1, title="DELL LATITUDE 5490", hammer=40.0, next_bid=42.0,
        end_time=None, status=2, has_winner=True, vat_pct=20, premium_pct=20,
        internet_pct=5, postal=True,
    )
    base.update(kw)
    return Lot(**base)


def test_parse_lots_from_list_and_detail_objects():
    detail = dict(_LIST_LOT, id=5900914, title="DELL LATITUDE 5480",
                  description="Screen off; not tested", conditionReport="light scratches")
    payload = (
        '33:["$","$L35",null,{"_lots":' + json.dumps([_LIST_LOT]) + "}]\n"
        '34:["$","$L3a",null,{"_lot":' + json.dumps(detail) + "}]"
    )
    lots = parse_lots(_rsc_page(payload))

    assert set(lots) == {5902205, 5900914}
    lot = lots[5902205]
    assert lot.hammer == 40 and lot.next_bid == 42
    assert lot.sold and not lot.is_open and lot.postal
    assert lot.title.endswith("SILVER")  # whitespace normalised
    assert lots[5900914].description == "Screen off; not tested"


def test_open_or_unsold_lots_are_not_sold():
    assert not _lot(status=0).sold
    assert not _lot(has_winner=False).sold
    assert not _lot(hammer=None).sold


def test_parse_auction_ids():
    assert parse_auction_ids(_rsc_page("")) == [13297, 13395]


def test_search_title_keeps_the_product_and_drops_the_lot_description():
    assert search_title("DELL LATITUDE 5480 LAPTOP – BLACK, 14-INCH") == "dell latitude 5480"
    assert (
        search_title("MICROSOFT SURFACE PRO 5 TABLET – SILVER, I5, 8GB RAM, 256GB SSD")
        == "microsoft surface pro 5 i5"
    )
    assert search_title("APPLE IPAD TABLET - A2316") == "apple ipad a2316"


def test_it_filter():
    assert is_it_lot("UNBOXED HP ELITEBOOK X360 1040 6G")  # x360 is not "360 units"
    assert is_it_lot("DELL P2419H 24 INCH MONITOR")
    assert not is_it_lot("DELL MONITOR STAND")
    assert not is_it_lot("BOXED FLIP STORAGE CABINET")
    assert not is_it_lot("WRIST ELECTRONIC SPHYGMOMANOMETER (BLOOD PRESSURE MONITOR)")
    assert not is_it_lot("PALLET OF ASSORTED HOUSEHOLD GOODS TO INCLUDE;MONITOR, KETTLE")


def test_stated_faults_are_flagged_but_untested_is_not():
    assert is_faulty(_lot(title="LAPTOP - CRACKED SCREEN"))
    assert is_faulty(_lot(condition_report="Missing battery"))
    assert not is_faulty(_lot(description="Screen off in images; not tested"))


def test_landed_cost_applies_vat_premiums_and_lot_fee(settings):
    settings.auction_postage_estimate = 8.0
    # 60 * 1.2 (VAT) + (60 * 25% + 2) * 1.2 (fees + VAT) + 8 postage = 72 + 20.4 + 8
    assert landed_cost(60, _lot(), settings) == 100.4
    # A VAT-free hammer, collected: only the premiums carry VAT.
    settings.auction_collection_cost = 0.0
    assert landed_cost(60, _lot(vat_pct=0, postal=False), settings) == 80.4


def test_max_hammer_is_the_last_bid_that_clears_both_gates(settings):
    v = Valuation(product_key="k", basis=PriceBasis.ACTIVE, resale_price=200.0,
                  confidence=0.6)
    lot = _lot()
    ceiling = max_hammer(lot, v, settings)
    assert ceiling > 0

    def clears(h: float) -> bool:
        p = profit_at(200.0, landed_cost(h, lot, settings), SellChannel.EBAY, settings)
        return p.profit >= settings.min_profit and p.roi_pct >= settings.min_roi

    assert clears(ceiling)
    assert not clears(ceiling + 1)


def test_max_hammer_is_zero_without_a_trusted_valuation(settings):
    weak = Valuation(product_key="k", basis=PriceBasis.ACTIVE, resale_price=200.0,
                     confidence=0.1)
    assert max_hammer(_lot(), weak, settings) == 0.0


def test_store_round_trip_keeps_detail_on_later_list_upserts(tmp_path):
    store = LotStore(tmp_path / "a.db")
    store.upsert("sc", _lot(description="has charger"))
    store.upsert("sc", _lot(description="", hammer=55.0))  # later list-page refresh

    [lot] = store.lots("sc", sold=True)
    assert lot.hammer == 55.0
    assert lot.description == "has charger"
    assert store.has_detail("sc", 1)
