import argparse
import base64
import csv
import io
import json
import logging
import os
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from zoneinfo import ZoneInfo


STOOQ_URL = "https://stooq.com/q/d/l/"
STOOQ_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://stooq.com/",
}
DEFAULT_TECH_STOCKS = ["MSFT", "NVDA", "META", "AAPL", "GOOGL", "AMZN", "TSM", "AVGO"]
INDEX_SYMBOL = "GSPC"
INDEX_DISPLAY_NAME = "标普500指数"
INDEX_SHORT_NAME = "标普500"


class ConfigError(Exception):
    pass


class NonRetryableProviderError(RuntimeError):
    pass


@dataclass
class Settings:
    # 这里把所有运行时配置集中起来，是为了让“策略”和“实现”分开。
    # 后面如果要换阈值、换数据源、换日志路径，只需要动配置，不用反复改主流程。
    index_lookback_days: int
    index_60d_threshold_pct: float
    index_ytd_threshold_pct: float
    stock_lookback_days: int
    stock_60d_threshold_pct: float
    tech_stocks: list[str]
    request_timeout_seconds: int
    request_retries: int
    request_retry_delay_seconds: int
    fmp_api_key: str
    twelvedata_api_key: str
    eodhd_api_key: str
    eodhd_stock_exchange_code: str
    eodhd_index_symbols: list[str]
    email_provider: str
    resend_api_key: str
    resend_from: str
    resend_requests_per_second: float
    resend_max_attempts: int
    resend_retry_delay_seconds: float
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_sender: str
    email_recipients: str
    smtp_use_ssl: bool
    smtp_use_starttls: bool
    state_file: Path
    log_file: Path
    cache_dir: Path


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Metric:
    symbol: str
    current_date: str
    current_close: float
    previous_close: float | None
    previous_date: str | None
    daily_change_pct: float | None
    high_60d: float
    high_60d_date: str
    drawdown_60d_pct: float
    high_1y: float
    high_1y_date: str
    low_1y: float
    low_1y_date: str
    drawdown_1y_pct: float
    ytd_start_date: str | None = None
    ytd_start_close: float | None = None
    ytd_change_pct: float | None = None


@dataclass
class FetchResult:
    bars: list[Bar]
    source: str


def load_dotenv(path: Path) -> None:
    # 这里优先支持本地 .env，是为了让 VPS 部署简单直接；
    # 同时仍然保留真正环境变量的优先级，方便 systemd 或 shell 里覆盖配置。
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(*names: str, default: str | None = None, required: bool = False) -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    if required:
        raise ConfigError(f"Missing required environment variable: {names[0]}")
    return default or ""


def env_int(*names: str, default: int) -> int:
    return int(env(*names, default=str(default)))


def env_float(*names: str, default: float) -> float:
    return float(env(*names, default=str(default)))


def format_from_high_pct(value: float) -> str:
    # “距离一年高点”这类字段内部按正回撤计算更方便，
    # 但展示上用户更习惯看到负号，所以统一在出口层做格式转换。
    if abs(value) < 0.005:
        return "0.00%"
    return f"-{value:.2f}%"


def format_signed_pct(value: float | None) -> str:
    # 涨跌幅这类字段带正负号更利于扫读；
    # 这里统一格式，避免 HTML、纯文本、日志三套口径各写各的。
    if value is None:
        return "N/A"
    if abs(value) < 0.005:
        return "0.00%"
    return f"{value:+.2f}%"


def change_color(value: float | None) -> str:
    # 用户阅读习惯里最敏感的是“今天到底红还是绿”，
    # 所以颜色规则集中收口，后面所有卡片都复用这一套。
    if value is None or abs(value) < 0.005:
        return "#334155"
    return "#16a34a" if value > 0 else "#dc2626"


def provider_display_name(name: str) -> str:
    return {
        "fmp": "FMP",
        "twelvedata": "Twelve Data",
        "eodhd": "EODHD",
        "yahoo": "Yahoo",
        "stooq": "Stooq",
    }.get(name, name)


def summarize_sources(index_result: FetchResult, stock_fetch_results: dict[str, FetchResult]) -> str:
    # 数据源摘要不是给交易判断看的，而是给运维排查看的。
    # 这里尽量压缩成一行，既不打扰阅读，又能让用户知道今天到底走了哪条兜底链路。
    stock_counts: dict[str, int] = {}
    for result in stock_fetch_results.values():
        stock_counts[result.source] = stock_counts.get(result.source, 0) + 1
    stock_parts = []
    for source, count in sorted(stock_counts.items(), key=lambda item: item[0]):
        label = provider_display_name(source)
        stock_parts.append(f"{label} x{count}" if count > 1 else label)
    stock_summary = "、".join(stock_parts) if stock_parts else "无"
    return f"指数 {provider_display_name(index_result.source)} | 个股 {stock_summary}"


def load_settings(base_dir: Path) -> Settings:
    # API key 在这里设计成“可选”，是因为这套脚本本来就依赖多数据源兜底。
    # 某个 key 缺失时不该让程序直接无法启动，而应该交给后面的 provider 链路去跳过。
    # 指数相关规则统一使用 INDEX_* 配置，当前监控对象是标普 500 指数。
    smtp_use_ssl = env("SMTP_USE_SSL", default="false").lower() == "true"
    smtp_use_starttls = env("SMTP_USE_STARTTLS", default="true").lower() == "true"
    if smtp_use_ssl and smtp_use_starttls:
        raise ConfigError("SMTP_USE_SSL and SMTP_USE_STARTTLS cannot both be true")
    resend_api_key = env("RESEND_API_KEY")
    email_provider = env("EMAIL_PROVIDER", default="resend" if resend_api_key else "smtp").strip().lower()
    if email_provider not in {"smtp", "resend"}:
        raise ConfigError("EMAIL_PROVIDER must be either smtp or resend")
    if email_provider == "resend" and not resend_api_key:
        raise ConfigError("Missing required environment variable: RESEND_API_KEY")
    resend_from = env("RESEND_FROM", default=env("SMTP_SENDER"))
    if email_provider == "resend" and not resend_from:
        raise ConfigError("Missing required environment variable: RESEND_FROM")
    resend_requests_per_second = env_float("RESEND_REQUESTS_PER_SECOND", default=4.0)
    resend_max_attempts = env_int("RESEND_MAX_ATTEMPTS", default=4)
    resend_retry_delay_seconds = env_float("RESEND_RETRY_DELAY_SECONDS", default=1.0)
    if resend_requests_per_second <= 0:
        raise ConfigError("RESEND_REQUESTS_PER_SECOND must be greater than 0")
    if resend_max_attempts < 1:
        raise ConfigError("RESEND_MAX_ATTEMPTS must be at least 1")
    if resend_retry_delay_seconds < 0:
        raise ConfigError("RESEND_RETRY_DELAY_SECONDS cannot be negative")

    tech_stocks = [
        symbol.strip().upper()
        for symbol in env("TECH_STOCKS", default=",".join(DEFAULT_TECH_STOCKS)).split(",")
        if symbol.strip()
    ]
    eodhd_index_symbols = [
        symbol.strip()
        for symbol in env(
            "EODHD_INDEX_SYMBOLS",
            default="GSPC.INDX,SPX.INDX,SP500.INDX,^GSPC.INDX",
        ).split(",")
        if symbol.strip()
    ]

    return Settings(
        index_lookback_days=env_int("INDEX_LOOKBACK_DAYS", default=60),
        index_60d_threshold_pct=env_float("INDEX_60D_DROP_THRESHOLD_PCT", default=7.0),
        index_ytd_threshold_pct=env_float("INDEX_YTD_DROP_THRESHOLD_PCT", default=7.0),
        stock_lookback_days=env_int("STOCK_LOOKBACK_DAYS", default=60),
        stock_60d_threshold_pct=env_float("STOCK_60D_DROP_THRESHOLD_PCT", default=20.0),
        tech_stocks=tech_stocks,
        request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", default=20),
        request_retries=env_int("REQUEST_RETRIES", default=3),
        request_retry_delay_seconds=env_int("REQUEST_RETRY_DELAY_SECONDS", default=2),
        fmp_api_key=env("FMP_API_KEY"),
        twelvedata_api_key=env("TWELVEDATA_API_KEY"),
        eodhd_api_key=env("EODHD_API_KEY"),
        eodhd_stock_exchange_code=env("EODHD_STOCK_EXCHANGE_CODE", default="US").strip().upper() or "US",
        eodhd_index_symbols=eodhd_index_symbols,
        email_provider=email_provider,
        resend_api_key=resend_api_key,
        resend_from=resend_from,
        resend_requests_per_second=resend_requests_per_second,
        resend_max_attempts=resend_max_attempts,
        resend_retry_delay_seconds=resend_retry_delay_seconds,
        smtp_host=env("SMTP_HOST", required=email_provider == "smtp"),
        smtp_port=env_int("SMTP_PORT", default=587),
        smtp_user=env("SMTP_USER", required=email_provider == "smtp"),
        smtp_password=env("SMTP_PASSWORD", required=email_provider == "smtp"),
        smtp_sender=env("SMTP_SENDER", required=email_provider == "smtp"),
        email_recipients=env("EMAIL_RECIPIENTS", required=True),
        smtp_use_ssl=smtp_use_ssl,
        smtp_use_starttls=smtp_use_starttls,
        state_file=Path(env("STATE_FILE", default=str(base_dir / "data" / "state.json"))),
        log_file=Path(env("LOG_FILE", default=str(base_dir / "logs" / "market-signal-monitor.log"))),
        cache_dir=Path(env("CACHE_DIR", default=str(base_dir / "data" / "cache"))),
    )


