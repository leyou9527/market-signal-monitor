# Market Signal Monitor（标普500 + 科技股）

这个项目用于每天监控：

- 标普500指数 `GSPC`
- `MSFT NVDA META AAPL GOOGL AMZN TSM AVGO` 这 8 家科技公司

脚本会在检测到新的“已完成美股市场日”后发送一封日报邮件。如果某个监控条件被触发，邮件里会对对应项目打上预警标记；如果没有触发，就发送普通日报，不额外标红。

补充说明：

- 如果你在美股收盘前手动执行 `--force-send`，系统仍然会发一封“盘中快照”
- 盘中快照不会阻止收盘后的正式日报继续发送

## 监控规则

当前一共监控 3 个维度：

- 标普500指数相对最近 `60` 个交易日最高收盘价的回撤是否达到或超过 `7%`
- 标普500指数的 `YTD` 跌幅是否达到或超过 `7%`
- `MSFT NVDA META AAPL GOOGL AMZN TSM AVGO` 任意一只相对最近 `60` 个交易日最高收盘价的回撤是否达到或超过 `20%`

只要任意一个维度触发，就会发出一封汇总邮件；邮件里会同时包含：

- 标普500指数的 60 日回撤
- 标普500指数的 YTD
- 8 家科技公司的完整概览

## 数据源策略

为了尽量避免单一数据源抽风导致漏发，脚本使用分层兜底：

- 指数：`FMP -> EODHD -> Yahoo -> Stooq`
- 8 只股票：`FMP -> Twelve Data -> EODHD -> Yahoo -> Stooq`

补充说明：

- 本地缓存只作为诊断快照保存，不再拿来冒充“当天真实行情”
- 同一个标的只会选用一个 provider 的完整日线，不会把不同 provider 的 OHLC 字段硬拼在一起
- 脚本会先为每个标的收集多家 provider 候选结果，再统一选出“最新已完成市场日”的可用快照
- 如果当天所有在线数据源都失败，脚本会发一封“监控异常通知邮件”
- 如果图表生成失败，脚本也会先把文字版日报发出去，不让整封邮件因为图表挂掉而失败

## 邮件内容

每封日报会包含：

- 当天是否有触发项
- 标普500指数最新收盘、今日涨跌幅、60 日回撤、YTD
- 标普500指数最近 1 年最高收盘、最低收盘、以及当前距离近 1 年最高点的回撤
- 标普500指数最近 60 个交易日 K 线图
- 8 家科技公司的最新价、今日涨跌幅、60 日回撤、YTD
- 8 家科技公司各自最近 1 年最高/最低收盘，以及当前距离近 1 年最高点的回撤
- 一行简短的数据源摘要，方便排查当天实际走了哪条兜底链路

邮件样式以手机阅读为优先，适合在 iPhone 的 Gmail 客户端里查看。

## 目录说明

- `market_monitor.py`：正式监控脚本
- `.env.example`：配置模板
- `requirements.txt`：Python 依赖
- `deploy/market-signal-monitor.service`：systemd 服务模板
- `deploy/market-signal-monitor.timer`：systemd 定时器模板
- `longbridge_probe.py`：长桥开放平台测试脚本，仅做单独验证，不参与正式链路

## 本地或 VPS 初始化

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

然后把 `.env` 里的 SMTP 和 API key 补齐。

## 关键配置

常用变量如下：

- `INDEX_LOOKBACK_DAYS`：指数回看窗口，默认 `60`
- `INDEX_60D_DROP_THRESHOLD_PCT`：指数 60 日回撤阈值，默认 `7`
- `INDEX_YTD_DROP_THRESHOLD_PCT`：指数 YTD 跌幅阈值，默认 `7`
- `STOCK_LOOKBACK_DAYS`：个股回看窗口，默认 `60`
- `STOCK_60D_DROP_THRESHOLD_PCT`：个股 60 日回撤阈值，默认 `20`
- `TECH_STOCKS`：股票列表，默认 `MSFT,NVDA,META,AAPL,GOOGL,AMZN,TSM,AVGO`
- `REQUEST_TIMEOUT_SECONDS`：请求超时秒数，默认 `20`
- `REQUEST_RETRIES`：单个数据源的重试次数，默认 `3`
- `REQUEST_RETRY_DELAY_SECONDS`：重试间隔秒数，默认 `2`
- `FMP_API_KEY`：可选但强烈建议填写
- `TWELVEDATA_API_KEY`：可选但强烈建议填写
- `EODHD_API_KEY`：可选但强烈建议填写
- `EODHD_STOCK_EXCHANGE_CODE`：默认 `US`
- `EODHD_INDEX_SYMBOLS`：默认 `GSPC.INDX,SPX.INDX,SP500.INDX,^GSPC.INDX`
- `CACHE_DIR`：诊断缓存目录，不作为当天正式行情来源
- `LOG_FILE`：日志文件路径，默认 `/opt/market-signal-monitor/logs/market-signal-monitor.log`
- `STATE_FILE`：状态文件路径，默认 `data/state.json`

说明：

