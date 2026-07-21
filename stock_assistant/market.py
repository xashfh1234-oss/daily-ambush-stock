import pandas as pd

from .database import query, upsert_records
from .scoring import eligible_stock, score_stock


def price_frame(path, ts_code: str) -> pd.DataFrame:
    rows = query(path, "SELECT * FROM daily_prices WHERE ts_code=? ORDER BY trade_date", (ts_code.upper(),))
    return pd.DataFrame([dict(row) for row in rows])


def latest_trade_date(path) -> str | None:
    rows = query(path, "SELECT MAX(trade_date) value FROM daily_prices")
    return rows[0]["value"] if rows else None


def market_as_of_date(path, minimum_coverage: float = 0.8) -> str | None:
    """Return the latest date covering most eligible locally priced stocks.

    This prevents a few manually entered or partially updated quotes from
    moving the whole-market ranking to a date the rest of the universe lacks.
    Filtered stocks are excluded because daily updates intentionally do not
    maintain them. Before eligibility has been initialized, all priced stocks
    are used as a safe fallback.
    """
    rows = query(
        path,
        """SELECT p.trade_date,COUNT(DISTINCT p.ts_code) n
        FROM daily_prices p JOIN stock_sync_status q ON q.ts_code=p.ts_code
        WHERE q.eligible=1 GROUP BY p.trade_date ORDER BY p.trade_date DESC""",
    )
    if not rows:
        rows = query(
            path,
            "SELECT trade_date,COUNT(DISTINCT ts_code) n FROM daily_prices GROUP BY trade_date ORDER BY trade_date DESC",
        )
    if not rows:
        return None
    peak = max(int(row["n"]) for row in rows)
    threshold = max(1, int(peak * minimum_coverage))
    return next((row["trade_date"] for row in rows if int(row["n"]) >= threshold), None)


def build_scores(path, trade_date: str | None = None) -> int:
    trade_date = trade_date or market_as_of_date(path)
    if not trade_date:
        return 0
    stocks = query(
        path,
        """SELECT s.ts_code,s.name,s.list_date,b.pe_ttm,b.pb,b.ps_ttm
        FROM stocks s LEFT JOIN daily_basic b
          ON b.ts_code=s.ts_code AND b.trade_date=?
        WHERE s.list_status='L'""",
        (trade_date,),
    )
    results = []
    for stock in stocks:
        prices = query(
            path,
            "SELECT * FROM daily_prices WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 120",
            (stock["ts_code"], trade_date),
        )
        if len(prices) < 60:
            continue
        price_data = pd.DataFrame([dict(row) for row in reversed(prices)])
        average_amount = price_data["amount"].tail(20).mean() * 1000
        allowed, _ = eligible_stock(stock["name"], stock["list_date"], average_amount, trade_date)
        if not allowed:
            continue
        financial_rows = query(
            path,
            """SELECT * FROM financial_indicators
            WHERE ts_code=? AND ann_date<=? ORDER BY ann_date DESC,end_date DESC LIMIT 1""",
            (stock["ts_code"], trade_date),
        )
        financial = dict(financial_rows[0]) if financial_rows else None
        result = score_stock(price_data, dict(stock), financial)
        results.append({
            "ts_code": stock["ts_code"], "trade_date": trade_date,
            "fundamental_score": result.fundamental, "trend_score": result.trend,
            "momentum_score": result.momentum, "risk_score": result.risk,
            "liquidity_score": result.liquidity, "total_score": result.total,
            "reason": "；".join(result.reasons),
        })
    return upsert_records(
        path, "scores", results,
        ["ts_code", "trade_date", "fundamental_score", "trend_score", "momentum_score", "risk_score", "liquidity_score", "total_score", "reason"],
    )


def ranked_scores(path, limit=20):
    return query(
        path,
        """SELECT sc.*,s.name,s.industry,p.close
        FROM scores sc JOIN stocks s ON s.ts_code=sc.ts_code
        LEFT JOIN daily_prices p ON p.ts_code=sc.ts_code AND p.trade_date=sc.trade_date
        WHERE sc.trade_date=(SELECT MAX(trade_date) FROM scores)
        ORDER BY sc.total_score DESC LIMIT ?""",
        (limit,),
    )