def setup_logging(path: Path) -> logging.Logger:
    # 同时写文件和控制台，是为了兼顾两种排查路径：
    # 平时用 tail -f 看本地日志，systemd 出问题时还能从 journalctl 追溯。
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("market_signal_monitor")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for handler in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_processed_market_date": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_processed_market_date": None}


def save_state(path: Path, state: dict) -> None:
    # 状态文件只保留“是否已经发过”和“今天是否已经发过失败通知”这类最小状态，
    # 目的是让脚本尽量保持幂等，不因为重复执行而重复发信。
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def stooq_symbol(symbol: str) -> str:
    return "^spx" if symbol == INDEX_SYMBOL else f"{symbol.lower()}.us"


def yahoo_symbol(symbol: str) -> str:
    return "^GSPC" if symbol == INDEX_SYMBOL else symbol


def eodhd_symbol_candidates(symbol: str, settings: Settings) -> list[str]:
    if symbol == INDEX_SYMBOL:
        candidates = settings.eodhd_index_symbols
    else:
        suffix = settings.eodhd_stock_exchange_code
        candidates = [f"{symbol}.{suffix}"]
        if "." in symbol:
            candidates.append(symbol)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def cache_file_for(settings: Settings, symbol: str) -> Path:
    return settings.cache_dir / f"{symbol.lower()}.json"