- 新部署优先使用 `INDEX_*` 配置字段，名称与当前“指数维度”逻辑一致
- 程序仍兼容旧版 `NDX_*` 字段，因此现有 VPS 的 `.env` 不需要立刻迁移
- 如果你希望稳定拿到标普500指数的最新收盘数据，强烈建议配置 `EODHD_API_KEY`

邮件发送相关：

- `EMAIL_PROVIDER`：可选，`resend` 或 `smtp`
- `RESEND_API_KEY`：使用 Resend 时填写
- `RESEND_FROM`：使用 Resend 时的发件人，例如 `Market Monitor <report@example.com>`
- `RESEND_REQUESTS_PER_SECOND`：Resend 主动限速，默认 `4` 次/秒
- `RESEND_MAX_ATTEMPTS`：429、5xx 或临时网络错误的总尝试次数，默认 `4`
- `RESEND_RETRY_DELAY_SECONDS`：首次重试等待时间，默认 `1` 秒，后续采用指数退避
- `SMTP_RECIPIENT`：主收件人，会显示在 To 里
- `SMTP_BCC`：可选，多个邮箱用英文逗号分隔；Resend 模式下会逐个单独发送，每个人的 To 都是自己；单个地址失败不会阻断其余地址

SMTP 备用配置：

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_SENDER`
- `SMTP_USE_SSL`
- `SMTP_USE_STARTTLS`

常见 SMTP 示例：

- QQ 邮箱：`SMTP_HOST=smtp.qq.com`，`SMTP_PORT=465`，`SMTP_USE_SSL=true`
- Gmail：`SMTP_HOST=smtp.gmail.com`，`SMTP_PORT=587`，`SMTP_USE_STARTTLS=true`
- Outlook：`SMTP_HOST=smtp.office365.com`，`SMTP_PORT=587`，`SMTP_USE_STARTTLS=true`

## 手动运行

```bash
python market_monitor.py --dry-run
python market_monitor.py --send-test-email
python market_monitor.py
```

如果你想忽略“今天是否已经发过”的状态，强制补跑一次：

```bash
python market_monitor.py --force-send
```

补充说明：

- `--force-send` 适合手动补跑或临时检查
- 如果执行时美股尚未收盘，邮件会以“盘中快照”形式发出
- 收盘后的同一市场日期正式日报仍会照常发送，不会被这封盘中快照顶掉

## 部署到 Linux VPS

1. 把项目上传到 VPS，例如 `/opt/market-signal-monitor`
2. 创建虚拟环境并安装依赖
3. 拷贝 `.env.example` 为 `.env`
4. 填好 SMTP 和 API key
5. 安装 systemd service / timer

示例：

```bash
cd /opt/market-signal-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
sudo cp deploy/market-signal-monitor.service /etc/systemd/system/
sudo cp deploy/market-signal-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now market-signal-monitor.timer
systemctl list-timers market-signal-monitor.timer
```

当前定时器逻辑是：

- 每天本地时间 `07:50` 开始
- 随机延迟最多 `20` 分钟
- 所以实际发信时间会落在 `07:50 - 08:10`
- service 是一次性任务；被 timer 拉起、运行完成后显示 `inactive (dead)` 且 `status=0/SUCCESS` 属于正常现象

如果你的 VPS 按中国时间使用，建议先设置时区：

```bash
timedatectl set-timezone Asia/Shanghai
```

## 中文字体

如果邮件里的图表需要正常显示中文，Ubuntu / Debian 建议安装：

```bash
apt update
apt install -y fonts-noto-cjk
fc-cache -fv
```

## 日志查看

脚本会同时写：

- `systemd journal`
- 本地日志文件

常用命令：

```bash
tail -f /opt/market-signal-monitor/logs/market-signal-monitor.log
tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log
journalctl -u market-signal-monitor.service -n 50 --no-pager
systemctl status market-signal-monitor.timer --no-pager
systemctl status market-signal-monitor.service --no-pager
```

## 排查思路

如果某天没收到邮件，优先按这个顺序查：

1. `systemctl list-timers market-signal-monitor.timer`
2. `systemctl status market-signal-monitor.service --no-pager`
3. `tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log`
4. `journalctl -u market-signal-monitor.service --since "today 07:40" --no-pager`

常见几类结果：

- `Daily email sent.`：日报已发出
- `No new U.S. market session data is available yet; skip sending.`：没有新的“已完成美股市场日”，自动跳过
- `Intraday snapshot for the current U.S. market session was already sent; skip sending.`：同一个市场日期的盘中快照当天已经发过，不再重复发盘中版本
- `Monitor run failed: ...`：数据源或脚本异常
- `Failure notice email sent.`：正式日报失败，但异常通知邮件已经发出

## 当前对长桥的结论

`longbridge_probe.py` 已验证：

- 8 家科技公司的美股日线可以正常取到
- 但标普500指数在长桥里的 symbol 目前还没确认

再加上长桥 `Access Token` 有有效期，所以目前不建议把长桥接入正式监控链路。现阶段把它作为备选方案保留更稳妥。
