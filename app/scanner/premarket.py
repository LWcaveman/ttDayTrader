import pandas as pd
import yfinance as yf
from datetime import datetime
import pytz
import urllib.request
import io

# Elite Curated Day-Trading Universe (Optimized for 9 EMA / VWAP 3R Setup)
DEFAULT_CURATED_TICKERS = [
    "ARM", "HOOD", "PLTR", "AMZN", "AAPL", "GOOGL"
]

def get_curated_tickers(config=None):
    """
    Returns the curated elite day-trading universe from config.yaml,
    falling back to DEFAULT_CURATED_TICKERS.
    """
    if config and isinstance(config, dict):
        universe_cfg = config.get("universe", {})
        tickers = universe_cfg.get("curated_tickers")
        if tickers and isinstance(tickers, list) and len(tickers) > 0:
            return [t.upper().strip() for t in tickers]
    return DEFAULT_CURATED_TICKERS

def get_sp500_tickers():
    """
    Dynamically fetches the current S&P 500 index ticker list.
    Includes multiple fallbacks to ensure the daemon never crashes on boot.
    """
    print("Fetching the current S&P 500 universe...")
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
        df = pd.read_csv(url)
        return df['Symbol'].str.replace('.', '-', regex=False).tolist()
    except Exception as e:
        print(f"Primary source failed ({e}). Falling back to Wikipedia...")

    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
        )
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8')
        tables = pd.read_html(io.StringIO(html))
        return tables[0]['Symbol'].str.replace('.', '-', regex=False).tolist()
    except Exception as e:
        print(f"Wikipedia fallback failed ({e}).")

    return DEFAULT_CURATED_TICKERS

def run_screener(config=None):
    """
    Scans candidate tickers to verify they closed yesterday within the 
    specified tolerance (default: 3%) of their 5 SMA or 10 SMA.
    Defaults to the curated elite universe (ARM, HOOD, PLTR, AMZN, AAPL, GOOGL).
    """
    mode = "curated"
    tolerance = 0.03
    require_pullback = True

    if config and isinstance(config, dict):
        univ_cfg = config.get("universe", {})
        mode = univ_cfg.get("mode", "curated")
        tolerance = float(univ_cfg.get("sma_tolerance_pct", 0.03))
        require_pullback = bool(univ_cfg.get("require_daily_sma_pullback", True))

    if mode == "sp500":
        universe = get_sp500_tickers()
    else:
        universe = get_curated_tickers(config)

    print(f"Running pre-market screener for {len(universe)} curated tickers: {', '.join(universe)}")

    if not require_pullback:
        print(f"Daily SMA pre-filtering disabled. Monitoring all {len(universe)} tickers.")
        return universe

    valid_tickers = []
    try:
        data = yf.download(universe, period="20d", interval="1d", progress=False)
        if not data.empty and 'Close' in data:
            close_prices = data['Close']

            for ticker in universe:
                if isinstance(close_prices, pd.Series):
                    series = close_prices.dropna()
                elif ticker in close_prices.columns:
                    series = close_prices[ticker].dropna()
                else:
                    continue

                if len(series) < 10:
                    continue

                sma5 = series.rolling(window=5).mean().iloc[-1]
                sma10 = series.rolling(window=10).mean().iloc[-1]
                last_close = series.iloc[-1]

                dist_5 = abs(last_close - sma5) / sma5
                dist_10 = abs(last_close - sma10) / sma10
                near_5 = dist_5 < tolerance
                near_10 = dist_10 < tolerance

                if near_5 or near_10:
                    print(f"  [ELIGIBLE] {ticker:5s} | Close: ${last_close:6.2f} | Dist 5SMA: {dist_5*100:4.1f}% | Dist 10SMA: {dist_10*100:4.1f}%")
                    valid_tickers.append(ticker)
                else:
                    print(f"  [SKIPPED]  {ticker:5s} | Close: ${last_close:6.2f} | Dist 5SMA: {dist_5*100:4.1f}% | Dist 10SMA: {dist_10*100:4.1f}% (> {tolerance*100:.0f}%)")

    except Exception as e:
        print(f"Error during screening: {e}")

    if not valid_tickers:
        print(f"Notice: No tickers met the {tolerance*100:.0f}% pre-market SMA pullback filter.")
        print("Falling back to monitoring all curated tickers so intraday crosses can dynamically evaluate SMA pullback at execution.")
        return universe

    print(f"Screener finished: {len(valid_tickers)}/{len(universe)} tickers eligible for intraday execution today.")
    return valid_tickers

def is_within_trading_window(config):
    tz = pytz.timezone('America/New_York')
    now = datetime.now(tz).time()
    
    for window in config['execution']['trading_windows']:
        start = datetime.strptime(window['start'], "%H:%M").time()
        end = datetime.strptime(window['end'], "%H:%M").time()
        if start <= now <= end:
            return True
    return False