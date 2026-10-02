"""Auction houses as a buy side: scrape lots, value them on eBay, find the margin.

Separate from `sources/` because an auction lot is not a listing you can buy at
a price — it is a hammer price plus a fee schedule, and whether there is money
in it is a question about *past* hammer prices, not today's standing bid.
"""
