from __future__ import annotations

import httpx
import pytest
import respx

from arb.models import Condition, SellChannel
from engine.deals import cex_floor_profit, evaluate
from oracle.cex_client import (
    CexBox,
    CexClient,
    parse_boxes,
    quote_from_boxes,
    select_boxes,
    strip_grade,
)
from tests.conftest import make_listing, make_valuation


def _payload(*boxes: dict) -> dict:
    return {"response": {"ack": "Success", "data": {"boxes": list(boxes)}}, "error": {}}


def _box(name: str, sell: float, cash: float, exchange: float = 0.0, **kwargs) -> dict:
    return {
        "boxId": kwargs.get("box_id", name[:10]),
        "boxName": name,
        "sellPrice": sell,
        "cashPrice": cash,
        "exchangePrice": exchange,
        "categoryFriendlyName": kwargs.get("category", "Phones"),
        "outOfStock": kwargs.get("out_of_stock", 0),
    }


# ------------------------------------------------------------------ parsing
def test_parse_boxes_reads_all_three_prices():
    boxes = parse_boxes(_payload(_box("Apple iPhone 12 128GB Blue, Unlocked A", 300, 180, 210)))
    assert len(boxes) == 1
    box = boxes[0]
    assert box.sell_price == 300.0
    assert box.cash_price == 180.0
    assert box.exchange_price == 210.0
    assert box.grade == "A"
    assert box.name == "Apple iPhone 12 128GB Blue, Unlocked"


def test_parse_boxes_skips_entries_without_a_price():
    boxes = parse_boxes(_payload(_box("Broken thing", 0, 0), _box("Good thing", 50, 20)))
    assert [b.name for b in boxes] == ["Good thing"]


def test_parse_boxes_handles_a_failed_response():
    assert parse_boxes({"response": {"ack": "Failure", "data": {}}}) == []
    assert parse_boxes({}) == []


def test_strip_grade_handles_the_formats_cex_uses():
    assert strip_grade("iPhone 12 128GB A") == ("iPhone 12 128GB", "A")
    assert strip_grade("iPhone 12 128GB, B") == ("iPhone 12 128GB", "B")
    assert strip_grade("iPhone 12 128GB (C)") == ("iPhone 12 128GB", "C")
    assert strip_grade("iPhone 12 128GB") == ("iPhone 12 128GB", None)


def test_strip_grade_does_not_eat_a_real_trailing_word():
    name, grade = strip_grade("Nintendo Switch Console")
    assert name == "Nintendo Switch Console"
    assert grade is None


# ------------------------------------------------------------------ matching
def test_select_boxes_rejects_accessories_and_near_misses():
    boxes = parse_boxes(
        _payload(
            _box("Apple iPhone 12 128GB Blue", 300, 180),
            _box("Apple iPhone 12 128GB Case", 5, 1),
            _box("Apple iPhone 13 128GB Blue", 400, 250),
        )
    )
    kept = select_boxes("Apple iPhone 12 128GB", boxes, min_relevance=0.6)
    assert [b.name for b in kept] == ["Apple iPhone 12 128GB Blue"]


def test_quote_prefers_the_grade_matching_the_condition():
    boxes = [
        CexBox("a", "Apple iPhone 12 128GB", 320, 200, 230, grade="A"),
        CexBox("b", "Apple iPhone 12 128GB", 280, 160, 190, grade="B"),
        CexBox("c", "Apple iPhone 12 128GB", 240, 130, 150, grade="C"),
    ]
    used = quote_from_boxes("Apple iPhone 12 128GB", boxes, condition=Condition.USED)
    new = quote_from_boxes("Apple iPhone 12 128GB", boxes, condition=Condition.NEW)

    assert used.sell_price == 280  # grade B
    assert new.sell_price == 320   # grade A


def test_quote_takes_the_median_when_condition_is_unknown():
    """Spanning the grades beats assuming the best case."""
    boxes = [
        CexBox("a", "Apple iPhone 12 128GB", 320, 200, 230, grade="A"),
        CexBox("b", "Apple iPhone 12 128GB", 280, 160, 190, grade="B"),
        CexBox("c", "Apple iPhone 12 128GB", 240, 130, 150, grade="C"),
    ]
    quote = quote_from_boxes("Apple iPhone 12 128GB", boxes, condition=Condition.UNKNOWN)
    assert quote.sell_price == 280
    assert quote.cash_price == 160
    assert quote.n_matched == 3


