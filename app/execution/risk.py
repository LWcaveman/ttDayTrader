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
        shares = float(trade['shares'])
        orig_shares = float(trade['orig_shares']) if trade.get('orig_shares') is not None else shares
        unit = float(trade['unit']) if trade.get('unit') is not None else float(trade['target'] - trade['entry_price'])
        active_positions[ticker] = {
            'shares': shares,
            'orig_shares': orig_shares,
            'entry_price': float(trade['entry_price']),
            'stop_loss': float(trade['stop_loss']),
            'target': float(trade['target']),
            'unit': unit,
            'entry_time': entry_dt,
            'scaled': bool(trade.get('scaled', False)),
            'scaled_shares': float(trade.get('scaled_shares', 0.0) or 0.0),
            'scaled_price': float(trade.get('scaled_price', 0.0) or 0.0),
            'scaled_pnl': float(trade.get('scaled_pnl', 0.0) or 0.0),
            'strategy': trade.get('strategy', 'MORNING_MOMENTUM')
        }
    if open_trades:
        print(f"[RECOVERY] Hydrated {len(open_trades)} active positions from SQLite.")

async def evaluate_setup(ticker, tracker, current_price, prod_session, config, db, strategy="MORNING_MOMENTUM", reclaim_info=None, midday_info=None):
    if len(active_positions) >= 1 or ticker in active_positions:
        return

    if ticker in rejected_cooldowns:
        return

    lod = tracker.lod if tracker else 0.0

    if strategy == "MIDDAY_REVERSION" and midday_info:
        entry_price = float(midday_info.get('entry_price', current_price))
        flush_low = float(midday_info.get('flush_low', lod))
        m_cfg = config.get('midday_reversion', {}) if config else {}
        min_stop_pct = float(m_cfg.get('min_stop_pct', 0.005))
        max_stop_pct = float(m_cfg.get('max_stop_pct', 0.025))
        raw_stop_dist = entry_price - flush_low
        clamped_stop_dist = max(
            entry_price * min_stop_pct,
            min(raw_stop_dist, entry_price * max_stop_pct)
        )
        stop_loss = entry_price - clamped_stop_dist
        unit = clamped_stop_dist * 3.0
        target = float(midday_info.get('vwap', tracker.vwap if tracker and tracker.vwap > 0 else current_price + unit))
    elif strategy == "VWAP_RECLAIM" and reclaim_info:
        entry_price = float(reclaim_info.get('entry_price', current_price))
        sweep_low = float(reclaim_info.get('sweep_low', lod))
        min_stop_pct = float(config.get('vwap_reclaim', {}).get('min_stop_pct', 0.006))
        max_stop_pct = float(config.get('vwap_reclaim', {}).get('max_stop_pct', 0.025))
        raw_stop_dist = entry_price - sweep_low
        clamped_stop_dist = max(
            entry_price * min_stop_pct,
            min(raw_stop_dist, entry_price * max_stop_pct)
        )
        stop_loss = entry_price - clamped_stop_dist
        unit = clamped_stop_dist * 3.0
    else:
        if current_price <= lod:
            return

        unit = current_price - lod
        unit_pct = (unit / current_price) if current_price > 0 else 1.0

        # Morning vs Midday Execution Filter (Morning Momentum only)
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

        stop_loss = current_price - (unit / 3.0)

    # FETCH LIVE BALANCE FROM ROBINHOOD
    try:
        raw_buying_power = await get_rh_buying_power()
    except Exception as e:
        print(f"[{ticker}] CRITICAL: Failed to query Robinhood buying power: {e}. Aborting setup.")
        return
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

    enable_partial = config.get('risk_management', {}).get('enable_partial_scale', True)
    runner_r = float(config.get('risk_management', {}).get('runner_r', 4.0))

    if strategy == "MIDDAY_REVERSION":
        target_label = "VWAP"
    elif enable_partial:
        target = current_price + (unit / 3.0) * runner_r
        target_label = f"{runner_r:.1f}R"
    else:
        target = current_price + unit
        target_label = "3.0R"

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

    strat_tag = f"[{strategy}] " if strategy else ""
    print(f"[{ticker}] {strat_tag}Setup Detected: Entry ${current_price:.2f} | Target ({target_label}) ${target:.2f} | Stop (-1.0R) ${stop_loss:.2f}")
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
            entry_time=now_dt.isoformat(),
            orig_shares=shares,
            unit=unit,
            strategy=strategy
        )

        active_positions[ticker] = {
            'shares': shares,
            'orig_shares': shares,
            'entry_price': current_price,
            'stop_loss': stop_loss,
            'target': target,
            'unit': unit,
            'entry_time': now_dt,
            'scaled': False,
            'scaled_shares': 0.0,
            'scaled_price': 0.0,
            'scaled_pnl': 0.0,
            'strategy': strategy
        }
        print(f"[{ticker}] Robinhood Buy Order Routed Successfully. Position active.")
    except Exception as e:
        print(f"[{ticker}] Robinhood Order Failed: {e}")
        rejected_cooldowns.add(ticker) 

