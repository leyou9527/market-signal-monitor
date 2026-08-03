import argparse
from datetime import date


def format_candle(candle) -> str:
    timestamp = getattr(candle, "timestamp", None)
    open_price = getattr(candle, "open", None)
    high_price = getattr(candle, "high", None)
    low_price = getattr(candle, "low", None)
    close_price = getattr(candle, "close", None)
    return f"ts={timestamp} open={open_price} high={high_price} low={low_price} close={close_price}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Longbridge historical candlesticks for S&P 500 candidates and tech stocks.")
    parser.add_argument(
        "--symbols",
        default="AAPL.US,MSFT.US,NVDA.US,META.US,GOOGL.US,AMZN.US,TSM.US,AVGO.US,GSPC.US,^GSPC.US,.INX,SPX.IND",
        help="Comma-separated Longbridge symbols to test",
    )
    parser.add_argument("--start", default="2026-01-01", help="Start date in YYYY-MM-DD")
    parser.add_argument("--end", default="2026-03-31", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    try:
        from longbridge.openapi import AdjustType, Config, Period, QuoteContext
    except ModuleNotFoundError:
        print("Missing dependency: longbridge")
        print("Install it with: pip install longbridge")
        return 1

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]

    config = Config.from_apikey_env()
    ctx = QuoteContext(config)

    print("Testing Longbridge historical daily candlesticks")
    print(f"Date range: {start_date} -> {end_date}")
    print(f"Symbols: {', '.join(symbols)}")
    print()

    overall_success = False
    for symbol in symbols:
        print(f"=== {symbol} ===")
        try:
            candles = ctx.history_candlesticks_by_date(
                symbol,
                Period.Day,
                AdjustType.NoAdjust,
                start_date,
                end_date,
            )
            count = len(candles)
            if count == 0:
                print("SUCCESS, but returned 0 candles")
                continue
            overall_success = True
            print(f"SUCCESS, candles={count}")
            print(f"first: {format_candle(candles[0])}")
            print(f"last:  {format_candle(candles[-1])}")
        except Exception as exc:
            print(f"FAILED: {exc}")
        print()

    return 0 if overall_success else 2


if __name__ == "__main__":
    raise SystemExit(main())
