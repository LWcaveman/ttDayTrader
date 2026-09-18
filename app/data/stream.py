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
    
    # Include Index Gate benchmarks (e.g. SPY, QQQ) so their intraday VWAP is tracked in real-time
    gate_cfg = config.get("market_gate", {}) if config else {}
    gate_enabled = gate_cfg.get("enabled", True)
    idx_ticker = gate_cfg.get("index_ticker", "SPY")
    reg_ticker = gate_cfg.get("regime_ticker", "QQQ")

    # Include Midday Reversion tickers so their trackers are live and ready for 11:30
    midday_cfg = config.get("midday_reversion", {}) if config else {}
    midday_enabled = midday_cfg.get("enabled", True)
    midday_tickers = midday_cfg.get("tickers", ["CONL", "NVDL", "TQQQ", "SOXL", "UPRO", "BITX"]) if midday_enabled else []
    
    extra_tickers = []
    if gate_enabled:
        for sym in [idx_ticker, reg_ticker]:
            if sym and sym not in active_tickers and sym not in active_positions:
                extra_tickers.append(sym)
    if midday_enabled:
        for sym in midday_tickers:
            if sym and sym not in active_tickers and sym not in active_positions and sym not in extra_tickers:
                extra_tickers.append(sym)

    monitored_tickers = list(set(active_tickers + list(active_positions.keys()) + extra_tickers))
    
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

    stream_symbols = list(trackers.keys())
    if not stream_symbols:
        print("No tickers bootstrapped successfully. Exiting.")
        return

    print(f"Successfully bootstrapped {len(stream_symbols)} tickers.")
    print("Connecting to DXLink WebSocket...")
    tz = pytz.timezone('America/New_York')

    async with DXLinkStreamer(prod_session) as streamer:
        await streamer.subscribe(Trade, stream_symbols)
        print("Data stream active. Monitoring 1-minute aggregations for crossovers & Index Gate alignment...")

        async for trade_event in streamer.listen(Trade):
            ticker = trade_event.event_symbol
            price = float(trade_event.price)
            volume = float(getattr(trade_event, 'size', 1.0) or 1.0)
            now_dt = datetime.now(tz)

            if ticker not in trackers:
                continue

            tracker = trackers[ticker]
            tracker.update_tick(price, volume, now_dt)

            # 1. Exit Evaluation (Now passing prod_session and tracker)
            if ticker in active_positions:
                await check_and_execute_exit(
                    ticker=ticker,
                    current_price=price,
                    current_dt=now_dt,
                    prod_session=prod_session,
                    config=config,
                    db=db,
                    tracker=tracker
                )
                continue

            # 2. Entry Evaluation (Now passing prod_session)
            active_strat = config.get('active_strategy', 'MORNING_MOMENTUM')
            if is_within_trading_window(config, strategy=active_strat) and len(active_positions) < 1:
                # Benchmark indices are monitored for market tide only, not entered as trades
                if gate_enabled and ticker in [idx_ticker, reg_ticker]:
                    continue

                if active_strat == 'VWAP_RECLAIM':
                    reclaim_sig = tracker.check_vwap_reclaim(config=config)
                    if reclaim_sig:
                        # Evaluate Intraday Market Gate Tide Alignment (Longs require SPY above VWAP)
                        if gate_enabled and gate_cfg.get("require_intraday_vwap_alignment", True):
                            idx_tracker = trackers.get(idx_ticker)
                            if idx_tracker and idx_tracker.is_ready and idx_tracker.vwap > 0:
                                if idx_tracker.current_close < idx_tracker.vwap:
                                    print(f"[INDEX GATE REJECT] {ticker} VWAP Reclaim skipped: {idx_ticker} (${idx_tracker.current_close:.2f}) is BELOW intraday VWAP (${idx_tracker.vwap:.2f}). Longs require upward market tide.")
                                    continue

                        print(f"[VWAP RECLAIM DETECTED] {ticker} | Price: {price:.2f} | VWAP: {tracker.vwap:.2f} | Sweep Low: {reclaim_sig['sweep_low']:.2f}")
                        await evaluate_setup(
                            ticker=ticker,
                            tracker=tracker,
                            current_price=price,
                            prod_session=prod_session,
                            config=config,
                            db=db,
                            strategy='VWAP_RECLAIM',
                            reclaim_info=reclaim_sig
                        )
                else:
                    min_close = float(config.get('execution', {}).get('min_close_pct', 0.60))
                    min_vol = float(config.get('execution', {}).get('min_vol_ratio', 0.80))
                    if tracker.check_crossover(current_price=price, min_close_pct=min_close, min_vol_ratio=min_vol):
                        # Evaluate Intraday Market Gate Tide Alignment
                        if gate_enabled and gate_cfg.get("require_intraday_vwap_alignment", True):
                            inverse_tickers = gate_cfg.get("inverse_tickers", ["PSQ", "SH"])
                            is_inverse = ticker in inverse_tickers

                            if is_inverse:
                                # Inverses require market declining intraday (QQQ below VWAP)
                                reg_tracker = trackers.get(reg_ticker)
                                if reg_tracker and reg_tracker.is_ready and reg_tracker.vwap > 0:
                                    if reg_tracker.current_close >= reg_tracker.vwap:
                                        print(f"[INDEX GATE REJECT] {ticker} setup skipped: {reg_ticker} (${reg_tracker.current_close:.2f}) is ABOVE intraday VWAP (${reg_tracker.vwap:.2f}). Inverses require downward market tide.")
                                        continue
                            else:
                                # Longs require market lifting intraday (SPY above VWAP)
                                idx_tracker = trackers.get(idx_ticker)
                                if idx_tracker and idx_tracker.is_ready and idx_tracker.vwap > 0:
                                    if idx_tracker.current_close < idx_tracker.vwap:
                                        print(f"[INDEX GATE REJECT] {ticker} setup skipped: {idx_ticker} (${idx_tracker.current_close:.2f}) is BELOW intraday VWAP (${idx_tracker.vwap:.2f}). Longs require upward market tide.")
                                        continue

                        print(f"[CROSS DETECTED] {ticker} | Price: {price:.2f} | VWAP: {tracker.vwap:.2f} | 9EMA: {tracker.ema_9:.2f}")
                        await evaluate_setup(
                            ticker=ticker,
                            tracker=tracker,
                            current_price=price,
                            prod_session=prod_session,
                            config=config,
                            db=db,
                            strategy='MORNING_MOMENTUM'
                        )

            # 3. Midday Mean-Reversion Entry Evaluation (Engine 2: 11:30 - 13:30)
            if midday_enabled and is_within_trading_window(config, strategy='MIDDAY_REVERSION') and len(active_positions) < 1:
                if ticker in midday_tickers:
                    midday_sig = tracker.check_midday_reversion(config=config)
                    if midday_sig:
                        print(f"[MIDDAY REVERSION DETECTED] {ticker} | Price: {price:.2f} | VWAP: {tracker.vwap:.2f} | -2.5SD: {tracker.vwap_lower_2_5sd:.2f} | Flush Low: {midday_sig['flush_low']:.2f}")
                        await evaluate_setup(
                            ticker=ticker,
                            tracker=tracker,
                            current_price=price,
                            prod_session=prod_session,
                            config=config,
                            db=db,
                            strategy='MIDDAY_REVERSION',
                            midday_info=midday_sig
                        )