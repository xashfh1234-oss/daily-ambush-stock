import pandas as pd
import streamlit as st

from stock_assistant.config import settings
from stock_assistant.database import initialize, query


st.set_page_config(page_title="每日伏击股 · 历史", page_icon="🎯", layout="wide")
initialize(settings.database_path)
st.markdown("""
<style>
  .block-container {padding-top: 1.5rem; max-width: 1500px;}
  .hero {padding: 22px 24px; border-radius: 16px; margin-bottom: 18px; background: linear-gradient(120deg, rgba(19,112,89,.20), rgba(28,79,145,.13)); border: 1px solid rgba(60,140,120,.25);}
  .hero h1 {font-size: 1.65rem; margin: 0 0 8px 0;}
  .hero p {margin: 0; opacity: .8;}
  [data-testid="stMetric"] {background: rgba(128,128,128,.06); border: 1px solid rgba(128,128,128,.14); border-radius: 12px; padding: 12px;}
</style>
<div class="hero"><h1>每日伏击股 · 历史记录</h1><p>后台服务自动同步、筛选并推送微信；本页面只读，不会触发任何任务。</p></div>
""", unsafe_allow_html=True)

runs = [dict(row) for row in query(settings.database_path, "SELECT * FROM recommendation_runs ORDER BY id DESC LIMIT 200")]
latest = runs[0] if runs else None
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("最近执行", latest["run_at"][5:16].replace("T", " ") if latest else "暂无")
c2.metric("任务状态", latest["status"] if latest else "暂无")
c3.metric("最近候选", latest["candidate_count"] if latest else 0)
c4.metric("微信推送", "成功" if latest and latest["pushed"] else "未推送")
c5.metric("数据可信度", f"{float(latest.get('data_confidence') or 0):.0%}" if latest else "--")

if latest:
    status_text = f"数据 {latest.get('data_status') or '未知'} · {'允许正式候选' if latest.get('recommendation_allowed') else '仅观察'}"
    (st.success if latest.get("recommendation_allowed") else st.warning)(status_text)

if latest and latest.get("error"):
    st.error(latest["error"])
if latest and latest.get("message"):
    with st.expander("最近一次微信内容", expanded=True):
        st.text(latest["message"])

st.subheader("推荐历史")
if not runs:
    st.info("后台服务尚未产生记录。可以先运行 `python3 -m stock_assistant.runner --once --no-push` 进行验证。")
else:
    history = pd.DataFrame(runs).rename(columns={
        "id": "任务ID", "run_at": "执行时间", "status": "状态", "market_state": "市场状态",
        "up_ratio": "上涨占比", "median_pct": "涨跌中位数", "candidate_count": "候选数量",
        "pushed": "已推送", "error": "错误",
        "slot_label": "扫描时点", "is_final": "最终名单", "is_catchup": "补跑",
        "data_status": "数据状态", "data_confidence": "数据可信度",
        "recommendation_allowed": "允许推荐", "change_summary": "变化摘要", "push_status": "推送状态",
    })
    event = st.dataframe(
        history[["任务ID", "执行时间", "扫描时点", "最终名单", "补跑", "状态", "市场状态", "数据状态", "数据可信度", "允许推荐", "候选数量", "变化摘要", "推送状态", "错误"]],
        width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
        column_config={"数据可信度": st.column_config.NumberColumn(format="%.0%%")},
    )
    run_id = int(history.iloc[event.selection.rows[0]]["任务ID"]) if event.selection.rows else int(history.iloc[0]["任务ID"])
    items = [dict(row) for row in query(settings.database_path, "SELECT * FROM recommendation_items WHERE run_id=? ORDER BY rank_no", (run_id,))]
    st.subheader(f"任务 #{run_id} 候选详情")
    if not items:
        st.caption("该次执行没有同时满足全部条件的股票。")
    else:
        table = pd.DataFrame(items).rename(columns={
            "rank_no": "排名", "ts_code": "股票代码", "name": "股票名称", "industry": "所属行业",
            "score": "评分", "price": "价格", "pct_chg": "涨幅", "main_net": "大单净流入",
            "small_net": "非大单净流入", "volume_ratio": "量比", "position60": "60日位置",
            "confirm_price": "确认价", "invalid_price": "失效价", "source": "数据来源", "reason": "条件",
            "appearance_count": "出现次数", "lifecycle": "生命周期", "final_score": "最终评分",
            "data_confidence": "个股数据可信度", "active": "当前满足", "formal": "正式候选", "exit_reason": "退出/待恢复原因",
        })
        st.dataframe(table[["排名", "生命周期", "出现次数", "当前满足", "正式候选", "股票代码", "股票名称", "所属行业", "评分", "最终评分", "个股数据可信度", "价格", "涨幅", "大单净流入", "非大单净流入", "量比", "60日位置", "确认价", "失效价", "数据来源", "退出/待恢复原因", "条件"]], width="stretch", hide_index=True)

    st.subheader("当日候选变化")
    today = str(history.iloc[0]["执行时间"])[:10]
    timeline = history[history["执行时间"].astype(str).str.startswith(today)].copy().sort_values("执行时间")
    if not timeline.empty:
        st.line_chart(timeline.set_index("执行时间")[["候选数量"]])

st.subheader("数据与服务健康")
sync = [dict(row) for row in query(settings.database_path, "SELECT * FROM intraday_sync_runs ORDER BY id DESC LIMIT 1")]
outbox = query(settings.database_path, "SELECT status,COUNT(*) n FROM push_outbox GROUP BY status")
if sync:
    st.json({"最近快照": sync[0], "微信队列": {row["status"]: row["n"] for row in outbox}}, expanded=False)

st.caption("仅供研究，不构成投资建议。后台服务和微信推送状态请使用 systemctl --user status daily-ambush-stock 查看。")
