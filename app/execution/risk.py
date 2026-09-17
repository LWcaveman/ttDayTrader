import asyncio
from datetime import datetime, time
import pytz

# NEW ROBINHOOD IMPORTS
from app.execution.rh_router import get_rh_buying_power, route_rh_market_order

active_positions = {}
rejected_cooldowns = set()

async def hydrate_positions(db):
    """Loads open positions from SQLite into memory on daemon boot."""
    open_trades = await db.get_open_positions()
    tz = pytz.timezone('America/New_York')
    for trade in open_trades:
        ticker = trade['ticker']
        entry_dt = datetime.fromisoformat(trade['entry_time'])
        if entry_dt.tzinfo is None:
            entry_dt = tz.localize(entry_dt)
        active_positions[ticker] = {
            'shares': trade['shares'],
            'entry_price': trade['entry_price'],
            'stop_loss': trade['stop_loss'],
            'target': trade['target'],
            'unit': trade['target'] - trade['entry_price'],
            'entry_time': entry_dt
        }
    if open_trades:
        print(f"[RECOVERY] Hydrated {len(open_trades)} active positions from SQLite.")

async def evaluate_setup(ticker, tracker, current_price, prod_session, config, db):
    if len(active_positions) >= 1 or ticker in active_positions:
        return

    if ticker in rejected_cooldowns:
        return

    lod = tracker.lod
    if current_price <= lod:
        return

    # FETCH LIVE BALANCE FROM ROBINHOOD
    raw_buying_power = await get_rh_buying_power()
    buying_power = raw_buying_power * 0.95 

    trades_today = await db.get_trades_count_today()
    max_trades_cfg = config.get('risk_management', {}).get('max_trades_per_day')
    if max_trades_cfg is not None:
        max_trades = int(max_trades_cfg)
    elif raw_buying_power < 100.00:
        max_trades = 1
    elif raw_buying_power < 300.00:
        max_trades = 2
    else:
        max_trades = 3

    if trades_today >= max_trades:
        print(f"[{ticker}] Passed: Daily trade limit ({max_trades}) reached. Staying in cash.")
        rejected_cooldowns.add(ticker)
        return

    unit = current_price - lod
    unit_pct = (unit / current_price) if current_price > 0 else 1.0

    # Morning vs Midday Execution Filter
    tz = pytz.timezone('America/New_York')
    now_time = datetime.now(tz).time()

    morning_cutoff_str = config.get('execution', {}).get('morning_cutoff', '10:45')
    morning_cutoff = datetime.strptime(morning_cutoff_str, "%H:%M").time()

    if now_time > morning_cutoff:
        max_midday_unit = float(config.get('execution', {}).get('midday_max_unit_pct', 0.0075))
        if unit_pct > max_midday_unit:
            print(f"[{ticker}] Passed: Midday entry unit ({unit_pct*100:.2f}%) exceeds tight threshold ({max_midday_unit*100:.2f}%). Staying in cash.")
            rejected_cooldowns.add(ticker)
            return

    target = current_price + unit
    stop_loss = current_price - (unit / 3.0)

    risk_pct = config['risk_management']['risk_pct_per_trade']
    max_risk = config['risk_management']['max_risk_dollars']
    risk_dollars = min(buying_power * risk_pct, max_risk)

    stop_distance = current_price - stop_loss
    if stop_distance <= 0:
        return

    shares = round(risk_dollars / stop_distance, 4)
    cost_basis = round(shares * current_price, 2)

    # Note: Removed the $5.00 Tastytrade minimum limit here since RH allows micro-fractionals
    if cost_basis > buying_power:
        shares = round(buying_power / current_price, 4)
        cost_basis = round(shares * current_price, 2)
        if cost_basis <= 0:
            return

    print(f"[{ticker}] Setup Detected: Entry ${current_price:.2f} | Target ${target:.2f} | Stop ${stop_loss:.2f}")
    print(f"[{ticker}] Executing {shares} shares via ROBINHOOD (${cost_basis:.2f})...")

    tz = pytz.timezone('America/New_York')
    now_dt = datetime.now(tz)

    try:
        # EXECUTE BUY VIA ROBINHOOD
        await route_rh_market_order(ticker, shares, action="BUY")

        await db.open_position(
            ticker=ticker,
            shares=shares,
            entry_price=current_price,
            stop_loss=stop_loss,
            target=target,
            entry_time=now_dt.isoformat()
        )

        active_positions[ticker] = {
            'shares': shares,
            'entry_price': current_price,
            'stop_loss': stop_loss,
            'target': target,
            'unit': unit,
            'entry_time': now_dt
        }
        print(f"[{ticker}] Robinhood Buy Order Routed Successfully. Position active.")
    except Exception as e:
        print(f"[{ticker}] Robinhood Order Failed: {e}")
        rejected_cooldowns.add(ticker) 

