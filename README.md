# Market Signal Monitor

Market Signal Monitor 是一个面向长期无人值守运行的美股日报与回撤预警服务。目前监控：

- 标普 500 指数 `GSPC`
- `MSFT`、`NVDA`、`META`、`AAPL`、`GOOGL`、`AMZN`、`TSM`、`AVGO` 八家科技公司

程序会在检测到新的、已经完成的美股市场日后生成完整日报。达到阈值时，邮件会标记对应预警；没有达到阈值时仍会发送普通日报。系统支持 Resend 和传统 SMTP，生产环境推荐使用 Resend。

> 安全原则：`.env`、API Key、真实发件地址和真实收件地址只保存在部署环境，不得提交到 GitHub。本 README 使用占位符，但完整保留配置方法和运维信息。

## 目录

- [监控逻辑](#监控逻辑)
- [日报内容](#日报内容)
- [数据一致性与状态设计](#数据一致性与状态设计)
- [数据源策略与已知情况](#数据源策略与已知情况)
- [邮件投递与收件人隐私](#邮件投递与收件人隐私)
- [项目结构](#项目结构)
- [配置说明](#配置说明)
- [本地运行](#本地运行)
- [完整 VPS 部署](#完整-vps-部署)
- [systemd 定时任务](#systemd-定时任务)
- [日常运维](#日常运维)
- [故障排查](#故障排查)
- [谨慎修改事项](#谨慎修改事项)
- [迁移与交接检查清单](#迁移与交接检查清单)
- [长桥探测脚本](#长桥探测脚本)
- [安全与开源检查](#安全与开源检查)
- [许可证与免责声明](#许可证与免责声明)

## 监控逻辑

系统检查三个维度：

1. 标普 500 指数相对最近 `60` 个交易日最高收盘价的回撤是否达到或超过 `7%`
2. 标普 500 指数年初至今（YTD）跌幅是否达到或超过 `7%`
3. 八家科技公司中，是否有任意一只相对最近 `60` 个交易日最高收盘价的回撤达到或超过 `20%`

这些阈值都可以通过 `.env` 调整。只要任意一项触发，日报会突出显示触发项，但邮件仍会包含指数和全部八只股票，而不是只发送触发标的。

### 正式日报与盘中快照

- 自动定时任务以“新的已完成美股市场日”为正式发送口径。
- 在美股收盘前手动执行 `--force-send` 时，会发送一封盘中快照。
- 盘中快照与收盘后正式日报分别去重；盘中发过不会阻止当天收盘后的正式日报。
- 同一市场日期已经发送过盘中快照时，不会重复发送第二封盘中快照。

## 日报内容

每封日报固定包含：

- 当天是否存在触发项
- 标普 500 指数最新收盘、今日涨跌幅、60 日回撤和 YTD
- 标普 500 指数近一年最高收盘、最低收盘及距离一年高点的回撤
- 标普 500 指数最近 60 个交易日 K 线图
- 八家科技公司的最新价、今日涨跌幅、60 日回撤和 YTD
- 八家公司各自近一年最高/最低收盘及距离一年高点的回撤
- 一行数据源摘要，说明本次指数和个股最终使用了哪些 provider

邮件样式以手机阅读为优先，适合在 iPhone、Gmail 等客户端查看。如果图表生成失败，系统会先发送文字版日报，避免整封邮件因为图片失败而丢失。

## 数据一致性与状态设计

这套服务的目标不是“每天勉强凑一封邮件”，而是保证行情口径可信、故障可见、去重可靠。

关键设计如下：

1. 各数据源返回的数据先统一为同一套 `Bar` 结构。
2. 指标统一通过 `build_metric()` 计算，避免不同 provider 使用不同口径。
3. 同一标的只会使用一个 provider 的完整 OHLC 日线，不拼接不同 provider 的字段。
4. 程序会先收集多个 provider 的候选结果，再选择共同可用的市场日期。
5. 如果不同标的在已经完成的市场日上无法对齐，宁可报错，也不会把不同日期混进同一封正式日报。
6. 本地缓存只用于诊断，不会冒充当天正式行情。
7. 只有邮件真正发送成功后，才更新“该市场日已发送”的状态。
8. 如果所有在线数据源都失败，系统发送异常通知，而不是用旧缓存生成假日报。
9. 涨跌颜色、正负号、数据源摘要和邮件样式通过公共 helper 统一维护。

状态文件默认位于：

```text
/opt/market-signal-monitor/data/state.json
```

状态文件主要记录：

- 最近成功处理的正式市场日期
- 当天盘中快照的发送状态

不要把状态写入提前到发信之前，否则可能出现“邮件实际失败，但系统认为今天已经发过”的隐蔽故障。

## 数据源策略与已知情况

当前兜底顺序：

- 指数：`FMP -> EODHD -> Yahoo -> Stooq`
- 八只股票：`FMP -> Twelve Data -> EODHD -> Yahoo -> Stooq`

### FMP

- 已接入指数和个股链路。
- 某些 Key 或套餐会返回 `402`、`403`。
- 这类权限或套餐错误被视为不可重试错误，程序会立即切换下一个 provider，避免无意义重试。

### Twelve Data

- 当前主要用于八只科技股，不用于指数。
- 存在分钟级请求额度限制。
- 超额或明确无效请求会快速切换到后续 provider。

### EODHD

- 同时支持标普 500 指数和个股。
- 对最新已完成市场日的补齐能力通常优于免费网页数据源。
- 强烈建议配置 `EODHD_API_KEY`；缺少它会降低指数最新收盘数据的稳定性。

### Yahoo

- 覆盖面广，指数和个股经常可以成功兜底。
- 不是带 SLA 的正式商业 API，可能偶尔限流或返回异常。

### Stooq

- 保留为最后兜底。
- 历史上出现过 `200 OK` 但响应正文为空，因此不能作为唯一数据源。

### 缓存

- 缓存目录默认是 `/opt/market-signal-monitor/data/cache`。
- 缓存只保存诊断快照。
- 当天在线数据源全部失败时，不会退回缓存发送正式日报。

## 邮件投递与收件人隐私

### Resend（推荐）

生产环境使用 Resend 时，示例配置如下：

```env
EMAIL_PROVIDER=resend
RESEND_API_KEY=你的_Resend_API_Key
RESEND_FROM=Market Monitor <report@your-domain.example>
RESEND_REQUESTS_PER_SECOND=4
RESEND_MAX_ATTEMPTS=4
RESEND_RETRY_DELAY_SECONDS=1
EMAIL_RECIPIENTS=owner@example.com,employee1@example.com,employee2@example.com
```

`EMAIL_RECIPIENTS` 是统一的收件人配置入口。多个地址用英文逗号分隔；程序解析并去重后，逐个调用 Resend API：

- 每个人收到一封独立邮件。
- 每封邮件的 `To` 只包含当前收件人自己。
- 收件人互相看不到彼此。
- 默认主动限速为每秒 4 个请求，低于 Resend 常见的每秒 5 请求限制。
- 遇到 `429`、`5xx` 或临时网络错误时采用指数退避，默认最多尝试 4 次。
- 同一收件人的重试复用同一个幂等键，降低网络结果不明确时重复投递的风险。
- 单个地址永久失败不会阻断后续地址；全部处理完成后统一汇总失败并以失败状态退出。
- 任何异常通知只发送给 `EMAIL_RECIPIENTS` 去重后的第一个地址；其余正常日报收件人不会收到内部故障通知。

当前生产部署的运维事实（不公开真实地址）：

- 使用一个已经在 Resend 验证的自有域名。
- Sending 已启用。
- Open Tracking 和 Click Tracking 已关闭。
- DNS 已配置 DMARC、DKIM、SPF 和 feedback MX。
- 当前正式收件人共 6 个，真实地址只保存在 VPS 的 `.env`。
- Resend 后台的测试投递记录显示为 `delivered`。
- Yahoo 测试邮箱进入收件箱；Gmail 对新发件域名仍可能判入垃圾邮件，这通常属于域名信誉问题，不代表 Resend API 发送失败。

如果 Gmail 仍进入垃圾箱，可在 Gmail 的“显示原文”中检查：

- `SPF`
- `DKIM`
- `DMARC`
- `Authentication-Results`

如果 Resend API Key 曾在聊天、日志或其他位置暴露，应在 Resend 后台轮换 Key，并同步更新 VPS 的 `.env`。

### 传统 SMTP（备用）

程序仍支持传统 SMTP：

```env
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=sender@example.com
SMTP_PASSWORD=邮箱授权码
SMTP_SENDER=sender@example.com
EMAIL_RECIPIENTS=owner@example.com,employee1@example.com,employee2@example.com
SMTP_USE_SSL=false
SMTP_USE_STARTTLS=true
```

SMTP 备用模式同样逐个独立投递，每封邮件的 `To` 只包含当前收件人，不使用共享的 To 或 BCC 头。

常见示例：

- QQ 邮箱：`SMTP_HOST=smtp.qq.com`、`SMTP_PORT=465`、`SMTP_USE_SSL=true`
- Gmail：`SMTP_HOST=smtp.gmail.com`、`SMTP_PORT=587`、`SMTP_USE_STARTTLS=true`
- Outlook：`SMTP_HOST=smtp.office365.com`、`SMTP_PORT=587`、`SMTP_USE_STARTTLS=true`

`SMTP_USE_SSL` 和 `SMTP_USE_STARTTLS` 不能同时为 `true`。

## 项目结构

```text
market-signal-monitor/
├─ market_monitor.py                         # 正式监控程序
├─ longbridge_probe.py                       # 长桥独立探测脚本，不参与正式链路
├─ requirements.txt                          # Python 依赖
├─ .env.example                              # 完整配置模板
├─ .gitignore                                # 密钥、状态、日志和本地文件忽略规则
├─ README.md                                 # 唯一项目文档
├─ deploy/
│  ├─ install_ubuntu.sh                      # Ubuntu/Debian 安装辅助脚本
│  ├─ market-signal-monitor.service          # systemd oneshot service
│  └─ market-signal-monitor.timer            # systemd timer
└─ tests/
   └─ test_resend_delivery.py                # Resend 限速、重试和失败隔离测试
```

运行时还会出现以下未提交目录或文件：

- `.env`：真实配置和密钥
- `.venv/`：Python 虚拟环境
- `data/`：状态和诊断缓存
- `logs/`：本地日志
- `__pycache__/`：Python 缓存

## 配置说明

完整示例和中文注释位于 `.env.example`。常用配置如下。

### 指数和个股规则

- `INDEX_LOOKBACK_DAYS`：指数回看交易日数，默认 `60`
- `INDEX_60D_DROP_THRESHOLD_PCT`：指数滚动回撤阈值，默认 `7`
- `INDEX_YTD_DROP_THRESHOLD_PCT`：指数 YTD 跌幅阈值，默认 `7`
- `STOCK_LOOKBACK_DAYS`：个股回看交易日数，默认 `60`
- `STOCK_60D_DROP_THRESHOLD_PCT`：个股滚动回撤阈值，默认 `20`
- `TECH_STOCKS`：监控股票，默认 `MSFT,NVDA,META,AAPL,GOOGL,AMZN,TSM,AVGO`

### 请求和数据源

- `REQUEST_TIMEOUT_SECONDS`：请求超时秒数，默认 `20`
- `REQUEST_RETRIES`：单个 provider 的重试次数，默认 `3`
- `REQUEST_RETRY_DELAY_SECONDS`：重试间隔秒数，默认 `2`
- `FMP_API_KEY`：FMP Key，可选但建议配置
- `TWELVEDATA_API_KEY`：Twelve Data Key，建议配置
- `EODHD_API_KEY`：EODHD Key，强烈建议配置
- `EODHD_STOCK_EXCHANGE_CODE`：个股交易所代码，默认 `US`
- `EODHD_INDEX_SYMBOLS`：指数候选代码，默认 `GSPC.INDX,SPX.INDX,SP500.INDX,^GSPC.INDX`

`.INDX` 是 EODHD 的指数证券代码格式，与旧项目名称无关。

### 路径

- `LOG_FILE`：默认 `/opt/market-signal-monitor/logs/market-signal-monitor.log`
- `CACHE_DIR`：默认 `/opt/market-signal-monitor/data/cache`
- `STATE_FILE`：默认 `/opt/market-signal-monitor/data/state.json`

### 邮件

- `EMAIL_PROVIDER`：`resend` 或 `smtp`
- `RESEND_API_KEY`
- `RESEND_FROM`
- `RESEND_REQUESTS_PER_SECOND`：默认 `4`
- `RESEND_MAX_ATTEMPTS`：默认 `4`
- `RESEND_RETRY_DELAY_SECONDS`：默认 `1`
- `EMAIL_RECIPIENTS`：全部收件人，多个地址使用英文逗号分隔
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_SENDER`
- `SMTP_USE_SSL`
- `SMTP_USE_STARTTLS`

## 本地运行

### 初始化

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell 激活虚拟环境时可使用：

```powershell
.\.venv\Scripts\Activate.ps1
```

编辑 `.env`，填入数据源和邮件配置后运行。

### 常用命令

只拉行情、计算指标，不发送邮件：

```bash
python market_monitor.py --dry-run
```

发送测试邮件：

```bash
python market_monitor.py --send-test-email
```

正常执行：

```bash
python market_monitor.py
```

忽略发送状态并补跑：

```bash
python market_monitor.py --force-send
```

注意：`--send-test-email` 和 `--force-send` 都可能向 `.env` 中配置的全部真实收件人发送邮件；在生产环境操作前先确认收件人列表。

### 测试

```bash
python -m py_compile market_monitor.py tests/test_resend_delivery.py
python -m unittest discover -s tests -v
```

当前测试覆盖：

- Resend `429` 重试
- 同一收件人重试复用幂等键
- 单个永久失败不阻断后续收件人
- 请求主动限速

## 完整 VPS 部署

本文默认：

- VPS 系统为 Ubuntu 或 Debian
- 使用 root；非 root 用户在系统命令前加 `sudo`
- 部署目录为 `/opt/market-signal-monitor`
- 使用 systemd timer 定时运行
- 推荐使用 Resend，SMTP 作为备用

### 1. 准备事项

至少准备：

1. 一台可 SSH 登录的 VPS
2. 项目完整代码
3. Resend 或 SMTP 发信配置
4. 至少一个可用数据源 Key

推荐同时准备：

- `TWELVEDATA_API_KEY`
- `FMP_API_KEY`
- `EODHD_API_KEY`

即使 FMP 已配置，也应保留 Twelve Data 和 EODHD，因为 FMP 某些套餐会返回 `402/403`。

### 2. 登录并确认系统

```bash
ssh root@你的VPSIP
cat /etc/os-release
```

### 3. 设置时区

如果希望每天按中国时间运行：

```bash
timedatectl set-timezone Asia/Shanghai
timedatectl
```

应看到 `Time zone: Asia/Shanghai`。timer 文件本身也显式使用 `Asia/Shanghai`，设置系统时区可以让日志和人工排查时间保持一致。

### 4. 安装系统依赖和中文字体

```bash
apt update
apt install -y python3 python3-venv python3-pip fonts-noto-cjk
fc-cache -fv
```

`fonts-noto-cjk` 用于 K 线图中文；缺少字体时中文可能显示为方块。

### 5. 上传代码

推荐目录：

```text
/opt/market-signal-monitor
```

Windows PowerShell 示例：

```powershell
scp -r C:\path\to\market-signal-monitor root@你的VPSIP:/opt/
```

也可以通过 Git 拉取公开文件，然后单独安全传输 `.env`。无论使用哪种方式，VPS 至少需要：

- `market_monitor.py`
- `requirements.txt`
- `.env`
- `.env.example`
- `README.md`
- `deploy/market-signal-monitor.service`
- `deploy/market-signal-monitor.timer`
- `tests/test_resend_delivery.py`

`.env` 不应进入 Git，需要单独创建或传输。

### 6. 检查目录

```bash
cd /opt/market-signal-monitor
ls -la
```

至少应看到 `market_monitor.py`、`requirements.txt`、`.env` 和 `deploy/`。

### 7. 创建虚拟环境并安装依赖

```bash
cd /opt/market-signal-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

如需运行长桥探测脚本，可额外安装：

```bash
pip install longbridge
```

### 8. 创建并检查 `.env`

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

至少确认：

- 指数和个股规则使用 `INDEX_*`、`STOCK_*`
- Resend 或 SMTP 配置完整
- `EMAIL_RECIPIENTS` 包含完整且正确的收件人列表
- 至少一个在线数据源 Key 可用
- 日志、缓存和状态路径指向 `/opt/market-signal-monitor`

只打印配置键名、不打印真实值：

```bash
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env | cut -d= -f1
```

不要使用会把真实 Key 直接打印到共享终端或截图中的检查方式。

### 9. 先运行测试和 dry-run

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python -m py_compile market_monitor.py tests/test_resend_delivery.py
python -m unittest discover -s tests -v
python market_monitor.py --dry-run
```

`--dry-run` 会真实拉取行情并计算指标，但不会发送日报。盘中运行时看到 `Market data stage=intraday` 是正常的；收盘数据确认后会看到 `Market data stage=finalized`。

### 10. 发送测试邮件（可选但上线前建议）

```bash
python market_monitor.py --send-test-email
```

它用于验证：

- Resend 或 SMTP 是否能投递
- HTML 样式是否正常
- 图表是否可显示

这条命令会向当前配置的全部收件人发送测试邮件。生产环境应先临时缩小收件人列表，避免打扰他人。

### 11. 安装 systemd 单元

```bash
cp deploy/market-signal-monitor.service /etc/systemd/system/
cp deploy/market-signal-monitor.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now market-signal-monitor.timer
systemctl restart market-signal-monitor.timer
```

也可以在代码和 `.env` 准备好后查看并使用：

```bash
bash deploy/install_ubuntu.sh
```

安装脚本会安装系统依赖、创建虚拟环境、安装 Python 依赖并启用 timer。执行前应先阅读脚本并确认路径。

### 12. 验证定时器

```bash
systemctl status market-signal-monitor.timer --no-pager
systemctl list-timers market-signal-monitor.timer
```

正常状态：

- `active (waiting)`
- 基础触发时间为每天 `07:50 Asia/Shanghai`
- 随机延迟最多 `20` 分钟
- 实际运行一般落在 `07:50 - 08:10`
- `Persistent=true`，错过的触发会在系统恢复后按 systemd 规则补触发

### 13. 验证 service

```bash
systemctl status market-signal-monitor.service --no-pager
journalctl -u market-signal-monitor.service -n 50 --no-pager
```

`market-signal-monitor.service` 是 `Type=oneshot`，运行完成后显示 `inactive (dead)` 且 `status=0/SUCCESS` 是正常现象。

如需手动执行 systemd service：

```bash
systemctl start market-signal-monitor.service
```

注意：它会运行真实发送逻辑，可能给全部生产收件人发邮件。只检查代码和行情时应使用 `python market_monitor.py --dry-run`。

## systemd 定时任务

service 的关键行为：

- 工作目录：`/opt/market-signal-monitor`
- Python：`/opt/market-signal-monitor/.venv/bin/python`
- 主程序：`/opt/market-signal-monitor/market_monitor.py`
- 用户：`root`
- 类型：`oneshot`

timer 的关键行为：

- `OnCalendar=*-*-* 07:50:00 Asia/Shanghai`
- `RandomizedDelaySec=20m`
- `AccuracySec=1s`
- `Persistent=true`

修改 unit 文件后必须执行：

```bash
systemctl daemon-reload
systemctl restart market-signal-monitor.timer
```

## 日常运维

### 定时器

```bash
systemctl status market-signal-monitor.timer --no-pager
systemctl list-timers market-signal-monitor.timer
```

### 服务

```bash
systemctl status market-signal-monitor.service --no-pager
```

### 本地日志

```bash
tail -f /opt/market-signal-monitor/logs/market-signal-monitor.log
tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log
```

### systemd 日志

```bash
journalctl -u market-signal-monitor.service -n 100 --no-pager
journalctl -u market-signal-monitor.timer -n 50 --no-pager
```

### 手动补跑

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python market_monitor.py --force-send
```

注意：补跑会执行真实发送；盘中会生成盘中快照，收盘后正式日报仍可发送。

### 修改配置后检查

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python market_monitor.py --dry-run
systemctl status market-signal-monitor.timer --no-pager
```

## 故障排查

某天没收到邮件时，按以下顺序检查：

```bash
systemctl list-timers market-signal-monitor.timer
systemctl status market-signal-monitor.service --no-pager
tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log
journalctl -u market-signal-monitor.service --since "today 07:40" --no-pager
```

常见日志：

- `Daily email sent.`：日报已成功发送。
- `No new U.S. market session data is available yet; skip sending.`：没有新的已完成市场日，正常跳过。
- `Intraday snapshot for the current U.S. market session was already sent; skip sending.`：当天盘中快照已经发过，不重复发送。
- `Monitor run failed: ...`：数据源、配置或程序异常。
- `Failure notice email sent.`：正式日报失败，但异常通知已经发出。
- `Market data stage=intraday`：当前行情仍属于盘中快照。
- `Market data stage=finalized`：当前行情已经按正式收盘口径确认。

排查方向：

1. timer 是否 `active (waiting)`，下一次触发时间是否正确。
2. service 最近一次 `Result` 和退出码是否为成功。
3. 日志里最终选择的数据源和市场日期是否一致。
4. `.env` 的 Key 是否过期、超额或权限不足。
5. Resend 是否显示 `delivered`、`bounced` 或拒绝原因。
6. 状态文件是否可读、日期是否异常提前写入。
7. VPS 时区、磁盘空间和 DNS 是否正常。

## 谨慎修改事项

下列修改最容易影响稳定性，变更前应备份并执行测试与 dry-run：

- provider 顺序及不可重试错误分类
- 市场日期对齐逻辑
- `last_processed_market_date` 的写入时机
- 盘中快照与正式日报的去重逻辑
- Resend 限速、重试和幂等键
- SMTP/Resend 收件人解析
- systemd timer 时间和时区
- 图表生成和邮件 MIME 结构

特别注意：

- 不要在发信成功前更新正式发送状态。
- 不要让本地缓存成为正式行情来源。
- 不要把不同日期或不同 provider 的 OHLC 字段拼成一根 K 线。
- 图表失败时应保留文字邮件降级能力。
- 单个收件地址失败时，应继续处理其余地址并在最后汇总错误。

## 迁移与交接检查清单

### 迁移到另一台 VPS 最容易忘记的事项

1. `.env` 不会跟随 Git，需要单独安全迁移。
2. 新机器时区应设置正确。
3. 中文字体缺失会导致图表中文变方块。
4. systemd unit 复制后必须执行 `daemon-reload`。
5. 需要重新创建虚拟环境并安装依赖，不要直接复制包含旧绝对路径的 `.venv`。
6. 日志、缓存、状态路径必须全部指向 `/opt/market-signal-monitor`。
7. 复制旧状态文件前要确认它代表的最后成功市场日期正确。
8. 上线前至少完成测试、dry-run 和一次受控测试邮件。

### 接手人需要知道

1. 正式行情链路目前没有接入长桥。
2. 缓存不是正式行情来源。
3. 失败时应发送异常通知，不应发送旧数据日报。
4. 只有发信成功后才能更新状态文件。
5. API Key 如果曾经暴露，必须轮换。
6. systemd service 是一次性任务，运行后 `inactive (dead)` 不代表失败。
7. Resend 模式逐个独立投递，收件人之间不可见。

### 新接手或新 VPS 验证流程

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python -m py_compile market_monitor.py tests/test_resend_delivery.py
python -m unittest discover -s tests -v
python market_monitor.py --dry-run
python market_monitor.py --send-test-email
systemctl status market-signal-monitor.timer --no-pager
systemctl list-timers market-signal-monitor.timer
tail -n 50 /opt/market-signal-monitor/logs/market-signal-monitor.log
```

发送测试邮件前必须先确认收件人，避免打扰生产用户。以上步骤全部正常，才可以认为部署具备稳定运行条件。

## 长桥探测脚本

`longbridge_probe.py` 仅用于独立验证，不参与正式日报。

已经验证：

- 八家科技公司的美股日线可以获取。

暂未接入正式链路，原因：

- 标普 500 指数在长桥中的 symbol 尚未确认。
- Access Token 有有效期，不适合当前长期无人值守场景。

如需单独测试：

```bash
pip install longbridge
python longbridge_probe.py
```

## 安全与开源检查

提交 GitHub 前确认以下内容没有进入版本控制：

- `.env` 及其他真实环境配置
- Resend、FMP、Twelve Data、EODHD 等 API Key
- SMTP 密码或授权码
- SSH 私钥、`.pem`、`.ppk`、`id_rsa*`、`id_ed25519*`
- 真实员工邮箱和生产收件人列表
- `data/`、`logs/`、缓存、状态文件
- `.venv/`、`__pycache__/`、编辑器配置

推荐检查：

```bash
git status --short
git ls-files
```

`.gitignore` 已包含常见密钥、运行状态、日志、虚拟环境、缓存和编辑器文件规则，但它不能替代提交前人工检查。

如果密钥曾经出现在聊天、终端输出、截图或 Git 历史中，仅删除文件并不够，必须在对应服务后台轮换密钥。

公开仓库前还应检查整个 Git 历史，而不只是当前工作目录；已经提交过的敏感信息不会因为后续删除文件而自动消失。建议同时在 GitHub 仓库设置中启用 Secret scanning 和 Push protection，阻止常见凭据被再次推送。

## 许可证与免责声明

本项目采用 [MIT License](LICENSE)。你可以在遵守许可证条款的前提下使用、修改和分发代码。

本项目仅用于软件开发、行情观察和技术研究，不构成投资建议、交易建议或对数据准确性的保证。第三方行情可能延迟、缺失或错误，邮件投递也可能受到网络、服务商额度和垃圾邮件策略影响。任何投资或交易决定及其结果均由使用者自行负责。