def test_quote_is_none_when_nothing_matches():
    boxes = [CexBox("x", "Samsung Galaxy S21", 200, 100, 120)]
    assert quote_from_boxes("Apple iPhone 12 128GB", boxes) is None


# ------------------------------------------------------------------ client
@pytest.mark.asyncio
@respx.mock
async def test_client_quotes_from_a_live_style_response():
    respx.get(url__startswith="https://wss2.cex.uk.webuy.io/v3/boxes").mock(
        return_value=httpx.Response(
            200, json=_payload(_box("Apple iPhone 12 128GB Blue, Unlocked B", 280, 160, 190))
        )
    )
    client = CexClient()
    try:
        quote = await client.quote("Apple iPhone 12 128GB Blue", Condition.USED)
    finally:
        await client.aclose()

    assert quote is not None
    assert quote.sell_price == 280
    assert quote.cash_price == 160


@pytest.mark.asyncio
@respx.mock
async def test_client_is_silent_on_failure():
    """CeX is an optional source; an outage must not break a scan."""
    respx.get(url__startswith="https://wss2.cex.uk.webuy.io").mock(
        return_value=httpx.Response(503)
    )
    client = CexClient()
    try:
        assert await client.search("anything") == []
        assert await client.quote("anything") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_client_survives_a_non_json_response():
    respx.get(url__startswith="https://wss2.cex.uk.webuy.io").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    client = CexClient()
    try:
        assert await client.search("anything") == []
    finally:
        await client.aclose()


def test_country_code_shapes_the_url():
    assert CexClient(country="es").base_url == "https://wss2.cex.es.webuy.io/v3"


# ------------------------------------------------------------------ the floor
def test_floor_profit_is_cash_price_minus_cost(settings):
    valuation = make_valuation(cex_cash_price=180.0)
    assert cex_floor_profit(valuation, buy_cost=150.0, settings=settings) == 30.0


def test_floor_profit_accounts_for_trade_in_cost(settings):
    settings.cex_trade_in_cost = 5.0
    valuation = make_valuation(cex_cash_price=180.0)
    assert cex_floor_profit(valuation, buy_cost=150.0, settings=settings) == 25.0


def test_no_floor_without_a_cex_quote(settings):
    assert cex_floor_profit(make_valuation(), buy_cost=150.0, settings=settings) is None


def test_no_floor_when_cex_is_disabled(settings):
    settings.enable_cex = False
    valuation = make_valuation(cex_cash_price=180.0)
    assert cex_floor_profit(valuation, buy_cost=150.0, settings=settings) is None


def test_floor_truncates_the_downside(settings):
    """A guaranteed cash offer replaces a worse predicted outcome."""
    listing = make_listing(price=200.0)
    without = evaluate(listing, make_valuation(price_p10=210.0), settings)
    with_floor = evaluate(listing, make_valuation(price_p10=210.0, cex_cash_price=245.0), settings)

    # At a p10 of 210 the eBay downside is a loss; CeX paying 245 is not.
    assert without.worst_case_profit < 0
    assert with_floor.worst_case_profit == 45.0
    assert with_floor.expected_profit > without.expected_profit


def test_floor_does_not_worsen_a_good_downside(settings):
    """A low cash offer must never drag the downside below the eBay case."""
    listing = make_listing(price=200.0)
    strong = evaluate(listing, make_valuation(price_p10=320.0), settings)
    with_low_floor = evaluate(
        listing, make_valuation(price_p10=320.0, cex_cash_price=50.0), settings
    )
    assert with_low_floor.worst_case_profit == strong.worst_case_profit


def test_a_guaranteed_floor_lifts_the_score(settings):
    listing = make_listing(price=200.0)
    plain = evaluate(listing, make_valuation(), settings)
    floored = evaluate(listing, make_valuation(cex_cash_price=260.0), settings)
    assert floored.score > plain.score
    assert floored.floor_profit == 60.0


def test_floor_is_reported_in_the_reasons(settings):
    deal = evaluate(make_listing(price=200.0), make_valuation(cex_cash_price=260.0), settings)
    assert any("CeX will pay" in reason for reason in deal.reasons)


def test_deal_still_works_with_no_cex_data(settings):
    deal = evaluate(make_listing(price=200.0), make_valuation(), settings)
    assert deal is not None
    assert deal.floor_profit is None
    assert deal.sell_channel == SellChannel.EBAY
