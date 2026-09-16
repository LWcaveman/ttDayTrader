import pandas as pd
import yfinance as yf
from datetime import datetime
import pytz

class IntradayTracker:
    def __init__(self, ticker):
        self.ticker = ticker
        
        # Core State
        self.vwap = 0.0
        self.ema_9 = 0.0
        self.prev_ema_9 = 0.0
        self.prev_vwap = 0.0
        self.lod = float('inf')
        
        # Cumulative VWAP state
        self.cum_vol = 0.0
        self.cum_vol_x_tp = 0.0
        
        # Current Aggregating 1-Minute Candle
        self.current_minute = None
        self.current_high = -float('inf')
        self.current_low = float('inf')
        self.current_close = 0.0
        self.current_vol = 0.0
        
        # Daily SMA constraint variables
        self.sma_5 = 0.0
        self.sma_10 = 0.0
        
        self.is_ready = False
        
    def bootstrap_today(self):
        """
        Fetches today's historical 1m data up to this exact minute to align 
        the LOD, VWAP, and EMA_9 baseline perfectly with the backtester.
        """
        try:
            # 1. Get Daily SMAs to enforce pulling back rule at time of execution
            daily_df = yf.download(self.ticker, period="20d", interval="1d", progress=False)
            if not daily_df.empty:
                if isinstance(daily_df.columns, pd.MultiIndex):
                     daily_df.columns = daily_df.columns.get_level_values(0)
                # Shift by 1 to compare against yesterday's close, exactly like backtester
                daily_df['SMA_5'] = daily_df['Close'].rolling(5).mean().shift(1)
                daily_df['SMA_10'] = daily_df['Close'].rolling(10).mean().shift(1)
                self.sma_5 = daily_df['SMA_5'].iloc[-1]
                self.sma_10 = daily_df['SMA_10'].iloc[-1]

            # 2. Bootstrap Intraday Indicators
            df = yf.download(self.ticker, period="5d", interval="1m", progress=False)
            if df.empty:
                return False
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            df = df.tz_convert('America/New_York')
            df['EMA_9'] = df['Close'].ewm(span=9, adjust=False).mean()
            
            # Isolate Today
            today = datetime.now(pytz.timezone('America/New_York')).date()
            today_df = df[df.index.date == today].copy()
            
            if today_df.empty:
                # Pre-market or exact open. Fallback to yesterday's values
                self.ema_9 = df['EMA_9'].iloc[-1]
                self.prev_ema_9 = df['EMA_9'].iloc[-2] if len(df) > 1 else self.ema_9
                self.is_ready = True
                return True
                
            today_df['Typical_Price'] = (today_df['High'] + today_df['Low'] + today_df['Close']) / 3.0
            today_df['Vol_x_TP'] = today_df['Typical_Price'] * today_df['Volume']
            
            self.cum_vol = today_df['Volume'].sum()
            self.cum_vol_x_tp = today_df['Vol_x_TP'].sum()
            
            self.vwap = self.cum_vol_x_tp / self.cum_vol if self.cum_vol > 0 else 0.0
            self.ema_9 = today_df['EMA_9'].iloc[-1]
            self.lod = today_df['Low'].min()
            
            if len(today_df) > 1:
                self.prev_ema_9 = today_df['EMA_9'].iloc[-2]
                prev_cum_vol = today_df['Volume'].iloc[:-1].sum()
                prev_cum_vol_x_tp = today_df['Vol_x_TP'].iloc[:-1].sum()
                self.prev_vwap = prev_cum_vol_x_tp / prev_cum_vol if prev_cum_vol > 0 else 0.0
            else:
                self.prev_ema_9 = self.ema_9
                self.prev_vwap = self.vwap

            self.is_ready = True
            return True
            
        except Exception as e:
            print(f"Error bootstrapping {self.ticker}: {e}")
            return False

    def update_tick(self, price, volume, dt_ny):
        """
        Aggregates live stream ticks into 1-minute bars, calculating VWAP 
        and EMA at the close of each minute.
        """
        if not self.is_ready:
            return
            
        tick_minute = dt_ny.replace(second=0, microsecond=0)
        
        if self.current_minute is None:
            self.current_minute = tick_minute
            self.current_high = price
            self.current_low = price
            self.current_close = price
            self.current_vol = volume
            return
            
        # Minute Rollover - Finalize previous candle
        if tick_minute > self.current_minute:
            self._finalize_candle()
            # Start new candle
            self.current_minute = tick_minute
            self.current_high = price
            self.current_low = price
            self.current_vol = volume
            self.current_close = price
        else:
            # Update Developing Candle
            if price > self.current_high: self.current_high = price
            if price < self.current_low: self.current_low = price
            self.current_close = price
            self.current_vol += volume

        # Dynamic Low of Day tracker
        if price < self.lod:
            self.lod = price

    def _finalize_candle(self):
        """Calculates indicators on the completed 1m candle."""
        self.prev_ema_9 = self.ema_9
        self.prev_vwap = self.vwap
        
        # 1. VWAP
        tp = (self.current_high + self.current_low + self.current_close) / 3.0
        self.cum_vol += self.current_vol
        self.cum_vol_x_tp += (tp * self.current_vol)
        
        if self.cum_vol > 0:
            self.vwap = self.cum_vol_x_tp / self.cum_vol
            
        # 2. EMA 9
        k = 2.0 / (9.0 + 1.0)
        self.ema_9 = (self.current_close - self.prev_ema_9) * k + self.prev_ema_9

    def check_crossover(self, current_price):
        """
        Evaluates the crossing constraint and the daily pullback constraint natively.
        """
        if self.prev_ema_9 == 0.0 or self.prev_vwap == 0.0:
            return False
            
        cross_up = (self.ema_9 > self.vwap) and (self.prev_ema_9 <= self.prev_vwap)
        clean_slope = self.ema_9 > self.prev_ema_9
        
        if cross_up and clean_slope:
            near_5 = abs(current_price - self.sma_5) / self.sma_5 < 0.03 if self.sma_5 else False
            near_10 = abs(current_price - self.sma_10) / self.sma_10 < 0.03 if self.sma_10 else False
            return near_5 or near_10
            
        return False