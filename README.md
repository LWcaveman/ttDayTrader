# ttDayTrader

An automated, intraday algorithmic trading daemon designed for executing the **9 EMA / VWAP 3R Pullback Setup**. 

The system utilizes **Tastytrade's DXLink WebSocket** feed for high-precision real-time streaming market data and executes fractional share orders through **Robinhood**. State and trade persistence are managed asynchronously using a local SQLite engine.

---

## Architecture Overview

```
                        ┌──────────────────────────────────────────────┐
                        │        Premarket Screener (yfinance)         │
                        │    (Filters for 5 SMA / 10 SMA Pullbacks)    │
                        └──────────────────────┬───────────────────────┘
                                               │ Active Tickers
                                               ▼
┌───────────────────────────┐      ┌───────────────────────────────────┐
│  Tastytrade DXLink Stream │ ───► │  Intraday Aggregator & Indicator  │
│   (Real-time Tick Data)   │      │ (1m Candles, VWAP, 9 EMA, LOD)    │
└───────────────────────────┘      └─────────────────┬─────────────────┘
                                                     │
                                                     ▼
                                   ┌───────────────────────────────────┐
                                   │       Risk & Execution Engine     │
                                   │   (Entry Crossover, 3R R:R Sizing)│
                                   └───────┬───────────────────┬───────┘
                                           │                   │
                                           ▼                   ▼
                     ┌──────────────────────────┐  ┌─────────────────────────┐
                     │ Robinhood Router         │  │ SQLite State Manager    │
                     │ (Fractional Execution)   │  │ (Positions, Balance)    │
                     └──────────────────────────┘  └─────────────────────────┘
```

---

## Trading Strategy & Regime-Routed Architecture

The daemon dynamically routes to the highest-performing quantitative strategy based on the macro market regime (QQQ 50-day EMA) and time of day:

### 1. Multi-Engine Strategy Architecture
- **Engine 1: Late Entry Morning Momentum (Bull Regime: QQQ > 50 EMA)**:
  - **Universe**: `TSLL`, `NVDL`, `CONL`, `TQQQ`, `PLTR`, `RBLX`, `AAPL`, `AMZN`
  - **Execution Window**: 10:00 AM to 10:45 AM EST
  - **Signal Trigger**: 9 EMA crosses above VWAP on 1-minute bars with upward sloping EMA 9, daily 5/10 SMA pullback within 3%, upper 40% bullish candle close (`min_close_pct: 0.60`), and volume >= 0.8x 10-bar SMA.
- **Engine 3: Morning VWAP Reclaim (Bear Regime: QQQ <= 50 EMA)**:
  - **Universe**: `CONL`, `SOFI`, `MARA`, `PLTR` (High-Beta Liquidity Sweepers)
  - **Execution Window**: 09:40 AM to 10:15 AM EST
  - **Signal Trigger**: Intraday liquidity flush below VWAP (at least 2 consecutive 1m bars, dip depth between 0.3% and 2.5%), followed by a strong reclaim candle closing back above VWAP with upper 40% candle close (`min_close_pct: 0.60`) and volume >= 0.8x 10-bar SMA.
- **Engine 2: Midday Mean-Reversion (Leveraged 2x/3x ETF Washouts)**:
  - **Universe**: `CONL`, `NVDL`, `TQQQ`, `SOXL`, `UPRO`, `BITX`
  - **Execution Window**: 11:30 AM to 1:30 PM EST
  - **Signal Trigger**: Panic washouts piercing -2.5 SD below VWAP (`VWAP - 2.5 * VWAP_SD`) during rangebound midday conditions (5m ADX <= 25.0) with seller exhaustion (`Volume < 10-bar SMA`) and a bullish reversal candle (Hammer wick >= 40% or Bullish Engulfing).
  - **Exit Architecture**: Hybrid 9 EMA + Central VWAP (50% partial exit at 9 EMA $\rightarrow$ stop ratchets to Breakeven $\rightarrow$ runner exits at Central VWAP).

