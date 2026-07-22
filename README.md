# 每日伏击股

一个完全独立的本地 Streamlit 项目，只实现综合伏击模式。候选股票必须同时满足：

- 最强3个板块之一
- 大单资金净流入、非大单资金净流出
- 当日涨幅低于5%且不低于-3%
- 近3日资金为正；备用来源缺失时使用近期量价承接验证
- 量比1.1～2.5、60日位置不高于75%
- 20日平均成交额不低于1亿元
- 中期趋势未破坏、上影和短期涨幅不过热
- 排除30开头、688、北交所、ST和退市风险股票

## 运行

```bash
cd /home/robotera/daily-ambush-stock
python3 -m pip install -r requirements.txt
streamlit run app.py --server.address 127.0.0.1
```

## 后台运行

立即执行一次并推送微信：

```bash
python3 -m stock_assistant.runner --once
```

安装常驻服务：

```bash
mkdir -p ~/.config/systemd/user
cp daily-ambush-stock.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now daily-ambush-stock
```

默认在交易日 `09:45、10:05、11:20、14:00、14:45` 扫描。前四次跟踪新进入、持续入选和已退出股票；`14:45` 结合当前策略评分、全天出现次数、尾盘资金强度和板块持续性，生成 1—3 只最终候选。盘中名单未变时不重复推送，最终名单始终推送。收盘后自动维护日线。扫描时点可在 `.env` 修改。

正式候选还会通过数据日期/覆盖率、真实交易日、市场退潮、ST/新股/流动性、已知风险公告和可用基本面数据的否决检查。退潮或数据不完整时只保留观察池。同一股票首次掉出标记为“待恢复”，连续两次掉出才退出。错过的扫描可在有效时限内补跑并明确标注；微信使用幂等键和本地重试队列，收盘后发送服务健康日报。

查看服务：

```bash
systemctl --user status daily-ambush-stock
journalctl --user -u daily-ambush-stock -f
```

数据库位于本项目的 `data/stock_assistant.db`，与原平台完全独立，不会提交到Git。Web页面仅用于查看每次推荐历史。

所有市场数据源均免费且无需 API Token。盘后日线优先使用通达信公开行情节点，多节点自动切换；通达信失败时使用腾讯 K 线补漏。BaoStock 仅保留为低频手动备用和可用财务数据源，不再阻塞每日行情。盘中资金优先使用东方财富公开接口；异常时降级到新浪大单代理和行业资金。公告使用巨潮资讯和东方财富双源校验。

本项目不连接券商、不会自动下单，仅用于研究，不构成投资建议。