def save_cached_bars(settings: Settings, symbol: str, bars: list[Bar], source: str) -> None:
    # 这里仍然保留缓存，但定位已经收缩成“诊断快照”。
    # 我们保存它，是为了排查数据源异常时能知道上一次成功抓到的内容，
    # 而不是为了把旧数据伪装成今天的数据继续发给用户。
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file_for(settings, symbol).write_text(
        json.dumps(
            {
                "symbol": symbol,
                "source": source,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "bars": [bar.__dict__ for bar in bars],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_cached_bars(settings: Settings, symbol: str) -> list[Bar]:
    path = cache_file_for(settings, symbol)
    if not path.exists():
        raise RuntimeError(f"No cache file for {symbol}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    bars = [Bar(**item) for item in payload.get("bars", [])]
    if not bars:
        raise RuntimeError(f"Cache file for {symbol} is empty")
    bars.sort(key=lambda item: item.date)
    return bars


def parse_stooq_csv(text: str) -> list[Bar]:
    rows = list(csv.DictReader(io.StringIO(text)))
    bars: list[Bar] = []
    for row in rows:
        try:
            bars.append(
                Bar(
                    date=row["Date"],
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                )
            )
        except Exception:
            continue
    bars.sort(key=lambda item: item.date)
    return bars


def parse_json_bars(rows: list[dict]) -> list[Bar]:
    # 各家 provider 的字段命名不一致，但真正计算时只关心统一的 OHLC + date。
    # 所以先在这里做一次“归一化”，后面指标计算就不需要知道数据来自谁。
    bars: list[Bar] = []
    for row in rows:
        try:
            date_value = row.get("date") or row.get("datetime") or row.get("Date")
            if not date_value:
                continue
            bars.append(
                Bar(
                    date=str(date_value)[:10],
                    open=float(row.get("open") or row.get("Open")),
                    high=float(row.get("high") or row.get("High")),
                    low=float(row.get("low") or row.get("Low")),
                    close=float(row.get("close") or row.get("Close")),
                )
            )
        except Exception:
            continue
    bars.sort(key=lambda item: item.date)
    return bars


def fetch_from_fmp(session, symbol: str, settings: Settings) -> list[Bar]:
    # FMP 放在最前面，是因为它本来更像正式 API；
    # 但实际免费档/权限经常受限制，所以这里的策略不是“死磕”，而是尽快识别能不能用。
    if not settings.fmp_api_key:
        raise RuntimeError("FMP_API_KEY is not configured")

    date_from = (datetime.now(timezone.utc) - timedelta(days=450)).strftime("%Y-%m-%d")
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    symbol_candidates = [symbol] if symbol != INDEX_SYMBOL else ["^GSPC", "GSPC", ".INX", "SPX", ".SPX"]
    endpoint_candidates: list[tuple[str, dict]] = []

    for candidate in symbol_candidates:
        endpoint_candidates.extend(
            [
                (
                    "https://financialmodelingprep.com/stable/historical-price-eod/light",
                    {"symbol": candidate, "from": date_from, "to": date_to, "apikey": settings.fmp_api_key},
                ),
            ]
        )
        if symbol == INDEX_SYMBOL:
            endpoint_candidates.append(
                (
                    "https://financialmodelingprep.com/stable/historical-price-eod/light",
                    {"symbol": candidate, "apikey": settings.fmp_api_key},
                )
            )
        else:
            endpoint_candidates.append(
                (
                    f"https://financialmodelingprep.com/api/v3/historical-price-full/{candidate}",
                    {"from": date_from, "to": date_to, "apikey": settings.fmp_api_key},
                )
            )

    last_error: Exception | None = None
    for url, params in endpoint_candidates:
        try:
            response = session.get(url, params=params, timeout=settings.request_timeout_seconds)
            # 4xx 基本不是抖动，而是权限、套餐或端点不匹配。
            # 这类错误继续重试几次没有意义，应该尽快切到下一个 provider。
            if 400 <= response.status_code < 500:
                raise NonRetryableProviderError(f"FMP returned HTTP {response.status_code} for {symbol}")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("Error Message"):
                raise NonRetryableProviderError(payload["Error Message"])
            if isinstance(payload, dict) and payload.get("historical"):
                bars = parse_json_bars(payload["historical"])
            elif isinstance(payload, list):
                bars = parse_json_bars(payload)
            elif isinstance(payload, dict) and payload.get("data"):
                bars = parse_json_bars(payload["data"])
            else:
                raise RuntimeError(f"Unexpected FMP payload shape: {type(payload).__name__}")
            if len(bars) < 60:
                raise RuntimeError(f"FMP returned only {len(bars)} valid rows for {symbol}")
            return bars
        except NonRetryableProviderError:
            raise
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"FMP fetch failed for {symbol}: {last_error}") from last_error


def fetch_from_twelvedata(session, symbol: str, settings: Settings) -> list[Bar]:
    # Twelve Data 这边我们明确不拿来跑指数，
    # 因为实测里它对这个指数代码并不稳定，强行接进去只会增加分钟额度浪费。
    if not settings.twelvedata_api_key:
        raise RuntimeError("TWELVEDATA_API_KEY is not configured")
    if symbol == INDEX_SYMBOL:
        raise NonRetryableProviderError("Twelve Data is skipped for the index in current setup")

    symbol_candidates = [symbol]
    last_error: Exception | None = None
    for candidate in symbol_candidates:
        try:
            response = session.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol": candidate,
                    "interval": "1day",
                    "outputsize": 400,
                    "apikey": settings.twelvedata_api_key,
                },
                timeout=settings.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") == "error":
                message = payload.get("message") or payload.get("code") or "unknown Twelve Data error"
                lowered = message.lower()
                # 额度耗尽和 symbol 无效这两类错误都不是“等两秒再试一次”能解决的，
                # 所以一旦识别出来就直接切下一个源，避免把分钟额度快速打爆。
                if "run out of api credits" in lowered or "missing or invalid" in lowered:
                    raise NonRetryableProviderError(message)
                raise RuntimeError(message)
            values = payload.get("values") or []
            bars = parse_json_bars(values)
            if len(bars) < 60:
                raise RuntimeError(f"Twelve Data returned only {len(bars)} valid rows for {symbol}")
            return bars
        except NonRetryableProviderError:
            raise
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Twelve Data fetch failed for {symbol}: {last_error}") from last_error


def fetch_from_eodhd(session, symbol: str, settings: Settings) -> list[Bar]:
    if not settings.eodhd_api_key:
        raise NonRetryableProviderError("EODHD_API_KEY is not configured")

    date_from = (datetime.now(timezone.utc) - timedelta(days=450)).strftime("%Y-%m-%d")
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_error: Exception | None = None

    for candidate in eodhd_symbol_candidates(symbol, settings):
        try:
            response = session.get(
                f"https://eodhd.com/api/eod/{candidate}",
                params={
                    "api_token": settings.eodhd_api_key,
                    "fmt": "json",
                    "from": date_from,
                    "to": date_to,
                },
                timeout=settings.request_timeout_seconds,
            )
            if 400 <= response.status_code < 500:
                raise NonRetryableProviderError(f"EODHD returned HTTP {response.status_code} for {candidate}")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message") or payload.get("status")
                if message:
                    raise NonRetryableProviderError(f"EODHD returned {message} for {candidate}")
                raise RuntimeError(f"Unexpected EODHD payload shape for {candidate}: dict")
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected EODHD payload shape for {candidate}: {type(payload).__name__}")

            bars = parse_json_bars(payload)
            if len(bars) < 60:
                raise RuntimeError(f"EODHD returned only {len(bars)} valid rows for {candidate}")
            return bars
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"EODHD fetch failed for {symbol}: {last_error}") from last_error


def fetch_from_stooq(session, symbol: str, settings: Settings) -> list[Bar]:
    # Stooq 留在链路里，是因为它在某些场景下反而比其他免费源更稳；
    # 但它也出现过 200 + 空 body，所以只能放在后面的兜底层。
    params = {
        "s": stooq_symbol(symbol),
        "i": "d",
        "d1": (datetime.now(timezone.utc) - timedelta(days=450)).strftime("%Y%m%d"),
        "d2": datetime.now(timezone.utc).strftime("%Y%m%d"),
    }
    response = session.get(STOOQ_URL, params=params, headers=STOOQ_HEADERS, timeout=settings.request_timeout_seconds)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        raise RuntimeError("Stooq returned an empty response body")
    if "No data" in text:
        raise RuntimeError(f"Stooq returned no data for {symbol}")
    bars = parse_stooq_csv(text)
    if len(bars) < 60:
        raise RuntimeError(f"Stooq returned only {len(bars)} valid rows for {symbol}")
    return bars


def fetch_from_yahoo(session, symbol: str, settings: Settings) -> list[Bar]:
    # Yahoo 不是正式带 SLA 的付费数据接口，但覆盖面很广，
    # 作为中后段兜底非常有价值，尤其是指数类数据经常能补上别家拿不到的空缺。
    quote_symbol = yahoo_symbol(symbol)
    session.get(
        f"https://finance.yahoo.com/quote/{quote_symbol}",
        headers={"User-Agent": STOOQ_HEADERS["User-Agent"], "Accept-Language": STOOQ_HEADERS["Accept-Language"]},
        timeout=settings.request_timeout_seconds,
    )
    params = {
        "interval": "1d",
        "range": "2y",
        "includePrePost": "false",
        "events": "div,splits",
    }
    last_error: Exception | None = None
    freshest_bars: list[Bar] | None = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            response = session.get(
                f"https://{host}/v8/finance/chart/{quote_symbol}",
                params=params,
                headers={
                    "User-Agent": STOOQ_HEADERS["User-Agent"],
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": STOOQ_HEADERS["Accept-Language"],
                    "Referer": "https://finance.yahoo.com/",
                    "Origin": "https://finance.yahoo.com",
                },
                timeout=settings.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not result:
                error_info = payload.get("chart", {}).get("error")
                raise RuntimeError(f"Yahoo chart payload is empty: {error_info}")

            timestamps = result.get("timestamp") or []
            indicators = result.get("indicators", {}).get("quote") or []
            if not timestamps or not indicators:
                raise RuntimeError("Yahoo chart response has no candles")

            quote = indicators[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []

            bars: list[Bar] = []
            for ts, open_price, high_price, low_price, close_price in zip(timestamps, opens, highs, lows, closes):
                if None in (open_price, high_price, low_price, close_price):
                    continue
                bars.append(
                    Bar(
                        date=datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                        open=float(open_price),
                        high=float(high_price),
                        low=float(low_price),
                        close=float(close_price),
                    )
                )
            bars.sort(key=lambda item: item.date)
            if len(bars) < 60:
                raise RuntimeError(f"Yahoo returned only {len(bars)} valid rows for {symbol}")
            if freshest_bars is None or bars[-1].date > freshest_bars[-1].date:
                freshest_bars = bars
        except Exception as exc:
            last_error = exc
    if freshest_bars is not None:
        return freshest_bars
    raise RuntimeError(f"Yahoo fetch failed for {symbol}: {last_error}") from last_error


def provider_chain_for(symbol: str) -> tuple[tuple[str, object], ...]:
    return (
        (("fmp", fetch_from_fmp), ("eodhd", fetch_from_eodhd), ("yahoo", fetch_from_yahoo), ("stooq", fetch_from_stooq))
        if symbol == INDEX_SYMBOL
        else (
            ("fmp", fetch_from_fmp),
            ("twelvedata", fetch_from_twelvedata),
            ("eodhd", fetch_from_eodhd),
            ("yahoo", fetch_from_yahoo),
            ("stooq", fetch_from_stooq),
        )
    )


def fetch_provider_result(
    session,
    symbol: str,
    settings: Settings,
    logger: logging.Logger,
    provider_name: str,
    provider,
) -> FetchResult:
    errors: list[str] = []
    for attempt in range(1, settings.request_retries + 1):
        try:
            bars = provider(session, symbol, settings)
            save_cached_bars(settings, symbol, bars, provider_name)
            logger.info(
                "%s data fetched from %s (%s rows, latest %s)",
                symbol,
                provider_name,
                len(bars),
                bars[-1].date,
            )
            return FetchResult(bars=bars, source=provider_name)
        except Exception as exc:
            errors.append(f"{provider_name} attempt {attempt}: {exc}")
            logger.warning("%s fetch failed via %s (attempt %s/%s): %s", symbol, provider_name, attempt, settings.request_retries, exc)
            if isinstance(exc, NonRetryableProviderError):
                break
            if attempt < settings.request_retries:
                time.sleep(settings.request_retry_delay_seconds)

    raise RuntimeError(f"Failed to fetch usable data for {symbol}: {' | '.join(errors)}")


def collect_fetch_candidates(symbol: str, settings: Settings, logger: logging.Logger) -> dict[str, FetchResult]:
    # 每个标的把所有 provider 的可用结果都收集起来，后面统一按“目标市场日期”做选择。
    # 这样允许不同标的来自不同源，但同一标的仍然整根K线只来自同一个 provider。
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install it with: pip install -r requirements.txt"
        ) from exc

    session = requests.Session()
    candidates: dict[str, FetchResult] = {}
    errors: list[str] = []
    for provider_name, provider in provider_chain_for(symbol):
        try:
            candidates[provider_name] = fetch_provider_result(session, symbol, settings, logger, provider_name, provider)
        except Exception as exc:
            errors.append(str(exc))
    if not candidates:
        raise RuntimeError(f"No usable providers for {symbol}: {' | '.join(errors)}")
    return candidates


def result_contains_market_date(result: FetchResult, market_date: str) -> bool:
    return bool(result.bars) and result.bars[0].date <= market_date <= result.bars[-1].date and any(
        bar.date == market_date for bar in result.bars
    )


def trim_result_to_market_date(result: FetchResult, market_date: str) -> FetchResult:
    trimmed_bars = [bar for bar in result.bars if bar.date <= market_date]
    if not trimmed_bars or trimmed_bars[-1].date != market_date:
        raise RuntimeError(f"{result.source} does not contain market date {market_date}")
    return FetchResult(bars=trimmed_bars, source=result.source)


def select_symbol_result(
    symbol: str,
    candidates: dict[str, FetchResult],
    market_date: str,
) -> FetchResult:
    provider_priority = {provider_name: index for index, (provider_name, _) in enumerate(provider_chain_for(symbol))}
    eligible_results = [
        trim_result_to_market_date(result, market_date)
        for provider_name, result in candidates.items()
        if result_contains_market_date(result, market_date)
    ]
    if not eligible_results:
        raise RuntimeError(f"No provider for {symbol} contains market date {market_date}")
    eligible_results.sort(key=lambda result: provider_priority.get(result.source, 999))
    return eligible_results[0]


def resolve_market_snapshot(
    index_candidates: dict[str, FetchResult],
    stock_candidate_map: dict[str, dict[str, FetchResult]],
    logger: logging.Logger,
) -> tuple[str, FetchResult, dict[str, FetchResult]]:
    all_candidates = {INDEX_SYMBOL: index_candidates, **stock_candidate_map}
    best_latest_dates = {
        symbol: max(result.bars[-1].date for result in candidates.values())
        for symbol, candidates in all_candidates.items()
    }
    target_market_date = max(best_latest_dates.values())
    symbols_missing_target = [symbol for symbol, latest_date in best_latest_dates.items() if latest_date < target_market_date]

    if symbols_missing_target:
        logger.warning(
            "Latest market date is not aligned yet. target=%s | stale=%s",
            target_market_date,
            ", ".join(
                f"{symbol}={best_latest_dates[symbol]} via "
                f"{max(all_candidates[symbol].values(), key=lambda item: item.bars[-1].date).source}"
                for symbol in symbols_missing_target
            ),
        )
        if is_market_data_finalized(target_market_date):
            raise RuntimeError(
                f"Latest finalized market date mismatch: expected all symbols at {target_market_date}, "
                + ", ".join(f"{symbol}={best_latest_dates[symbol]}" for symbol in sorted(best_latest_dates))
            )
        target_market_date = min(best_latest_dates.values())
        logger.warning(
            "Proceeding with common market date %s after intraday mismatch: %s",
            target_market_date,
            ", ".join(f"{symbol}={best_latest_dates[symbol]}" for symbol in sorted(best_latest_dates)),
        )

    index_result = select_symbol_result(INDEX_SYMBOL, index_candidates, target_market_date)
    stock_fetch_results = {
        symbol: select_symbol_result(symbol, candidates, target_market_date)
        for symbol, candidates in stock_candidate_map.items()
    }
    return target_market_date, index_result, stock_fetch_results


def build_metric(symbol: str, bars: list[Bar], lookback_days: int, with_ytd: bool) -> tuple[Metric, list[Bar]]:
    # 所有报警判断最终都要落回一组统一指标，所以这里故意把计算收口到一个函数。
    # 这样无论数据来自哪家 provider，报警口径都能保持一致。
    if len(bars) < lookback_days:
        raise RuntimeError(f"{symbol} data is too short")

    window_60d = bars[-lookback_days:]
    current = window_60d[-1]
    previous = bars[-2] if len(bars) >= 2 else None
    high_60d_bar = max(window_60d, key=lambda item: item.close)

    current_dt = datetime.strptime(current.date, "%Y-%m-%d")
    cutoff = (current_dt - timedelta(days=370)).strftime("%Y-%m-%d")
    window_1y = [bar for bar in bars if bar.date >= cutoff]
    if not window_1y:
        window_1y = bars[-min(len(bars), 252):]
    high_1y_bar = max(window_1y, key=lambda item: item.close)
    low_1y_bar = min(window_1y, key=lambda item: item.close)

    metric = Metric(
        symbol=symbol,
        current_date=current.date,
        current_close=current.close,
        previous_close=previous.close if previous else None,
        previous_date=previous.date if previous else None,
        daily_change_pct=((current.close / previous.close) - 1) * 100 if previous and previous.close else None,
        high_60d=high_60d_bar.close,
        high_60d_date=high_60d_bar.date,
        drawdown_60d_pct=((high_60d_bar.close - current.close) / high_60d_bar.close) * 100,
        high_1y=high_1y_bar.close,
        high_1y_date=high_1y_bar.date,
        low_1y=low_1y_bar.close,
        low_1y_date=low_1y_bar.date,
        drawdown_1y_pct=max(((high_1y_bar.close - current.close) / high_1y_bar.close) * 100, 0.0),
    )

    if with_ytd:
        year_bars = [bar for bar in bars if bar.date.startswith(current.date[:4])]
        year_start = year_bars[0]
        metric.ytd_start_date = year_start.date
        metric.ytd_start_close = year_start.close
        metric.ytd_change_pct = ((current.close / year_start.close) - 1) * 100

    return metric, window_60d


def resolve_chart_font():
    from matplotlib import font_manager

    for path in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]:
        if Path(path).exists():
            return font_manager.FontProperties(fname=path)
    return None


def build_chart(index_metric: Metric, bars: list[Bar]) -> bytes:
    # 图表不是主逻辑，而是提升可读性的附加能力。
    # 所以这里单独做函数，便于主流程在图表失败时退化成“只发文字版”。
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    chart_font = resolve_chart_font()
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(11, 6), dpi=140)
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#ffffff")

    for idx, bar in enumerate(bars):
        color = "#16a34a" if bar.close >= bar.open else "#dc2626"
        ax.vlines(idx, bar.low, bar.high, color=color, linewidth=1)
        ax.add_patch(
            Rectangle(
                (idx - 0.3, min(bar.open, bar.close)),
                0.6,
                max(abs(bar.close - bar.open), 1.0),
                facecolor=color,
                edgecolor=color,
            )
        )

    ax.set_title(
        f"{INDEX_DISPLAY_NAME}最近{len(bars)}个交易日K线\n今日 {format_signed_pct(index_metric.daily_change_pct)} | 60日回撤 {index_metric.drawdown_60d_pct:.2f}% | YTD {index_metric.ytd_change_pct:.2f}%",
        fontproperties=chart_font,
    )
    ax.set_ylabel("指数点位", fontproperties=chart_font)

    tick_step = max(1, len(bars) // 8)
    ticks = list(range(0, len(bars), tick_step))
    if ticks[-1] != len(bars) - 1:
        ticks.append(len(bars) - 1)
    ax.set_xticks(ticks)
    ax.set_xticklabels([bars[i].date[5:] for i in ticks])
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    output = io.BytesIO()
    plt.tight_layout()
    fig.savefig(output, format="png", bbox_inches="tight")
    plt.close(fig)
    return output.getvalue()


def parse_email_addresses(value: str) -> list[str]:
    return [address for _name, address in getaddresses([value]) if address]


def unique_email_addresses(addresses: list[str]) -> list[str]:
    seen = set()
    unique = []
    for address in addresses:
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(address)
    return unique


def resend_retry_wait_seconds(response: object | None, attempt: int, base_delay_seconds: float) -> float:
    if response is not None:
        headers = getattr(response, "headers", {})
        for header_name in ("retry-after", "ratelimit-reset"):
            header_value = headers.get(header_name)
            if header_value is None:
                continue
            try:
                return max(float(header_value), 0.0)
            except (TypeError, ValueError):
                pass
    return base_delay_seconds * (2 ** max(attempt - 1, 0))


def send_resend_email(settings: Settings, subject: str, plain: str, html: str, chart_bytes: bytes | None = None) -> None:
    import requests

    recipients = unique_email_addresses(parse_email_addresses(settings.email_recipients))
    if not recipients:
        raise RuntimeError("Resend email delivery failed: no valid recipients")

    logger = logging.getLogger("market_signal_monitor")
    minimum_request_interval = 1.0 / settings.resend_requests_per_second
    last_request_started: float | None = None
    failures: list[str] = []
    if chart_bytes is not None:
        attachments: list[dict[str, str]] | None = [
            {
                "filename": "market-chart.png",
                "content": base64.b64encode(chart_bytes).decode("ascii"),
                "content_id": "market_chart",
            }
        ]
    else:
        attachments = None

    for recipient_index, recipient in enumerate(recipients, start=1):
        payload: dict[str, object] = {
            "from": settings.resend_from,
            "to": [recipient],
            "subject": subject,
            "text": plain,
            "html": html,
        }
        if attachments is not None:
            payload["attachments"] = attachments

        # The same key is reused only for retries of this recipient. A new program run
        # gets a fresh key, while ambiguous network failures cannot create duplicates.
        idempotency_key = f"market-signal-{uuid.uuid4().hex}"
        failure_detail = "unknown error"

        for attempt in range(1, settings.resend_max_attempts + 1):
            if last_request_started is not None:
                elapsed = time.monotonic() - last_request_started
                pacing_wait = minimum_request_interval - elapsed
                if pacing_wait > 0:
                    time.sleep(pacing_wait)
            last_request_started = time.monotonic()

            response = None
            try:
                response = requests.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {settings.resend_api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": idempotency_key,
                    },
                    json=payload,
                    timeout=settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                failure_detail = f"network error: {exc}"
                retryable = True
            else:
                if response.status_code < 400:
                    logger.info(
                        "Resend accepted email for %s (%s/%s).",
                        recipient,
                        recipient_index,
                        len(recipients),
                    )
                    break
                failure_detail = f"HTTP {response.status_code} {response.text[:500]}"
                retryable = response.status_code == 429 or response.status_code >= 500

            if not retryable or attempt >= settings.resend_max_attempts:
                failures.append(f"{recipient}: {failure_detail}")
                logger.error("Resend delivery failed for %s after %s attempt(s): %s", recipient, attempt, failure_detail)
                break

            retry_wait = resend_retry_wait_seconds(response, attempt, settings.resend_retry_delay_seconds)
            logger.warning(
                "Resend delivery retry for %s after %s (attempt %s/%s, wait %.2fs).",
                recipient,
                failure_detail,
                attempt,
                settings.resend_max_attempts,
                retry_wait,
            )
            if retry_wait > 0:
                time.sleep(retry_wait)

    if failures:
        raise RuntimeError(
            f"Resend delivered to {len(recipients) - len(failures)}/{len(recipients)} recipients; "
            f"failures: {' | '.join(failures)}"
        )


def send_smtp_email(settings: Settings, subject: str, plain: str, html: str, chart_bytes: bytes | None = None) -> None:
    recipients = unique_email_addresses(parse_email_addresses(settings.email_recipients))
    if not recipients:
        raise RuntimeError("SMTP email delivery failed: no valid recipients")

    def build_message(recipient: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.smtp_sender
        message["To"] = recipient
        message.set_content(plain)
        message.add_alternative(html, subtype="html")
        if chart_bytes is not None:
            message.get_payload()[-1].add_related(
                chart_bytes,
                maintype="image",
                subtype="png",
                cid="<market_chart>",
                filename="market-chart.png",
            )
        return message

    def deliver(server: smtplib.SMTP) -> None:
        server.login(settings.smtp_user, settings.smtp_password)
        for recipient in recipients:
            server.send_message(build_message(recipient), to_addrs=[recipient])

    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context()) as server:
            deliver(server)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        if settings.smtp_use_starttls:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        deliver(server)


def send_email(settings: Settings, subject: str, plain: str, html: str, chart_bytes: bytes | None = None) -> None:
    if settings.email_provider == "resend":
        send_resend_email(settings, subject, plain, html, chart_bytes)
        return
    send_smtp_email(settings, subject, plain, html, chart_bytes)


def send_failure_email(settings: Settings, error_message: str, log_file: Path) -> None:
    # 这里专门发“异常通知”，是因为真正重要的不是每次都硬凑一封日报，
    # 而是当日报无法保证准确时，用户至少要第一时间知道服务出了问题。
    today = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = "[市场监控异常] 数据获取失败"
    plain = (
        "今天的市场日报没有正常生成。\n\n"
        f"失败时间: {today}\n"
        f"错误信息: {error_message}\n"
        f"日志文件: {log_file}\n\n"
        "建议先查看日志或手动执行 python market_monitor.py --force-send 复查。"
    )
    html = f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:20px;background:#f8fafc;font-family:'PingFang SC','Microsoft YaHei','Segoe UI',Arial,sans-serif;color:#0f172a;">
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0'>
      <tr>
        <td align='center'>
          <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:640px;background:#ffffff;border:1px solid #e2e8f0;border-radius:18px;'>
            <tr>
              <td style='padding:20px 20px 12px;'>
                <div style='display:inline-block;padding:5px 10px;background:#fee2e2;color:#991b1b;border-radius:999px;font-size:11px;font-weight:700;'>MONITOR ERROR</div>
                <div style='margin-top:12px;font-size:24px;line-height:1.35;font-weight:800;'>今天的市场日报发送失败</div>
              </td>
            </tr>
            <tr>
              <td style='padding:0 20px 20px;'>
                <div style='background:#fef2f2;border:1px solid #fecaca;border-radius:14px;padding:14px 16px;font-size:14px;line-height:1.8;color:#7f1d1d;'>
                  <div><strong>失败时间：</strong>{today}</div>
                  <div><strong>错误信息：</strong>{error_message}</div>
                  <div><strong>日志文件：</strong>{log_file}</div>
                </div>
                <div style='margin-top:14px;font-size:14px;line-height:1.8;color:#475569;'>建议先查看日志，或手动执行 <strong>python market_monitor.py --force-send</strong> 复查。</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
    send_email(settings, subject, plain, html)


def is_market_data_finalized(market_date: str, now_utc: datetime | None = None) -> bool:
    # 这里的判断目标不是“美国现在是不是休市”，而是“这根日线是不是已经可以当正式收盘数据用了”。
    # 对日报去重来说，这一点最关键：盘中快照可以发，但不能挡住收盘后的正式日报。
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    market_day = datetime.strptime(market_date, "%Y-%m-%d").date()
    today_et = now_et.date()
    if market_day < today_et:
        return True
    if market_day > today_et:
        return False
    return (now_et.hour, now_et.minute) >= (17, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor S&P 500 and major tech stock daily summaries.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-test-email", action="store_true")
    parser.add_argument("--force-send", action="store_true")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")
    settings = load_settings(base_dir)
    logger = setup_logging(settings.log_file)
    state = load_state(settings.state_file)
    today_local = datetime.now().astimezone().date().isoformat()

    try:
        # 这里先把每个标的在所有 provider 上的候选结果收集出来，
        # 再统一挑出“最新已完成市场日”的最佳数据，避免先成功先返回导致整次日报被旧一天的数据拖住。
        index_candidates = collect_fetch_candidates(INDEX_SYMBOL, settings, logger)
        stock_candidate_map = {symbol: collect_fetch_candidates(symbol, settings, logger) for symbol in settings.tech_stocks}
        target_market_date, index_result, stock_fetch_results = resolve_market_snapshot(index_candidates, stock_candidate_map, logger)

        index_metric, index_window = build_metric(INDEX_SYMBOL, index_result.bars, settings.index_lookback_days, True)
        stock_metrics = [
            build_metric(symbol, stock_fetch_results[symbol].bars, settings.stock_lookback_days, True)[0]
            for symbol in settings.tech_stocks
        ]
        stock_metrics.sort(key=lambda item: item.drawdown_60d_pct, reverse=True)

        index_60d_triggered = index_metric.drawdown_60d_pct >= settings.index_60d_threshold_pct
        index_ytd_triggered = index_metric.ytd_change_pct is not None and index_metric.ytd_change_pct <= -settings.index_ytd_threshold_pct
        triggered_stocks = [metric for metric in stock_metrics if metric.drawdown_60d_pct >= settings.stock_60d_threshold_pct]
        triggered_stock_symbols = {metric.symbol for metric in triggered_stocks}
        source_summary = summarize_sources(index_result, stock_fetch_results)

        triggers: list[str] = []
        if index_60d_triggered:
            triggers.append(f"{INDEX_SHORT_NAME} 60日回撤 {index_metric.drawdown_60d_pct:.2f}%")
        if index_ytd_triggered:
            triggers.append(f"{INDEX_SHORT_NAME} YTD 跌幅 {abs(index_metric.ytd_change_pct):.2f}%")
        triggers.extend(f"{metric.symbol} 60日回撤 {metric.drawdown_60d_pct:.2f}%" for metric in triggered_stocks)

        logger.info(
            "Market date=%s | %sDay=%s | %s60D=%.2f%% | %sYTD=%.2f%% | WorstStock=%s %.2f%% | Sources=%s | Triggered=%s",
            index_metric.current_date,
            INDEX_SYMBOL,
            format_signed_pct(index_metric.daily_change_pct),
            INDEX_SYMBOL,
            index_metric.drawdown_60d_pct,
            INDEX_SYMBOL,
            index_metric.ytd_change_pct,
            stock_metrics[0].symbol,
            stock_metrics[0].drawdown_60d_pct,
            source_summary,
            len(triggers),
        )
        market_data_finalized = is_market_data_finalized(target_market_date)
        logger.info(
            "Market data stage=%s",
            "finalized" if market_data_finalized else "intraday",
        )

        if args.dry_run:
            logger.info("[DRY RUN] %s", " | ".join(triggers) if triggers else "No trigger")
            return 0

        if not args.send_test_email:
            last_processed_date = state.get("last_finalized_market_date") or state.get("last_processed_market_date")
            last_intraday_date = state.get("last_intraday_market_date")
            # 是否跳过发送，最终只认“市场日期”而不是日历日期。
            # 这样周末、节假日或盘后延迟更新时，都不会因为本地时间跨天而误发重复邮件。
            if not args.force_send:
                if market_data_finalized and last_processed_date == index_metric.current_date:
                    logger.info("No new finalized U.S. market session data is available yet; skip sending.")
                    return 0
                if not market_data_finalized and last_intraday_date == index_metric.current_date:
                    logger.info("Intraday snapshot for the current U.S. market session was already sent; skip sending.")
                    return 0
            if not args.force_send and last_processed_date and index_metric.current_date < last_processed_date:
                error_message = f"最新市场日期 {index_metric.current_date} 早于上次已发送日期 {last_processed_date}"
                logger.error("%s", error_message)
                if state.get("last_failure_notice_date") != today_local:
                    try:
                        send_failure_email(settings, error_message, settings.log_file)
                        state["last_failure_notice_date"] = today_local
                        save_state(settings.state_file, state)
                        logger.info("Failure notice email sent.")
                    except Exception as failure_exc:
                        logger.exception("Failed to send failure notice email: %s", failure_exc)
                return 1
    except Exception as exc:
        logger.exception("Monitor run failed: %s", exc)
        # 这里的目标不是继续“硬发一封日报”，而是明确告诉用户今天链路断了。
        # 对真实交易提醒来说，及时暴露故障比伪造一份看似正常的日报更重要。
        if not args.send_test_email and not args.dry_run and state.get("last_failure_notice_date") != today_local:
            try:
                send_failure_email(settings, str(exc), settings.log_file)
                state["last_failure_notice_date"] = today_local
                save_state(settings.state_file, state)
                logger.info("Failure notice email sent.")
            except Exception as failure_exc:
                logger.exception("Failed to send failure notice email: %s", failure_exc)
        return 1

    plain_lines = [
        "市场监控测试邮件" if args.send_test_email else "市场日报",
        "",
        f"市场日期: {index_metric.current_date}",
    ]
    if args.send_test_email:
        plain_lines.extend(["说明: 这是一封测试邮件，用来确认日报样式和发送链路正常。", ""])
    elif not market_data_finalized:
        plain_lines.extend(["说明: 当前基于美股盘中日线快照生成；正式收盘后的同一市场日期日报仍会继续发送。", ""])
    if triggers:
        plain_lines.extend(["触发项:"])
        plain_lines.extend(f"- {item}" for item in triggers)
        plain_lines.append("")
    plain_lines.extend(
        [
            f"{INDEX_DISPLAY_NAME}:",
            f"- 最新收盘: {index_metric.current_close:.2f}",
            f"- 今日涨跌幅: {format_signed_pct(index_metric.daily_change_pct)}"
            + (f" (昨收 {index_metric.previous_close:.2f})" if index_metric.previous_close is not None else ""),
            f"- 60日回撤: {index_metric.drawdown_60d_pct:.2f}% (阈值 {settings.index_60d_threshold_pct:.2f}%)",
            f"- YTD涨跌幅: {index_metric.ytd_change_pct:.2f}% (跌幅提醒阈值 {settings.index_ytd_threshold_pct:.2f}%)",
            f"- 近1年最高收盘: {index_metric.high_1y:.2f} ({index_metric.high_1y_date})",
            f"- 近1年最低收盘: {index_metric.low_1y:.2f} ({index_metric.low_1y_date})",
            f"- 距近1年最高收盘: {format_from_high_pct(index_metric.drawdown_1y_pct)}",
            "",
            "科技股组合:",
        ]
    )
    plain_lines.extend(
        f"- {metric.symbol}: 最新 {metric.current_close:.2f} | 今日 {format_signed_pct(metric.daily_change_pct)} | 60日回撤 {metric.drawdown_60d_pct:.2f}% | YTD {metric.ytd_change_pct:.2f}% | 近1年高/低 {metric.high_1y:.2f}/{metric.low_1y:.2f} | 距1年高点 {format_from_high_pct(metric.drawdown_1y_pct)}"
        for metric in stock_metrics
    )
    plain_lines.extend(["", f"数据源: {source_summary}"])
    plain = "\n".join(plain_lines)

    trigger_section = ""
    if args.send_test_email:
        trigger_section = (
            "<tr><td style='padding:0 18px 14px;'><div style='background:#eff6ff;border:1px solid #bfdbfe;"
            "border-radius:14px;padding:14px 16px;'><div style='font-size:16px;font-weight:800;color:#1d4ed8;'>测试说明</div>"
            "<div style='margin-top:6px;font-size:15px;line-height:1.8;color:#1e3a8a;'>这是一封测试邮件，用来确认日报样式和发送链路正常。</div>"
            "</div></td></tr>"
        )
    elif not market_data_finalized:
        trigger_section = (
            "<tr><td style='padding:0 18px 14px;'><div style='background:#fff7ed;border:1px solid #fdba74;"
            "border-radius:14px;padding:14px 16px;'><div style='font-size:16px;font-weight:800;color:#9a3412;'>盘中快照说明</div>"
            "<div style='margin-top:6px;font-size:15px;line-height:1.8;color:#9a3412;'>当前邮件基于美股盘中日线快照生成；正式收盘后的同一市场日期日报仍会继续发送。</div>"
            "</div></td></tr>"
        )
    elif triggers:
        trigger_items = "".join(
            f"<div style='margin-top:6px;font-size:15px;line-height:1.8;color:#7f1d1d;'>• {item}</div>"
            for item in triggers
        )
        trigger_section = (
            "<tr><td style='padding:0 18px 14px;'><div style='background:#fef2f2;border:1px solid #fecaca;"
            "border-radius:14px;padding:14px 16px;'><div style='font-size:16px;font-weight:800;color:#7f1d1d;'>今日触发项</div>"
            f"{trigger_items}</div></td></tr>"
        )

    index_badge_60d = (
        "<span style='display:inline-block;margin-left:6px;padding:2px 8px;background:#fee2e2;color:#b91c1c;border-radius:999px;font-size:11px;font-weight:700;'>触发预警</span>"
        if index_60d_triggered
        else ""
    )
    index_badge_ytd = (
        "<span style='display:inline-block;margin-left:6px;padding:2px 8px;background:#fee2e2;color:#b91c1c;border-radius:999px;font-size:11px;font-weight:700;'>触发预警</span>"
        if index_ytd_triggered
        else ""
    )
    source_summary_html = f"<span style='font-size:13px;line-height:1.7;color:#64748b;'>数据源：{source_summary}</span>"

    stock_rows_parts: list[str] = []
    for metric in stock_metrics:
        stock_triggered = metric.symbol in triggered_stock_symbols
        stock_badge = (
            "<span style='display:inline-block;margin-left:6px;padding:2px 8px;background:#fee2e2;color:#b91c1c;border-radius:999px;font-size:11px;font-weight:700;'>触发预警</span>"
            if stock_triggered
            else ""
        )
        stock_rows_parts.append(
            f"<tr><td style='padding:0 0 10px;'><div style='background:{'#fef2f2' if stock_triggered else '#f8fafc'};"
            f"border:1px solid {'#fecaca' if stock_triggered else '#e2e8f0'};border-radius:14px;padding:12px 14px;'>"
            f"<div style='font-size:16px;font-weight:800;color:#0f172a;'>{metric.symbol}{stock_badge}</div>"
            f"<div style='margin-top:4px;font-size:14px;line-height:1.7;color:#475569;'>最新价 <strong style='font-weight:800;color:#0f172a;'>{metric.current_close:.2f}</strong> | 今日 <strong style='font-weight:800;color:{change_color(metric.daily_change_pct)};'>{format_signed_pct(metric.daily_change_pct)}</strong> | 60日高点 {metric.high_60d:.2f}</div>"
            f"<div style='margin-top:4px;font-size:14px;line-height:1.7;color:{'#b91c1c' if stock_triggered else '#334155'};font-weight:700;'>60日回撤 {metric.drawdown_60d_pct:.2f}% | YTD {metric.ytd_change_pct:.2f}%</div>"
            f"<div style='margin-top:4px;font-size:13px;line-height:1.7;color:#64748b;'>近1年最高 <strong style='font-weight:800;color:#334155;'>{metric.high_1y:.2f}</strong>，近1年最低 <strong style='font-weight:800;color:#334155;'>{metric.low_1y:.2f}</strong>，当前距离近1年最高点 <strong style='font-weight:800;color:#334155;'>{format_from_high_pct(metric.drawdown_1y_pct)}</strong></div>"
            "</div></td></tr>"
        )
    stock_rows = "".join(stock_rows_parts)
    stock_summary = (
        f"以下 {', '.join(metric.symbol for metric in triggered_stocks)} 的60日回撤已超过 {settings.stock_60d_threshold_pct:.0f}%"
        if triggered_stocks
        else f"当前 {len(settings.tech_stocks)} 家科技公司均未超过 {settings.stock_60d_threshold_pct:.0f}% 回撤阈值"
    )
    index_previous_close_text = f"{index_metric.previous_close:.2f}" if index_metric.previous_close is not None else "N/A"

    title = "市场监控测试邮件" if args.send_test_email else ("市场日报（盘中快照）" if not market_data_finalized else "市场日报")
    description = (
        "这是一封测试邮件，用来确认日报样式和发送链路正常。"
        if args.send_test_email
        else (
            f"市场日期：{index_metric.current_date} | 当前为盘中快照，正式收盘后同一市场日期仍会发送日报。"
            if not market_data_finalized
            else f"市场日期：{index_metric.current_date} | 当前邮件基于最新已完成的美股市场日生成。"
        )
    )
    chart_bytes: bytes | None = None
    try:
        chart_bytes = build_chart(index_metric, index_window)
    except Exception as exc:
        # 图表失败不应该拖垮正文邮件。
        # 用户最需要的是指标和触发结果，所以这里主动退化成文字版摘要。
        logger.exception("Chart generation failed, sending email without chart: %s", exc)
    chart_block_html = (
        "<img src='cid:market_chart' alt='index chart' style='display:block;width:100%;height:auto;border-radius:12px;'>"
        if chart_bytes is not None
        else "<div style='padding:18px 12px;font-size:14px;line-height:1.8;color:#64748b;text-align:center;'>图表生成失败，本次邮件先发送文字版摘要。</div>"
    )

    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <meta name='x-apple-disable-message-reformatting'>
    <meta name='color-scheme' content='light'>
  </head>
  <body style="margin:0;padding:0;background:#eef2f7;font-family:'PingFang SC','Microsoft YaHei','Segoe UI',Arial,sans-serif;color:#0f172a;">
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#eef2f7;'>
      <tr>
        <td align='center' style='padding:16px 10px 24px;'>
          <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:680px;background:#fff;border:1px solid #dbe4ee;border-radius:20px;'>
            <tr>
              <td style='padding:18px;'>
                <div style='display:inline-block;padding:5px 10px;background:#e0f2fe;color:#075985;border-radius:999px;font-size:11px;font-weight:700;'>MARKET MONITOR</div>
                <div style='margin-top:14px;font-size:28px;line-height:1.24;font-weight:800;'>{title}</div>
                <div style='margin-top:8px;font-size:15px;line-height:1.7;color:#475569;'>{description}</div>
              </td>
            </tr>
            {trigger_section}
            <tr>
              <td style='padding:0 18px 6px;'>
                <div style='font-size:18px;font-weight:800;margin-bottom:10px;'>{INDEX_DISPLAY_NAME}总览</div>
                <div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:14px 16px;'>
                  <div style='font-size:14px;line-height:1.8;color:#334155;'><strong style='color:#0f172a;'>最新收盘</strong> {index_metric.current_close:.2f} <span style='color:#64748b;'>({index_metric.current_date})</span></div>
                  <div style='font-size:14px;line-height:1.8;color:#334155;'><strong style='color:#0f172a;'>今日涨跌幅</strong> <strong style='font-weight:800;color:{change_color(index_metric.daily_change_pct)};'>{format_signed_pct(index_metric.daily_change_pct)}</strong><span style='color:#64748b;'> | 昨收 {index_previous_close_text}</span></div>
                  <div style='font-size:14px;line-height:1.8;color:#334155;'><strong style='color:#0f172a;'>60日回撤</strong> {index_metric.drawdown_60d_pct:.2f}% {index_badge_60d}<span style='color:#64748b;'> | 60日高点 {index_metric.high_60d:.2f}</span></div>
                  <div style='font-size:14px;line-height:1.8;color:#334155;'><strong style='color:#0f172a;'>YTD涨跌幅</strong> {index_metric.ytd_change_pct:.2f}% {index_badge_ytd}<span style='color:#64748b;'> | 年初收盘 {index_metric.ytd_start_close:.2f}</span></div>
                  <div style='font-size:13px;line-height:1.7;color:#64748b;'>近1年最高 <strong style='font-weight:800;color:#334155;'>{index_metric.high_1y:.2f}</strong>，近1年最低 <strong style='font-weight:800;color:#334155;'>{index_metric.low_1y:.2f}</strong>，当前距离近1年最高点 <strong style='font-weight:800;color:#334155;'>{format_from_high_pct(index_metric.drawdown_1y_pct)}</strong></div>
                </div>
              </td>
            </tr>
            <tr>
              <td style='padding:12px 18px 0;'>
                <div style='font-size:18px;font-weight:800;margin-bottom:10px;'>{INDEX_DISPLAY_NAME}最近 {settings.index_lookback_days} 个交易日K线</div>
                <div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:10px;'>
                  {chart_block_html}
                </div>
              </td>
            </tr>
            <tr>
              <td style='padding:18px;'>
                <div style='font-size:18px;font-weight:800;margin-bottom:10px;'>科技股组合概览</div>
                <div style='margin-bottom:4px;font-size:14px;line-height:1.7;color:#475569;'>{stock_summary}</div>
                <div style='margin-bottom:10px;'>{source_summary_html}</div>
                <table role='presentation' width='100%' cellpadding='0' cellspacing='0'>
                  {stock_rows}
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""

    subject = (
        f"[市场监控测试] {INDEX_SHORT_NAME} + 科技股组合日报"
        if args.send_test_email
        else (
            f"[市场快照] {len(triggers)} 项触发 | {INDEX_SHORT_NAME} + 科技股组合"
            if not market_data_finalized and triggers
            else (
                f"[市场快照] {INDEX_SHORT_NAME} + 科技股组合"
                if not market_data_finalized
                else (f"[市场提醒] {len(triggers)} 项触发 | {INDEX_SHORT_NAME} + 科技股组合日报" if triggers else f"[市场日报] {INDEX_SHORT_NAME} + 科技股组合")
            )
        )
    )

    try:
        send_email(settings, subject, plain, html, chart_bytes)
        if not args.send_test_email:
            # 只有真正发信成功以后才更新 last_processed_market_date，
            # 这样才能避免“邮件没发出去，但状态却已经记成发过了”的隐性漏报。
            if market_data_finalized:
                state["last_processed_market_date"] = index_metric.current_date
                state["last_finalized_market_date"] = index_metric.current_date
                state.pop("last_intraday_market_date", None)
            else:
                state["last_intraday_market_date"] = index_metric.current_date
            state.pop("last_failure_notice_date", None)
            save_state(settings.state_file, state)
        logger.info("%s", "Test email sent." if args.send_test_email else "Daily email sent.")
        return 0
    except Exception as exc:
        logger.exception("Email delivery failed: %s", exc)
        if not args.send_test_email and state.get("last_failure_notice_date") != today_local:
            try:
                send_failure_email(settings, f"日报发送失败: {exc}", settings.log_file)
                state["last_failure_notice_date"] = today_local
                save_state(settings.state_file, state)
                logger.info("Failure notice email sent.")
            except Exception as failure_exc:
                logger.exception("Failed to send failure notice email: %s", failure_exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