### 2. Intraday Index Gate Alignment
- Long setups for morning strategies require the intraday benchmark **SPY to be trading at or above its intraday VWAP** (`SPY >= VWAP`), confirming positive market tide.
- Midday Mean-Reversion operates independently on individual extreme ETF washouts when ADX confirms non-trending conditions.

### 3. Risk Management & Sizing Geometry
- **Stop Loss**:
  - Late Entry Momentum: `Entry - (Unit / 3.0)` where `Unit = Entry - LOD`.
  - VWAP Reclaim: `Entry - Clamped_Stop_Dist` (clamped between 0.6% and 2.5% of entry price based on sweep low).
  - Midday Reversion: `Entry - Clamped_Stop_Dist` (clamped between 0.5% and 2.5% based on 3-bar flush low).
- **Position Sizing**: Sized using `min(buying_power * risk_pct, max_risk)` / stop distance.
- **Daily Trade Limit**: `1 trade per day` (Option B) guarantees 100% SEC / Robinhood T+1 cash settlement compliance with ZERO Good Faith Violations (GFVs). On accounts with margin, allows multiple non-overlapping trades.

### 4. Exit Rules & Route 1 Partial Scaling
1. **Engine 1 & 3 Exits**:
   - **Partial Scale (+1.5R)**: Automatically sells 33% of the position via Robinhood and ratchets the stop loss to Breakeven (`PARTIAL_SCALE`).
   - **Runner Target (+4.0R)**: The remaining 67% position runs toward +4.0R (`PARTIAL_AND_RUNNER_4.0R`).
   - **Breakeven Exit**: Exits at breakeven if price pulls back to entry after partial scale (`PARTIAL_1.5R_AND_BE`).
2. **Engine 2 Midday Exits**:
   - **Partial Scale (9 EMA)**: Automatically sells 50% of the position at the 9 EMA and ratchets the stop loss to Breakeven.
   - **Runner Target (Central VWAP)**: The remaining 50% position runs to Central VWAP (`TARGET_HYBRID_VWAP`).
   - **Breakeven Exit**: Exits at breakeven if price pulls back after 9 EMA partial (`PARTIAL_AND_BE`).
3. **Initial Stop Loss**: If trade does not reach partial profit and hits initial stop, exits at -1.0R (`STOP_LOSS`).
4. **End of Day**: Any remaining open position liquidates automatically at 3:58 PM EST (`EOD_EXIT`).

---

## Repository Structure

```
ttDayTrader/
├── app/
│   ├── auth/
│   │   └── session.py            # Tastytrade OAuth session authentication
│   ├── data/
│   │   ├── indicators.py         # 1-minute candle aggregator, VWAP & 9 EMA tracker
│   │   └── stream.py             # DXLink streaming WebSocket listener
│   ├── execution/
│   │   ├── fractional_router.py  # Tastytrade sandbox execution router
│   │   ├── rh_router.py          # Live Robinhood fractional execution router
│   │   └── risk.py               # Position sizing, trade setups, and exit rules
│   ├── scanner/
│   │   └── premarket.py          # Daily SMA screener & universe loader
│   ├── state/
│   │   └── db.py                 # Async SQLite portfolio and position manager
│   └── main.py                   # Main daemon entry point
├── config.yaml                   # Risk and universe configuration
├── requirements.txt              # Python package dependencies
├── check_slippage.py             # Utility to inspect recent Robinhood fill prices
├── fill_check.sh                 # Quick script to run fill checks
├── start_prod.sh                 # Starts background daemon with sleep inhibition
├── status_prod.sh                # Checks daemon status and displays recent logs
├── stop_prod.sh                  # Gracefully shuts down the running daemon
└── ttdaytrader_state.db          # Local SQLite runtime database (gitignored)
```

---

## Setup & Installation

### 1. Prerequisites
- Linux OS (Ubuntu / Debian recommended)
- Python 3.11+
- `uv` (recommended) or standard `python3.11-venv`
- `systemd-inhibit` (installed by default on systemd-based Linux)

### 2. Create Virtual Environment & Install Dependencies

