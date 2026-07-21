from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pandas as pd

from .database import connect, upsert_records


def normalize_code(code: str) -> tuple[str, str, str]:
    raw = code.strip().upper()
    digits = raw.split(".")[0] if raw[:1].isdigit() else raw.split(".")[-1]
    if len(digits) != 6 or not digits.isdigit():
        raise ValueError("股票代码格式应类似 000001.SZ 或 600000.SH")
    # Beijing Exchange adopted the 920xxx code range for newly listed shares.
    exchange = "BJ" if digits.startswith(("4", "8", "920")) else "SH" if digits.startswith(("5", "6", "9")) else "SZ"
    return digits, f"{digits}.{exchange}", f"{exchange.lower()}.{digits}"


class AkShareSource:
    name = "AKShare"

    def sync_stocks(self, path) -> int:
        import akshare as ak

        frame = ak.stock_info_a_code_name()
        if frame.empty:
            raise RuntimeError("AKShare 股票列表返回空数据")
        details = {}
        try:
            sh = ak.stock_info_sh_name_code()
            for row in sh.to_dict("records"):
                details[str(row["证券代码"]).zfill(6)] = {
                    "list_date": pd.to_datetime(row.get("上市日期"), errors="coerce"),
                    "industry": None,
                }
        except Exception:
            pass
        try:
            sz = ak.stock_info_sz_name_code(indicator="A股列表")
            for row in sz.to_dict("records"):
                details[str(row["A股代码"]).zfill(6)] = {
                    "list_date": pd.to_datetime(row.get("A股上市日期"), errors="coerce"),
                    "industry": row.get("所属行业"),
                }
        except Exception:
            pass
        records = []
        for row in frame.to_dict("records"):
            digits, ts_code, _ = normalize_code(str(row["code"]))
            detail = details.get(digits, {})
            listed = detail.get("list_date")
            list_date = listed.strftime("%Y%m%d") if listed is not None and not pd.isna(listed) else "19900101"
            records.append({
                "ts_code": ts_code, "symbol": digits, "name": row["name"],
                "area": None, "industry": detail.get("industry"), "market": ts_code[-2:],
                "list_date": list_date, "list_status": "L",
            })
        return upsert_records(path, "stocks", records, ["ts_code", "symbol", "name", "area", "industry", "market", "list_date", "list_status"])

    def sync_price_history(self, path, code: str, start_date: str, end_date: str) -> int:
        import akshare as ak

        digits, ts_code, _ = normalize_code(code)
        frame = ak.stock_zh_a_hist(symbol=digits, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if frame.empty:
            raise RuntimeError("AKShare 历史行情返回空数据")
        renamed = frame.rename(columns={
            "日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
            "涨跌幅": "pct_chg", "成交量": "vol", "成交额": "amount",
        })
        renamed["trade_date"] = pd.to_datetime(renamed["trade_date"]).dt.strftime("%Y%m%d")
        renamed["ts_code"] = ts_code
        renamed["pre_close"] = renamed["close"].shift(1)
        # 统一成Tushare约定：成交量为手、成交额为千元。
        renamed["amount"] = renamed["amount"] / 1000
        return upsert_records(path, "daily_prices", renamed.to_dict("records"),
                              ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"])


@contextmanager
def _baostock_session():
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败：{login.error_msg}")
    try:
        yield bs
    finally:
        bs.logout()


def _result_frame(result) -> pd.DataFrame:
    rows = []
    while result.error_code == "0" and result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    return pd.DataFrame(rows, columns=result.fields)


class BaoStockSource:
    name = "BaoStock"

    def sync_industries(self, path) -> int:
        with _baostock_session() as bs:
            frame = _result_frame(bs.query_stock_industry())
        if frame.empty:
            raise RuntimeError("BaoStock 行业分类返回空数据")
        values = []
        for row in frame.to_dict("records"):
            industry = str(row.get("industry") or "").strip()
            if not industry:
                continue
            _, ts_code, _ = normalize_code(str(row["code"]))
            values.append((industry, ts_code))
        with connect(path) as connection:
            connection.executemany(
                "UPDATE stocks SET industry=?,updated_at=CURRENT_TIMESTAMP WHERE ts_code=?",
                values,
            )
        return len(values)

    def sync_price_history(self, path, code: str, start_date: str, end_date: str) -> int:
        _, ts_code, bao_code = normalize_code(code)
        with _baostock_session() as bs:
            result = bs.query_history_k_data_plus(
                bao_code, "date,open,high,low,close,preclose,volume,amount,pctChg",
                start_date=pd.Timestamp(start_date).strftime("%Y-%m-%d"),
                end_date=pd.Timestamp(end_date).strftime("%Y-%m-%d"), frequency="d", adjustflag="2",
            )
            frame = _result_frame(result)
        if frame.empty:
            raise RuntimeError("BaoStock 历史行情返回空数据")
        frame = frame.rename(columns={"date": "trade_date", "preclose": "pre_close", "volume": "vol", "pctChg": "pct_chg"})
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y%m%d")
        frame["ts_code"] = ts_code
        numeric = ["open", "high", "low", "close", "pre_close", "vol", "amount", "pct_chg"]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        frame["vol"] = frame["vol"] / 100
        frame["amount"] = frame["amount"] / 1000
        return upsert_records(path, "daily_prices", frame.to_dict("records"),
                              ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"])

    def fetch_financial_indicators(self, code: str, quarters_back: int = 8, session=None) -> list[dict]:
        _, ts_code, bao_code = normalize_code(code)
        combined: dict[tuple[str, str], dict] = {}
        def fetch(bs) -> None:
            today = date.today()
            # Use the latest quarter whose reporting window should be broadly
            # complete, avoiding requests for future/unpublished quarters.
            if today.month <= 4:
                latest_period = pd.Period(f"{today.year - 1}Q3", freq="Q")
            elif today.month <= 8:
                latest_period = pd.Period(f"{today.year}Q1", freq="Q")
            elif today.month <= 10:
                latest_period = pd.Period(f"{today.year}Q2", freq="Q")
            else:
                latest_period = pd.Period(f"{today.year}Q3", freq="Q")
            periods = reversed([latest_period - offset for offset in range(quarters_back)])
            for period in periods:
                frames = (
                    _result_frame(bs.query_profit_data(code=bao_code, year=period.year, quarter=period.quarter)),
                    _result_frame(bs.query_growth_data(code=bao_code, year=period.year, quarter=period.quarter)),
                    _result_frame(bs.query_balance_data(code=bao_code, year=period.year, quarter=period.quarter)),
                    _result_frame(bs.query_cash_flow_data(code=bao_code, year=period.year, quarter=period.quarter)),
                )
                for frame in frames:
                    if not frame.empty:
                        row = frame.iloc[0].to_dict()
                        combined.setdefault((row["pubDate"], row["statDate"]), {}).update(row)

        if session is None:
            with _baostock_session() as bs:
                fetch(bs)
        else:
            fetch(session)
        records = []
        number = lambda value: pd.to_numeric(value, errors="coerce")
        for (published, period), row in combined.items():
            records.append({
                "ts_code": ts_code,
                "ann_date": published.replace("-", ""), "end_date": period.replace("-", ""),
                "roe": number(row.get("roeAvg")) * 100,
                "debt_to_assets": number(row.get("liabilityToAsset")) * 100,
                "revenue_yoy": None,
                "netprofit_yoy": number(row.get("YOYNI")) * 100,
                "ocf_to_or": number(row.get("CFOToOR")) * 100,
                "grossprofit_margin": number(row.get("gpMargin")) * 100,
            })
        return records

    def sync_financial_indicators(self, path, code: str, years_back: int = 2) -> int:
        records = self.fetch_financial_indicators(code, quarters_back=max(4, years_back * 4))
        return upsert_records(path, "financial_indicators", records,
                              ["ts_code", "ann_date", "end_date", "roe", "debt_to_assets", "revenue_yoy", "netprofit_yoy", "ocf_to_or", "grossprofit_margin"])


def sync_with_fallback(path, code: str, start_date: str, end_date: str) -> tuple[str, int]:
    errors = []
    for source in (AkShareSource(), BaoStockSource()):
        try:
            return source.name, source.sync_price_history(path, code, start_date, end_date)
        except Exception as error:
            errors.append(f"{source.name}: {error}")
    raise RuntimeError("；".join(errors))
