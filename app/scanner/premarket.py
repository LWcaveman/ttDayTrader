import pandas as pd
import yfinance as yf
from datetime import datetime
import pytz
import urllib.request
import io

def get_sp500_tickers():
    """
    Dynamically fetches the current S&P 500 index ticker list.
    Includes multiple fallbacks to ensure the daemon never crashes on boot.
    """
    print("Fetching the current S&P 500 universe...")
    
    # 1. Primary Source: Reliable CSV Repo (Fastest, zero HTML parsing)
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
        df = pd.read_csv(url)
        tickers = df['Symbol'].str.replace('.', '-', regex=False).tolist()
        return tickers
    except Exception as e:
        print(f"Primary source failed ({e}). Falling back to Wikipedia...")

    # 2. Secondary Source: Wikipedia (Wrapped in StringIO to prevent HTML dumps)
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
        )
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8')
            
        # io.StringIO prevents Pandas from printing the raw HTML if a ValueError occurs
        tables = pd.read_html(io.StringIO(html))
        df = tables[0]
        tickers = df['Symbol'].str.replace('.', '-', regex=False).tolist()
        return tickers
    except Exception as e:
        print(f"Wikipedia fallback failed ({e}).")
        
    # 3. Emergency Source: Top High-Liquidity Momentum Tickers
    print("WARNING: Using emergency hard-coded ticker list to keep daemon running.")
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", 
        "AVGO", "JPM", "LLY", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", 
        "COST", "MRK", "ABBV", "CVX", "CRM", "AMD", "NFLX", "PEP", "KO", 
        "BAC", "WMT", "TMO", "LIN", "MCD", "ACN", "CSCO", "ABT", "INTC", 
        "QCOM", "INTU", "VZ", "CMCSA", "IBM", "DHR", "TXN", "AMGN", "NOW"
    ]

def run_screener():
    """
    Scans the entire S&P 500 for stocks that closed yesterday
    within 3% of their 5 SMA or 10 SMA.
    """
    universe = get_sp500_tickers()
    print(f"Running pre-market daily SMA screener for {len(universe)} tickers...")
    valid_tickers = []
    
    try:
        # Batch download is exponentially faster than looping single tickers
        data = yf.download(universe, period="20d", interval="1d", progress=False)
        close_prices = data['Close']
        
        for ticker in universe:
            if ticker not in close_prices.columns:
                continue
                
            series = close_prices[ticker].dropna()
            if len(series) < 10:
                continue
                
            sma5 = series.rolling(window=5).mean().iloc[-1]
            sma10 = series.rolling(window=10).mean().iloc[-1]
            last_close = series.iloc[-1]
            
            near_5 = abs(last_close - sma5) / sma5 < 0.03
            near_10 = abs(last_close - sma10) / sma10 < 0.03
            
            if near_5 or near_10:
                valid_tickers.append(ticker)
                
    except Exception as e:
        print(f"Error during batch screening: {e}")
            
    print(f"Screener finished. {len(valid_tickers)} tickers eligible for intraday scanning today.")
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