```bash
# Using uv (fastest):
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt

# Or using standard python:
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure Environment Variables (`.env`)

Create a `.env` file in the project root:

```env
# Production Credentials (Used for Tastytrade DXLink real-time market data)
TT_PROD_CLIENT_ID=your_tastytrade_client_id
TT_PROD_CLIENT_SECRET=your_tastytrade_client_secret
TT_PROD_REFRESH_TOKEN=your_tastytrade_refresh_token

# Sandbox Credentials (Optional - used if testing Tastytrade order routing)
TT_CERT_CLIENT_ID=your_sandbox_client_id
TT_CERT_CLIENT_SECRET=your_sandbox_client_secret
TT_CERT_REFRESH_TOKEN=your_sandbox_refresh_token

# Robinhood Credentials (Used for live trade execution)
RH_USERNAME=your_robinhood_email
RH_PASSWORD=your_robinhood_password
```

> **Note**: Robinhood Multi-Factor Authentication (MFA) session tokens are cached automatically in `~/.tokens/robinhood.pickle` after first successful login.

---

## Configuration (`config.yaml`)

```yaml
risk_management:
  risk_pct_per_trade: 0.02          # 2% of buying power per trade
  max_risk_dollars: 10.00           # Cap max risk per trade at $10.00
  max_trades_per_day: 1             # Limit total trades executed per day
  time_stop_minutes: 15             # Minutes before chop evaluation
  chop_progress_threshold: 0.3      # Minimum required progress towards target (30%)

execution:
  environment: "prod"
  trading_windows:
    - start: "10:00"                # Start monitoring (EST)
      end: "13:30"                  # Stop monitoring (EST)
  morning_cutoff: "10:45"           # Transition point to tighter afternoon filters
  midday_max_unit_pct: 0.0075       # 0.75% maximum (Entry - LOD) distance for midday

universe:
  mode: "curated"                   # "curated" or "sp500"
  curated_tickers:
    - "ARM"
    - "HOOD"
    - "PLTR"
    - "AMZN"
    - "AAPL"
    - "GOOGL"
  require_daily_sma_pullback: true  # Screen against 5 SMA / 10 SMA
  sma_tolerance_pct: 0.03           # 3% tolerance threshold
```

---

## Running the Daemon

### In Production (Background with Sleep Inhibition)

Use the provided management scripts:

```bash
# Start the daemon in the background (prevents system sleep/idle)
./start_prod.sh

# Check process status and view the latest log output
./status_prod.sh

# Stop the background daemon
./stop_prod.sh

# Stream live logs in real time
tail -f prod_trader.log
```

### Manual / Foreground Run

```bash
source .venv/bin/activate
systemd-inhibit --what=sleep:idle --why="ttDayTrader active" python -u -m app.main
```

### Automatic Start on Boot & Schedule (Cron)

The system is configured to auto-start and manage itself through [auto_start.sh](file:///home/levi/1.%20PROD/ttDayTrader/auto_start.sh) using `cron`:

1. **On Machine Startup (`@reboot`)**: Checks if today is a weekday and the time is between **8:00 AM and 3:00 PM CST**. If so, it waits for WiFi/network to establish and automatically starts the daemon with sleep inhibition.
2. **Daily at 8:00 AM CST**: Triggers the daemon before market opens.
3. **Midday Watchdog (Every 10 mins)**: Verifies the daemon is running during active market hours; restarts it automatically if it was stopped unexpectedly.
4. **Market Close (3:15 PM CST)**: Gracefully shuts down the daemon for the evening.

```bash
# View current cron schedules
crontab -l

# View autostart logs
cat autostart.log
```

---

## Verification & Utilities

- **Check Order Fills & Slippage**:
  Inspect the exact execution price and state of recent Robinhood orders:
  ```bash
  source .venv/bin/activate
  python check_slippage.py
  ```

- **Inspect SQLite State**:
  ```bash
  sqlite3 -column -header ttdaytrader_state.db "SELECT * FROM active_positions;"
  sqlite3 -column -header ttdaytrader_state.db "SELECT * FROM portfolio;"
  ```