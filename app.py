from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from stock_assistant.background_jobs import active_job, latest_job, request_stop, start_job, touch_heartbeat
from stock_assistant.config import settings
from stock_assistant.database import backup_database, initialize, query
from stock_assistant.intraday import build_intraday_candidates, latest_intraday_run, sync_intraday_data
from stock_assistant.market import market_as_of_date, price_frame


st.set_page_config(page_title="A股综合伏击雷达", page_icon="🎯", layout="wide")
backup_database(settings.database_path)
initialize(settings.database_path)
st.markdown("""
<style>
  .block-container {padding-top: 1.5rem; max-width: 1500px;}
  .hero {padding: 22px 24px; border-radius: 16px; margin-bottom: 18px; background: linear-gradient(120deg, rgba(19,112,89,.20), rgba(28,79,145,.13)); border: 1px solid rgba(60,140,120,.25);}
  .hero h1 {font-size: 1.65rem; margin: 0 0 8px 0;}
  .hero p {margin: 0; opacity: .8;}
  [data-testid="stMetric"] {background: rgba(128,128,128,.06); border: 1px solid rgba(128,128,128,.14); border-radius: 12px; padding: 12px;}
</style>
""", unsafe_allow_html=True)


@st.fragment(run_every=5)
def job_monitor():
    touch_heartbeat(settings.database_path)
    job = active_job(settings.database_path)
    if not job:
        return
    st.divider()
    st.write("后台行情同步")
    st.progress(min(job["current"] / job["total"], 1.0) if job["total"] else 0)
    st.caption(f"{job['current']}/{job['total']} · {job['current_code'] or '准备中'}")
    if st.button("停止任务", key=f"stop_{job['id']}"):
        request_stop(settings.database_path)


with st.sidebar:
    st.markdown("## 综合伏击雷达")
    st.caption("独立数据库 · 不连接券商")
    initialize_universe = st.button("1. 初始化股票与行业", width="stretch")
    running = active_job(settings.database_path)
    initialize_prices = st.button("2. 初始化/续传日线", disabled=bool(running), width="stretch")
    update_prices = st.button("更新最新可用日线", disabled=bool(running), width="stretch")
    refresh_intraday = st.button("3. 同步盘中资金", type="primary", width="stretch")
    job_monitor()


if initialize_universe:
    from stock_assistant.data_sources import AkShareSource, BaoStockSource
    try:
        with st.spinner("正在建立独立股票和行业数据……"):
            stocks = AkShareSource().sync_stocks(settings.database_path)
            industries = BaoStockSource().sync_industries(settings.database_path)
        st.success(f"股票{stocks}只，行业分类{industries}条。下一步点击“初始化/续传日线”。")
    except Exception as error:
        st.error(f"初始化失败：{error}")

if initialize_prices:
    try:
        job_id = start_job(settings.database_path, "MARKET", 100_000)
        st.success(f"日线任务 #{job_id} 已启动；保持浏览器打开，可切换标签页。")
    except Exception as error:
        st.error(str(error))

if update_prices:
    from stock_assistant.batch_sync import queue_incremental_update
    try:
        queue_incremental_update(settings.database_path)
        job_id = start_job(settings.database_path, "MARKET", 100_000)
        st.success(f"最新日线任务 #{job_id} 已启动。")
    except Exception as error:
        st.error(str(error))

if refresh_intraday:
    with st.spinner("正在同步全市场资金和板块数据，约需20～40秒……"):
        result = sync_intraday_data(settings.database_path)
    if result["status"] == "COMPLETED":
        st.success(f"盘中同步完成：资金{result['money']}只，板块{result['sector']}个。")
    elif result["status"] == "PARTIAL":
        st.warning("部分接口不可用，已自动使用备用来源或最近成功快照。")
    else:
        st.error("盘中接口暂时不可用，稍后重试。")

st.markdown("""
<div class="hero">
  <h1>综合伏击候选</h1>
  <p>只有同时满足强势板块、资金背离、涨幅、量能、位置、趋势和流动性要求的股票才会出现。</p>
</div>
""", unsafe_allow_html=True)

eligible = query(settings.database_path, "SELECT COUNT(*) n FROM stock_sync_status WHERE eligible=1")[0]["n"]
priced = query(settings.database_path, "SELECT COUNT(DISTINCT ts_code) n FROM daily_prices")[0]["n"]
last_run = latest_intraday_run(settings.database_path)
c1, c2, c3, c4 = st.columns(4)
c1.metric("统一日线日期", market_as_of_date(settings.database_path) or "暂无")
c2.metric("本地行情股票", int(priced or 0))
c3.metric("合格股票", int(eligible or 0))
c4.metric("盘中快照", last_run["snapshot_at"][5:16].replace("T", " ") if last_run else "暂无")
if last_run and last_run.get("message"):
    with st.expander("数据源与降级说明"):
        st.write(last_run["message"])

strategies = build_intraday_candidates(settings.database_path)
candidates = strategies["综合伏击候选"]
sectors = strategies["强势板块"]
if not sectors.empty:
    st.caption("当前强势板块：" + " · ".join(sectors["sector_name"].astype(str)))

if candidates.empty:
    st.info("当前没有同时满足全部条件的股票。首次使用请依次完成左侧1、2、3步；严格交集为空属于正常结果。")
else:
    table = candidates.rename(columns={
        "ts_code": "股票代码", "name": "股票名称", "industry": "所属行业", "strategy_score": "综合评分",
        "price": "现价", "pct_chg": "当日涨幅", "main_net": "大单净流入", "small_net": "非大单净流入",
        "three_day_main": "3日主力净流入", "volume_ratio": "量比", "position60": "60日位置",
        "confirm_price": "确认价格", "invalid_price": "失效价格", "source": "资金来源", "reason": "全部条件",
    })
    columns = ["股票代码", "股票名称", "所属行业", "综合评分", "现价", "当日涨幅", "大单净流入", "非大单净流入", "3日主力净流入", "量比", "60日位置", "确认价格", "失效价格", "资金来源", "全部条件"]
    event = st.dataframe(
        table[columns], width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
        column_config={
            "综合评分": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
            "60日位置": st.column_config.NumberColumn(format="%.1%%"),
            "大单净流入": st.column_config.NumberColumn(format="%.0f 元"),
            "非大单净流入": st.column_config.NumberColumn(format="%.0f 元"),
        },
    )
    if event.selection.rows:
        selected = candidates.iloc[event.selection.rows[0]]
        st.subheader(f"{selected['name']} · {selected['ts_code']}")
        frame = price_frame(settings.database_path, selected["ts_code"]).tail(250).copy()
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            chart = go.Figure(go.Candlestick(x=frame["trade_date"], open=frame["open"], high=frame["high"], low=frame["low"], close=frame["close"], name="日K"))
            chart.add_hline(y=selected["confirm_price"], line_dash="dash", line_color="#e6a23c", annotation_text="确认价")
            chart.add_hline(y=selected["invalid_price"], line_dash="dot", line_color="#d94f4f", annotation_text="失效价")
            chart.update_layout(height=520, xaxis_rangeslider_visible=False)
            st.plotly_chart(chart, width="stretch")

st.caption("仅用于研究，不构成投资建议。“大单/非大单”是行情平台按成交规模划分的代理口径，不代表真实账户身份。")
