import asyncio
import concurrent.futures
from datetime import datetime
import pytz
from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Trade
from app.data.indicators import IntradayTracker
from app.execution.risk import (
    active_positions,
    hydrate_positions,
    evaluate_setup,
    check_and_execute_exit
)
from app.scanner.premarket import is_within_trading_window

def _bootstrap_single_tracker(ticker):
    tracker = IntradayTracker(ticker)
    if tracker.bootstrap_today():
        return tracker
    return None

async def start_market_stream(prod_session, cert_session, active_tickers, config, db):
    await hydrate_positions(db)
    
    monitored_tickers = list(set(active_tickers + list(active_positions.keys())))
    
    print(f"Bootstrapping {len(monitored_tickers)} trackers using 20 concurrent threads (this should take ~15 seconds)...")
    trackers = {}
    
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [
            loop.run_in_executor(pool, _bootstrap_single_tracker, ticker)
            for ticker in monitored_tickers
        ]
        
        results = await asyncio.gather(*futures)
        
        for tracker in results:
            if tracker:
                trackers[tracker.ticker] = tracker

    active_tickers = list(trackers.keys())
    if not active_tickers:
        print("No tickers bootstrapped successfully. Exiting.")
        return

    print(f"Successfully bootstrapped {len(active_tickers)} tickers.")
    print("Connecting to DXLink WebSocket...")
    tz = pytz.timezone('America/New_York')

    async with DXLinkStreamer(prod_session) as streamer:
        await streamer.subscribe(Trade, active_tickers)
        print("Data stream active. Monitoring 1-minute aggregations for crossovers...")

        async for trade_event in streamer.listen(Trade):
            ticker = trade_event.event_symbol
            price = float(trade_event.price)
            volume = float(getattr(trade_event, 'size', 1.0) or 1.0)
            now_dt = datetime.now(tz)

            if ticker not in trackers:
                continue

            tracker = trackers[ticker]
            tracker.update_tick(price, volume, now_dt)

            # 1. Exit Evaluation (Now passing prod_session)
            if ticker in active_positions:
                await check_and_execute_exit(
                    ticker=ticker,
                    current_price=price,
                    current_dt=now_dt,
                    prod_session=prod_session,
                    config=config,
                    db=db
                )
                continue

            # 2. Entry Evaluation (Now passing prod_session)
            if is_within_trading_window(config) and len(active_positions) < 1:
                if tracker.check_crossover(current_price=price):
                    print(f"[CROSS DETECTED] {ticker} | Price: {price:.2f} | VWAP: {tracker.vwap:.2f} | 9EMA: {tracker.ema_9:.2f}")
                    await evaluate_setup(
                        ticker=ticker,
                        tracker=tracker,
                        current_price=price,
                        prod_session=prod_session,
                        config=config,
                        db=db
                    )