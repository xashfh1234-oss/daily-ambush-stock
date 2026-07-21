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

默认在交易日上午、下午每30分钟执行一次；每次同步市场资金和板块、生成严格交集候选、保存历史并推送微信。收盘后自动维护日线。参数可在 `.env` 修改。

查看服务：

```bash
systemctl --user status daily-ambush-stock
journalctl --user -u daily-ambush-stock -f
```

数据库位于本项目的 `data/stock_assistant.db`，与原平台完全独立，不会提交到Git。Web页面仅用于查看每次推荐历史。

盘中资金优先使用东方财富公开接口；接口异常时自动降级到新浪大单、个股净额和行业资金，并保留最近成功快照。

本项目不连接券商、不会自动下单，仅用于研究，不构成投资建议。