async def check_and_execute_exit(ticker, current_price, current_dt, prod_session, config, db, tracker=None):
    position = active_positions.get(ticker)
    if not position:
        return

    entry_price = position['entry_price']
    stop_loss = position['stop_loss']
    target = position['target']
    entry_time = position['entry_time']
    shares = position['shares']
    unit = position.get('unit', target - entry_price)
    pos_strategy = position.get('strategy', 'MORNING_MOMENTUM')

    if pos_strategy == 'MIDDAY_REVERSION':
        m_cfg = config.get('midday_reversion', {}) if config else {}
        partial_pct = float(m_cfg.get('partial_pct', 0.50))
        min_partial_r = float(m_cfg.get('min_partial_r', 1.0))

        # Initial 1R risk distance
        r_unit = (unit / 3.0) if unit > 0 else (entry_price - stop_loss if entry_price > stop_loss else (target - entry_price) / 2.0)
        min_partial = entry_price + (r_unit * min_partial_r)

        # Slice 1: Scale at 9 EMA, but require at least min_partial (+1.0R) above entry
        # to prevent instant 50% scale-out when reversal candle closes at/above the declining 9 EMA
        if tracker and tracker.ema_9 > 0:
            ema_target = max(tracker.ema_9, min_partial)
        else:
            ema_target = min_partial

        vwap_target = tracker.vwap if (tracker and tracker.vwap > 0) else target

        # If price reaches Central VWAP directly, exit the entire position
        exit_reason = None
        if current_price >= vwap_target:
            exit_reason = "TARGET_HYBRID_VWAP" if position.get('scaled', False) else "TARGET_VWAP"
        elif not position.get('scaled', False) and current_price >= ema_target:
            orig_shares = position.get('orig_shares', shares)
            scale_shares = round(orig_shares * partial_pct, 4)
            remaining_shares = round(shares - scale_shares, 4)

            if scale_shares > 0 and remaining_shares > 0:
                print(f"[{ticker}] MIDDAY 9 EMA PARTIAL TRIGGERED: Price ${current_price:.2f} reached 9 EMA target (${ema_target:.2f}). Selling {scale_shares} shares ({partial_pct*100:.0f}%)...")
                try:
                    await route_rh_market_order(ticker, scale_shares, action="SELL")
                    scale_cost = round(scale_shares * entry_price, 2)
                    scale_proceeds = round(scale_shares * current_price, 2)
                    scale_pnl = round(scale_proceeds - scale_cost, 2)

                    position['shares'] = remaining_shares
                    position['stop_loss'] = entry_price
                    position['scaled'] = True
                    position['scaled_shares'] = scale_shares
                    position['scaled_price'] = current_price
                    position['scaled_pnl'] = scale_pnl
                    stop_loss = entry_price

                    print(f"[{ticker}] MIDDAY PARTIAL EXECUTED: Sold {scale_shares} shares at ${current_price:.2f} | Scaled PnL: ${scale_pnl:+.2f}. Holding {remaining_shares} runner shares to Central VWAP (${vwap_target:.2f}). Stop moved to Breakeven (${entry_price:.2f}).")
                    try:
                        await db.record_partial_exit(
                            ticker=ticker,
                            scaled_shares=scale_shares,
                            remaining_shares=remaining_shares,
                            scaled_price=current_price,
                            scaled_pnl=scale_pnl,
                            new_stop_loss=entry_price
                        )
                    except Exception as e:
                        print(f"[{ticker}] Warning: Failed to persist partial exit to DB: {e}")
                except Exception as e:
                    print(f"[{ticker}] CRITICAL: Partial scale sell order failed: {e}. Moving stop to breakeven defensively.")
                    position['stop_loss'] = entry_price
                    stop_loss = entry_price
                    try:
                        await db.update_stop_loss(ticker, entry_price)
                    except Exception as ex:
                        print(f"[{ticker}] Warning: Failed to update stop to DB: {ex}")
            else:
                position['stop_loss'] = entry_price
                stop_loss = entry_price
                position['scaled'] = True
                print(f"[{ticker}] MIDDAY RATCHET ACTIVATED (Position too small to split): Stop moved to Breakeven (${entry_price:.2f}).")
                try:
                    await db.update_stop_loss(ticker, entry_price)
                except Exception as e:
                    print(f"[{ticker}] Warning: Failed to persist ratcheted stop to DB: {e}")

        # Stop loss check (initial stop or ratcheted breakeven)
        if not exit_reason and current_price <= stop_loss:
            exit_reason = "PARTIAL_AND_BE" if position.get('scaled', False) else "STOP_LOSS"
    else:
        rm_cfg = config.get('risk_management', {})
        enable_partial = rm_cfg.get('enable_partial_scale', True)
        partial_r = float(rm_cfg.get('partial_scale_r', 1.5))
        partial_pct = float(rm_cfg.get('partial_scale_pct', 0.33))
        runner_r = float(rm_cfg.get('runner_r', 4.0))
        ratchet_enabled = rm_cfg.get('ratchet_1_5r', True)

        # 1. Check Partial Scaling or Breakeven Ratchet
        # 1R distance is unit / 3.0
        r_unit = unit / 3.0 if unit > 0 else (target - entry_price) / runner_r
        halfway_target = entry_price + (r_unit * partial_r)  # +1.5R target

        if enable_partial and not position.get('scaled', False):
            if current_price >= halfway_target:
                orig_shares = position.get('orig_shares', shares)
                scale_shares = round(orig_shares * partial_pct, 4)
                remaining_shares = round(shares - scale_shares, 4)

                # Defensive validation: ensure fractional sizes are valid
                if scale_shares > 0 and remaining_shares > 0:
                    print(f"[{ticker}] PARTIAL SCALE TRIGGERED: Price ${current_price:.2f} reached +{partial_r}R (${halfway_target:.2f}). Selling {scale_shares} shares ({partial_pct*100:.0f}%)...")
                    try:
                        await route_rh_market_order(ticker, scale_shares, action="SELL")
                        scale_cost = round(scale_shares * entry_price, 2)
                        scale_proceeds = round(scale_shares * current_price, 2)
                        scale_pnl = round(scale_proceeds - scale_cost, 2)

                        position['shares'] = remaining_shares
                        position['stop_loss'] = entry_price
                        position['scaled'] = True
                        position['scaled_shares'] = scale_shares
                        position['scaled_price'] = current_price
                        position['scaled_pnl'] = scale_pnl
                        stop_loss = entry_price

                        print(f"[{ticker}] PARTIAL SCALE EXECUTED: Sold {scale_shares} shares at ${current_price:.2f} | Scaled PnL: ${scale_pnl:+.2f}. Holding {remaining_shares} runner shares to {runner_r}R. Stop moved to Breakeven (${entry_price:.2f}).")
                        try:
                            await db.record_partial_exit(
                                ticker=ticker,
                                scaled_shares=scale_shares,
                                remaining_shares=remaining_shares,
                                scaled_price=current_price,
                                scaled_pnl=scale_pnl,
                                new_stop_loss=entry_price
                            )
                        except Exception as e:
                            print(f"[{ticker}] Warning: Failed to persist partial exit to DB: {e}")
                    except Exception as e:
                        print(f"[{ticker}] CRITICAL: Partial scale sell order failed: {e}. Moving stop to breakeven defensively.")
                        position['stop_loss'] = entry_price
                        stop_loss = entry_price
                        try:
                            await db.update_stop_loss(ticker, entry_price)
                        except Exception as ex:
                            print(f"[{ticker}] Warning: Failed to update stop to DB: {ex}")
                else:
                    # Share size too small to divide; ratchet stop to breakeven
                    position['stop_loss'] = entry_price
                    stop_loss = entry_price
                    position['scaled'] = True
                    print(f"[{ticker}] RATCHET ACTIVATED (Position too small to split): Stop moved to Breakeven (${entry_price:.2f}).")
                    try:
                        await db.update_stop_loss(ticker, entry_price)
                    except Exception as e:
                        print(f"[{ticker}] Warning: Failed to persist ratcheted stop to DB: {e}")

        elif ratchet_enabled and stop_loss < entry_price:
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
            if position.get('scaled', False):
                exit_reason = f"PARTIAL_AND_RUNNER_{runner_r:.1f}R"
            else:
                exit_reason = f"TARGET_{runner_r:.1f}R" if enable_partial else "TARGET_3R"
        elif current_price <= stop_loss:
            if position.get('scaled', False):
                exit_reason = f"PARTIAL_{partial_r:.1f}R_AND_BE"
            else:
                exit_reason = "BREAKEVEN" if abs(stop_loss - entry_price) < 0.02 else "STOP_LOSS"
        else:
            # Time / Chop Stop (disabled by default under NO_CHOP_STOP policy)
            enable_chop_stop = rm_cfg.get('enable_chop_stop', False)
            if enable_chop_stop:
                elapsed_minutes = (current_dt - entry_time).total_seconds() / 60.0
                time_stop_limit = rm_cfg.get('time_stop_minutes', 15)
                progress_factor = rm_cfg.get('chop_progress_threshold', 0.3)

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

    current_shares = position['shares']
    remaining_cost = round(current_shares * entry_price, 2)
    remaining_proceeds = round(current_shares * current_price, 2)
    remaining_pnl = round(remaining_proceeds - remaining_cost, 2)

    scaled_pnl = float(position.get('scaled_pnl', 0.0) or 0.0)
    total_realized_pnl = round(scaled_pnl + remaining_pnl, 2)

    orig_shares = float(position.get('orig_shares', current_shares) or current_shares)
    total_cost_basis = round(orig_shares * entry_price, 2)
    total_proceeds = round(total_cost_basis + total_realized_pnl, 2)

    try:
        # EXECUTE SELL VIA ROBINHOOD
        await route_rh_market_order(ticker, current_shares, action="SELL")
        await db.close_position(
            ticker=ticker,
            exit_price=current_price,
            exit_time=current_dt.isoformat(),
            exit_reason=exit_reason,
            realized_pnl=total_realized_pnl,
            cost_basis=total_cost_basis,
            proceeds=total_proceeds
        )
        print(f"[{ticker}] Closed remaining {current_shares} shares | Trade Realized PnL: ${total_realized_pnl:+.2f} ({exit_reason})")
    except Exception as e:
        print(f"[{ticker}] CRITICAL: Failed to route SELL order via Robinhood: {e}")
        active_positions[ticker] = position