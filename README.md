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

## Trading Strategy & Logic

### 1. Pre-Market Universe Screener
- **Universe Modes**: Curated list (`ARM`, `HOOD`, `PLTR`, `AMZN`, `AAPL`, `GOOGL`) or full S&P 500 constituents.
- **Daily SMA Pullback**: Requires candidate tickers to have closed the prior session within a configurable tolerance (default `3%`) of their 5-day or 10-day daily Simple Moving Average.

### 2. Intraday Setup (The "Fashionably Late" Setup)
- **Trading Window**: Operates between **10:00 AM and 1:30 PM EST** to bypass the opening 30-minute volatility.
- **Trigger**: 9 EMA crosses above VWAP on 1-minute aggregated bars where current price > Low of Day (LOD).
- **Midday Tight Filter**: For entries after **10:45 AM EST**, the entry unit distance `(Entry - LOD) / Entry` must not exceed `0.75%` to prevent entering overextended moves.

### 3. Risk Management & Position Sizing
- **Risk Unit**: `Unit = Entry Price - LOD`.
- **Target (3R)**: `Entry + Unit` (equivalent to 3x the stop distance).
- **Stop Loss**: `Entry - (Unit / 3.0)` (1/3 of the Unit distance, creating an asymmetrical 3:1 reward-to-risk ratio).
- **Sizing**: Sized using `min(buying_power * risk_pct, max_risk)` (e.g. 2% account equity, capped at $10 max risk per trade).
- **Daily Trade Limit**: Enforces a strict maximum number of trades per day (default `1 trade/day`).

### 4. Exit Rules
1. **Target**: Price reaches or exceeds the 3R Target (`TARGET_3R`).
2. **Stop Loss**: Price drops to or below the stop loss (`STOP_LOSS`).
3. **Chop Time-Stop**: If 15 minutes elapse and price has not achieved at least 30% progress toward the target, the position is closed to prevent capital lockup (`CHOP_TIME_STOP`).
4. **End of Day**: Any remaining open position is liquidated automatically at 3:58 PM EST (`EOD_EXIT`).

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