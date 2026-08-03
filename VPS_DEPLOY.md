# 新 VPS 部署指南

这份文档的目标很明确：

- 你买了一台全新的 VPS
- 想把当前这个“标普500指数 + 八家科技公司”监控项目完整部署上去
- 希望按步骤操作，不用自己临时猜

本文默认场景：

- 系统是 `Ubuntu / Debian`
- 项目部署目录使用 `/opt/market-signal-monitor`
- 使用 `systemd timer` 定时运行
- 推荐使用 Resend 发信，也保留 SMTP 兼容

如果你不是 `root` 用户，文中的系统级命令前面加 `sudo` 即可。

## 一、部署前你需要准备什么

正式开始前，请先准备好下面这些东西：

1. 一台可以 SSH 登录的 VPS
2. 项目完整代码
3. SMTP 发信配置
4. 至少一个可用的数据源 API key

建议至少准备：

- `TWELVEDATA_API_KEY`
- `FMP_API_KEY`
- `EODHD_API_KEY`

补充说明：

- 当前 `FMP` 在部分账号或套餐上可能返回 `402/403`
- 所以就算你配置了 `FMP_API_KEY`，也仍然建议保留 `TWELVEDATA_API_KEY`
- 如果你要补齐指数和个股的最新收盘数据，建议额外配置 `EODHD_API_KEY`
- 当前标普500指数最新收盘数据的稳定性，实际更依赖 `EODHD_API_KEY`

补充说明：

- 新部署使用 `INDEX_*` 变量名描述指数监控规则

## 二、第一步：登录 VPS

在你本机执行：

```bash
ssh root@你的VPSIP
```

登录成功后，建议先确认系统版本：

```bash
cat /etc/os-release
```

## 三、第二步：设置时区

因为这套系统的发送时间是按 VPS 本地时间执行，所以先把时区设对很重要。

如果你希望按中国时间每天早上 `07:50 - 08:10` 发送，执行：

```bash
timedatectl set-timezone Asia/Shanghai
timedatectl
```

你应该看到类似：

- `Time zone: Asia/Shanghai`

## 四、第三步：安装系统依赖

更新系统软件源并安装 Python、虚拟环境、字体包：

```bash
apt update
apt install -y python3 python3-venv python3-pip fonts-noto-cjk
fc-cache -fv
```

为什么装 `fonts-noto-cjk`：

- 邮件里的 K 线图包含中文
- 如果不装中文字体，图表里的中文有可能显示成方块

## 五、第四步：上传项目代码

推荐把项目放在：

```bash
/opt/market-signal-monitor
```

### 方式 A：从本机直接上传整个目录

如果你在 Windows PowerShell，可以执行：

```powershell
scp -r C:\Users\leyou\Desktop\code\market-signal-monitor root@你的VPSIP:/opt/
```

上传后 VPS 上的目录会是：

```bash
/opt/market-signal-monitor
```

### 方式 B：如果你只想上传关键文件

至少要保证这些文件在 VPS 上：

- `market_monitor.py`
- `requirements.txt`
- `.env`
- `.env.example`
- `README.md`
- `HANDOVER.md`
- `VPS_DEPLOY.md`
- `deploy/market-signal-monitor.service`
- `deploy/market-signal-monitor.timer`

重要提醒：

- `.env` 里有真实密钥，通常不会进 Git
- 如果你是通过 Git 拉代码，记得单独把 `.env` 补到 VPS 上

## 六、第五步：检查项目目录

进入项目目录：

```bash
cd /opt/market-signal-monitor
ls -la
```

你至少应该能看到：

- `market_monitor.py`
- `requirements.txt`
- `.env`
- `deploy`

## 七、第六步：创建 Python 虚拟环境

如果这是全新 VPS，执行：

```bash
cd /opt/market-signal-monitor
python3 -m venv .venv
source .venv/bin/activate
```

激活成功后，命令行前面通常会出现：

```bash
(.venv)
```

## 八、第七步：安装 Python 依赖

在虚拟环境中执行：

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

如果你还要测试长桥探测脚本，可以额外安装：

```bash
pip install longbridge
```

## 九、第八步：检查并填写 .env

如果你还没有 `.env`，先从模板复制：

```bash
cp .env.example .env
```

然后编辑：

```bash
nano .env
```

你至少要确认这些字段：

### 1. SMTP 配置

例如 Resend 常见写法：

```env
EMAIL_PROVIDER=resend
RESEND_API_KEY=你的 Resend API Key
RESEND_FROM=Market Monitor <report@example.com>
RESEND_REQUESTS_PER_SECOND=4
RESEND_MAX_ATTEMPTS=4
RESEND_RETRY_DELAY_SECONDS=1
SMTP_RECIPIENT=你的收件邮箱
SMTP_BCC=员工1@example.com,员工2@example.com
```

脚本会逐个独立投递并默认控制在每秒 4 次请求。遇到 `429`、Resend `5xx` 或临时网络错误时会自动退避重试；某个地址永久失败时，后续地址仍会继续发送，最后统一报告失败结果。

如果不用 Resend，也可以继续使用 SMTP。比如 QQ 邮箱常见写法：

```env
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的发件邮箱
SMTP_PASSWORD=你的授权码
SMTP_SENDER=你的发件邮箱
SMTP_RECIPIENT=你的收件邮箱
SMTP_BCC=员工1@example.com,员工2@example.com
SMTP_USE_SSL=true
SMTP_USE_STARTTLS=false
```

### 2. 数据源 API key

