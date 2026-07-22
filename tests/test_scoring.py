import pandas as pd

from stock_assistant.scoring import eligible_stock, score_stock


def sample_prices(count=120):
    return pd.DataFrame({
        "trade_date": pd.date_range("2025-01-01", periods=count).strftime("%Y%m%d"),
        "close": [10 + i * 0.02 for i in range(count)],
        "amount": [200_000 for _ in range(count)],  # 库内单位：千元
    })


def test_score_is_bounded_to_100():
    result = score_stock(
        sample_prices(), {"pe_ttm": 20},
        {"roe": 15, "revenue_yoy": 12, "netprofit_yoy": 15, "ocf_to_or": 10, "debt_to_assets": 40},
    )
    assert 0 <= result.total <= 100
    assert result.trend == 25


def test_stock_pool_filters_st_and_liquidity():
    assert not eligible_stock("ST样例", "20200101", 200_000_000, "20260101")[0]
    assert not eligible_stock("样例", "20200101", 50_000_000, "20260101")[0]
    assert eligible_stock("样例", "20200101", 200_000_000, "20260101")[0]