async def check_and_execute_exit(ticker, current_price, current_dt, prod_session, config, db):
    position = active_positions.get(ticker)
    if not position:
        return

    entry_price = position['entry_price']
    stop_loss = position['stop_loss']
    target = position['target']
    entry_time = position['entry_time']
    shares = position['shares']
    unit = position.get('unit', target - entry_price)

    # 1. Check +1.5R Breakeven Ratchet (Protects small account gains)
    ratchet_enabled = config.get('risk_management', {}).get('ratchet_1_5r', True)
    if ratchet_enabled and stop_loss < entry_price:
        halfway_target = entry_price + (unit * 0.5)  # halfway to 3R = +1.5R
        if current_price >= halfway_target:
            position['stop_loss'] = entry_price
            stop_loss = entry_price
            print(f"[{ticker}] RATCHET ACTIVATED: Price reached +1.5R (${current_price:.2f} >= ${halfway_target:.2f}). Stop moved to Breakeven (${entry_price:.2f}).")
            try:
                await db.update_stop_loss(ticker, entry_price)
            except Exception as e:
                print(f"[{ticker}] Warning: Failed to persist ratcheted stop to DB: {e}")

    exit_reason = None

    if current_price >= target:
        exit_reason = "TARGET_3R"
    elif current_price <= stop_loss:
        exit_reason = "BREAKEVEN" if abs(stop_loss - entry_price) < 0.02 else "STOP_LOSS"
    else:
        # Time / Chop Stop (disabled by default under NO_CHOP_STOP policy)
        enable_chop_stop = config.get('risk_management', {}).get('enable_chop_stop', False)
        if enable_chop_stop:
            elapsed_minutes = (current_dt - entry_time).total_seconds() / 60.0
            time_stop_limit = config['risk_management'].get('time_stop_minutes', 15)
            progress_factor = config['risk_management'].get('chop_progress_threshold', 0.3)

            if time_stop_limit and time_stop_limit > 0 and elapsed_minutes >= time_stop_limit:
                progress_threshold = entry_price + ((target - entry_price) * progress_factor)
                if current_price < progress_threshold:
                    exit_reason = "CHOP_TIME_STOP"

    if not exit_reason and current_dt.time() >= time(15, 58):
        exit_reason = "EOD_EXIT"

    if not exit_reason:
        return

    del active_positions[ticker]
    print(f"[{ticker}] Exit Triggered: {exit_reason} at ${current_price:.2f}")

    cost_basis = round(shares * entry_price, 2)
    proceeds = round(shares * current_price, 2)
    realized_pnl = round(proceeds - cost_basis, 2)

    try:
        # EXECUTE SELL VIA ROBINHOOD
        await route_rh_market_order(ticker, shares, action="SELL")
        await db.close_position(
            ticker=ticker,
            exit_price=current_price,
            exit_time=current_dt.isoformat(),
            exit_reason=exit_reason,
            realized_pnl=realized_pnl,
            cost_basis=cost_basis,
            proceeds=proceeds
        )
        print(f"[{ticker}] Closed {shares} shares | PnL: ${realized_pnl:+.2f} ({exit_reason})")
    except Exception as e:
        print(f"[{ticker}] CRITICAL: Failed to route SELL order via Robinhood: {e}")
        active_positions[ticker] = position