```env
FMP_API_KEY=你的key
TWELVEDATA_API_KEY=你的key
EODHD_API_KEY=你的key
EODHD_STOCK_EXCHANGE_CODE=US
EODHD_INDEX_SYMBOLS=GSPC.INDX,SPX.INDX,SP500.INDX,^GSPC.INDX
```

### 3. 日志路径

建议保持默认：

```env
LOG_FILE=/opt/market-signal-monitor/logs/market-signal-monitor.log
```

### 4. 缓存路径

建议保持默认：

```env
CACHE_DIR=/opt/market-signal-monitor/data/cache
```

### 5. 状态文件路径

如果你没特殊需求，可以不写，程序会默认写到：

```bash
/opt/market-signal-monitor/data/state.json
```

编辑完成后，可以快速检查关键字段有没有：

```bash
grep -E "^(EMAIL_PROVIDER|RESEND_FROM|SMTP_RECIPIENT|FMP_API_KEY|TWELVEDATA_API_KEY|EODHD_API_KEY)=" .env
```

## 十、第九步：先手动验证脚本能不能跑

### 1. 先做 dry-run

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python market_monitor.py --dry-run
```

这个命令不会真正发正式日报，但会去拉行情并计算指标。
它会按“最新已完成美股市场日”的口径校验整套数据，不会为了凑结果而退回旧一天的正式日报。

### 2. 再发测试邮件

```bash
python market_monitor.py --send-test-email
```

这一步主要验证：

- SMTP 是否能发出去
- HTML 邮件样式是否正常
- 图表是否正常显示

如果测试邮件能收到，说明整条链路已经基本通了。

### 3. 如果想补跑一次正式邮件

```bash
python market_monitor.py --force-send
```

这个命令会忽略“今天是否已经发过”的状态，强制按正式逻辑跑一次。

补充说明：

- 如果执行时美股尚未收盘，系统会发一封“盘中快照”
- 这封盘中快照不会阻止收盘后的正式日报再次发送

## 十一、第十步：查看日志

本地日志文件：

```bash
tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log
```

实时看日志：

```bash
tail -f /opt/market-signal-monitor/logs/market-signal-monitor.log
```

如果手动执行失败，这里通常最先能看到原因。

## 十二、第十一步：安装 systemd service / timer

确认项目里的 systemd 模板已经在正确位置：

- `deploy/market-signal-monitor.service`
- `deploy/market-signal-monitor.timer`

然后执行：

```bash
cp deploy/market-signal-monitor.service /etc/systemd/system/
cp deploy/market-signal-monitor.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now market-signal-monitor.timer
systemctl restart market-signal-monitor.timer
```

检查 timer 状态：

```bash
systemctl status market-signal-monitor.timer --no-pager
systemctl list-timers market-signal-monitor.timer
```

你应该看到：

- `active (waiting)`
- 下一次触发时间在 `07:50 - 08:10` 之间

## 十三、第十二步：手动检查 service 是否正常

可以主动触发一次 service：

```bash
systemctl start market-signal-monitor.service
journalctl -u market-signal-monitor.service -n 50 --no-pager
```

如果看到：

- `Daily email sent.`

说明 systemd 方式也已经跑通。

补充说明：

- `market-signal-monitor.service` 是一次性任务，不是常驻进程
- 所以运行完成后显示 `inactive (dead)` 且 `status=0/SUCCESS` 属于正常现象

## 十四、以后最常用的运维命令

### 1. 看定时器状态

```bash
systemctl status market-signal-monitor.timer --no-pager
systemctl list-timers market-signal-monitor.timer
```

### 2. 看服务状态

```bash
systemctl status market-signal-monitor.service --no-pager
```

### 3. 看本地日志

```bash
tail -f /opt/market-signal-monitor/logs/market-signal-monitor.log
tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log
```

### 4. 看 systemd 日志

```bash
journalctl -u market-signal-monitor.service -n 100 --no-pager
journalctl -u market-signal-monitor.timer -n 50 --no-pager
```

### 5. 手动补跑

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python market_monitor.py --force-send
```

## 十五、如果某天没收到邮件，怎么查

按这个顺序查通常最快：

```bash
systemctl list-timers market-signal-monitor.timer
systemctl status market-signal-monitor.service --no-pager
tail -n 100 /opt/market-signal-monitor/logs/market-signal-monitor.log
journalctl -u market-signal-monitor.service --since "today 07:40" --no-pager
```

常见情况：

- `Daily email sent.`
  说明邮件已经发出

- `No new U.S. market session data is available yet; skip sending.`
  说明没有新的“已完成美股市场日”，正常跳过

- `Intraday snapshot for the current U.S. market session was already sent; skip sending.`
  说明当天盘中快照已经发过，系统不会重复发第二封盘中快照

- `Monitor run failed: ...`
  说明数据源或脚本异常

- `Failure notice email sent.`
  说明正式日报失败，但异常通知邮件已经发出

## 十六、迁移到另一台 VPS 时，最容易忘的点

1. `.env` 不会自动跟着 Git 走
2. 时区不设对，发送时间就不对
3. 不安装中文字体，图表里的中文可能是方块
4. systemd unit 文件复制后要 `daemon-reload`
5. 只有手动 `python market_monitor.py --send-test-email` 测过，才算真正验证过

## 十七、推荐的上线后检查清单

新 VPS 部署完成后，建议至少完成下面这些检查：

```bash
cd /opt/market-signal-monitor
source .venv/bin/activate
python market_monitor.py --dry-run
python market_monitor.py --send-test-email
systemctl status market-signal-monitor.timer --no-pager
systemctl list-timers market-signal-monitor.timer
tail -n 50 /opt/market-signal-monitor/logs/market-signal-monitor.log
```

如果这几步都正常，说明这台 VPS 已经具备稳定运行条件。
