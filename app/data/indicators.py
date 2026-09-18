import numpy as np
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
        self.cum_vol_x_tp2 = 0.0
        self.vwap_sd = 0.0
        self.prev_vwap_sd = 0.0
        self.vwap_lower_2_5sd = 0.0
        self.prev_vwap_lower_2_5sd = 0.0
        
        # Current Aggregating 1-Minute Candle
        self.current_minute = None
        self.current_open = 0.0
        self.current_high = -float('inf')
        self.current_low = float('inf')
        self.current_close = 0.0
        self.current_vol = 0.0

        # Last Completed 1-Minute Candle (for edge confirmation filters)
        self.last_candle_open = 0.0
        self.last_candle_high = 0.0
        self.last_candle_low = 0.0
        self.last_candle_close = 0.0
        self.last_candle_vol = 0.0
        self.recent_volumes = []
        self.recent_lows = []
        
        # Daily SMA constraint variables
        self.sma_5 = 0.0
        self.sma_10 = 0.0
        
        # Morning VWAP Reclaim State Tracking
        self.bars_below_vwap = 0
        self.sweep_low = float('inf')
        self.reclaim_signal = None

        # Midday Mean-Reversion State Tracking
        self.adx_5m = 20.0
        self.midday_signal = None
        
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
            today_df['Vol_x_TP2'] = (today_df['Typical_Price'] ** 2) * today_df['Volume']
            
            self.cum_vol = today_df['Volume'].sum()
            self.cum_vol_x_tp = today_df['Vol_x_TP'].sum()
            self.cum_vol_x_tp2 = today_df['Vol_x_TP2'].sum()
            
            self.vwap = self.cum_vol_x_tp / self.cum_vol if self.cum_vol > 0 else 0.0
            if self.cum_vol > 0:
                var = (self.cum_vol_x_tp2 / self.cum_vol) - (self.vwap ** 2)
                self.vwap_sd = (max(0.0, var)) ** 0.5
            else:
                self.vwap_sd = 0.0
            self.vwap_lower_2_5sd = self.vwap - (2.5 * self.vwap_sd)
            
            self.ema_9 = today_df['EMA_9'].iloc[-1]
            self.lod = today_df['Low'].min()
            
            if len(today_df) > 1:
                self.prev_ema_9 = today_df['EMA_9'].iloc[-2]
                prev_cum_vol = today_df['Volume'].iloc[:-1].sum()
                prev_cum_vol_x_tp = today_df['Vol_x_TP'].iloc[:-1].sum()
                prev_cum_vol_x_tp2 = today_df['Vol_x_TP2'].iloc[:-1].sum()
                self.prev_vwap = prev_cum_vol_x_tp / prev_cum_vol if prev_cum_vol > 0 else 0.0
                if prev_cum_vol > 0:
                    prev_var = (prev_cum_vol_x_tp2 / prev_cum_vol) - (self.prev_vwap ** 2)
                    self.prev_vwap_sd = (max(0.0, prev_var)) ** 0.5
                else:
                    self.prev_vwap_sd = 0.0
                self.prev_vwap_lower_2_5sd = self.prev_vwap - (2.5 * self.prev_vwap_sd)
            else:
                self.prev_ema_9 = self.ema_9
                self.prev_vwap = self.vwap
                self.prev_vwap_sd = self.vwap_sd
                self.prev_vwap_lower_2_5sd = self.vwap_lower_2_5sd

            # Populate last completed candle and rolling 10-bar volumes for edge filters
            self.recent_volumes = [float(v) for v in today_df['Volume'].iloc[-10:].tolist()]
            self.recent_lows = [float(l) for l in today_df['Low'].iloc[-5:].tolist()]
            last_row = today_df.iloc[-1]
            self.last_candle_open = float(last_row['Open'])
            self.last_candle_high = float(last_row['High'])
            self.last_candle_low = float(last_row['Low'])
            self.last_candle_close = float(last_row['Close'])
            self.last_candle_vol = float(last_row['Volume'])

            # Bootstrap 5m ADX
            try:
                df_5m = df[['High', 'Low', 'Close']].resample('5min').agg({
                    'High': 'max', 'Low': 'min', 'Close': 'last'
                }).dropna()
                if len(df_5m) >= 20:
                    h5 = df_5m['High']
                    l5 = df_5m['Low']
                    c5 = df_5m['Close']
                    c5_prev = c5.shift(1)
                    tr5 = pd.concat([h5 - l5, (h5 - c5_prev).abs(), (l5 - c5_prev).abs()], axis=1).max(axis=1)
                    up5 = h5 - h5.shift(1)
                    down5 = l5.shift(1) - l5
                    plus_dm = np.where((up5 > down5) & (up5 > 0), up5, 0.0)
                    minus_dm = np.where((down5 > up5) & (down5 > 0), down5, 0.0)
                    alpha = 1.0 / 14.0
                    atr5 = tr5.ewm(alpha=alpha, adjust=False).mean()
                    plus_di = 100.0 * (pd.Series(plus_dm, index=df_5m.index).ewm(alpha=alpha, adjust=False).mean() / atr5)
                    minus_di = 100.0 * (pd.Series(minus_dm, index=df_5m.index).ewm(alpha=alpha, adjust=False).mean() / atr5)
                    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
                    adx_series = dx.ewm(alpha=alpha, adjust=False).mean().fillna(20.0)
                    self.adx_5m = float(adx_series.iloc[-1])
                else:
                    self.adx_5m = 20.0
            except Exception:
                self.adx_5m = 20.0

            # Bootstrap VWAP Sweep state for Morning VWAP Reclaim
            today_df['Cum_Vol'] = today_df['Volume'].cumsum()
            today_df['Cum_Vol_x_TP'] = today_df['Vol_x_TP'].cumsum()
            today_df['Rolling_VWAP'] = today_df['Cum_Vol_x_TP'] / today_df['Cum_Vol']
            
            bars_below = 0
            s_low = float('inf')
            for _, r in today_df.iterrows():
                rc = float(r['Close'])
                rvwap = float(r['Rolling_VWAP'])
                rl = float(r['Low'])
                if rc < rvwap:
                    bars_below += 1
                    s_low = min(s_low, rl)
                else:
                    bars_below = 0
                    s_low = float('inf')
            self.bars_below_vwap = bars_below
            self.sweep_low = s_low

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
            self.current_open = price
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
            self.current_open = price
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
        prev_completed_close = self.last_candle_close
        prev_completed_vwap = self.vwap
        prev_completed_lower_2_5sd = self.vwap_lower_2_5sd

        self.last_candle_open = getattr(self, 'current_open', self.current_close)
        self.last_candle_high = self.current_high
        self.last_candle_low = self.current_low
        self.last_candle_close = self.current_close
        self.last_candle_vol = self.current_vol

        self.recent_volumes.append(float(self.current_vol))
        if len(self.recent_volumes) > 10:
            self.recent_volumes.pop(0)

        self.recent_lows.append(float(self.current_low))
        if len(self.recent_lows) > 5:
            self.recent_lows.pop(0)

        self.prev_ema_9 = self.ema_9
        self.prev_vwap = self.vwap
        self.prev_vwap_sd = self.vwap_sd
        self.prev_vwap_lower_2_5sd = self.vwap_lower_2_5sd
        
        # 1. VWAP & SD
        tp = (self.current_high + self.current_low + self.current_close) / 3.0
        self.cum_vol += self.current_vol
        self.cum_vol_x_tp += (tp * self.current_vol)
        self.cum_vol_x_tp2 += ((tp ** 2) * self.current_vol)
        
        if self.cum_vol > 0:
            self.vwap = self.cum_vol_x_tp / self.cum_vol
            var = (self.cum_vol_x_tp2 / self.cum_vol) - (self.vwap ** 2)
            self.vwap_sd = (max(0.0, var)) ** 0.5
            self.vwap_lower_2_5sd = self.vwap - (2.5 * self.vwap_sd)
            
        # 2. EMA 9
        k = 2.0 / (9.0 + 1.0)
        self.ema_9 = (self.current_close - self.prev_ema_9) * k + self.prev_ema_9

        # 3. Morning VWAP Reclaim State Machine
        c = self.current_close
        h = self.current_high
        l = self.current_low
        v = self.current_vol

        if c < self.vwap:
            self.bars_below_vwap += 1
            if l < self.sweep_low:
                self.sweep_low = l
            self.reclaim_signal = None
        else:
            # Check VWAP Reclaim setup
            if self.bars_below_vwap >= 2 and self.sweep_low < self.vwap and prev_completed_close <= prev_completed_vwap:
                sweep_depth = (self.vwap - self.sweep_low) / self.vwap if self.vwap > 0 else 0.0
                valid_depth = (0.003 <= sweep_depth <= 0.025)

                c_range = h - l
                close_pct = (c - l) / c_range if c_range > 0 else 0.5
                valid_candle = (close_pct >= 0.60)

                vol_sma = sum(self.recent_volumes) / len(self.recent_volumes) if self.recent_volumes else 0.0
                valid_vol = (vol_sma > 0) and (v >= 0.80 * vol_sma)

                if valid_depth and valid_candle and valid_vol:
                    self.reclaim_signal = {
                        'entry_price': c,
                        'sweep_low': self.sweep_low,
                        'bars_below_vwap': self.bars_below_vwap,
                        'sweep_depth': sweep_depth,
                        'minute': self.current_minute
                    }
                else:
                    self.reclaim_signal = None
            else:
                self.reclaim_signal = None

            # Reset sweep tracker once closed above VWAP
            self.bars_below_vwap = 0
            self.sweep_low = float('inf')

        # 4. Midday Mean-Reversion State Machine
        o = self.last_candle_open
        lower_band = self.vwap_lower_2_5sd
        prev_lower_band = self.prev_vwap_lower_2_5sd

        # Setup Condition: Price pierced below lower band on current or previous candle
        pierced = (l <= lower_band) or (self.last_candle_low <= prev_lower_band)

        # Reversal Candle Logic
        rng = h - l
        is_bullish = c > o
        is_hammer = (rng > 0) and is_bullish and (((min(o, c) - l) / rng) >= 0.40)
        is_engulf = is_bullish and (c > prev_completed_close)
        reversal = is_hammer or is_engulf

        # Volume Exhaustion Check: Selling volume dries up (v < 10-bar SMA)
        vol_sma = sum(self.recent_volumes) / len(self.recent_volumes) if self.recent_volumes else 0.0
        vol_exhausted = (vol_sma > 0) and (v < vol_sma)

        # ADX Filter: Ensure non-trending rangebound conditions
        rangebound = (self.adx_5m <= 25.0)

        if pierced and reversal and vol_exhausted and rangebound:
            flush_low = min(self.recent_lows[-3:]) if len(self.recent_lows) >= 3 else min(l, self.last_candle_low)
            self.midday_signal = {
                'entry_price': c,
                'flush_low': flush_low,
                'vwap': self.vwap,
                'vwap_sd': self.vwap_sd,
                'adx_5m': self.adx_5m,
                'minute': self.current_minute
            }
        else:
            self.midday_signal = None

    def check_vwap_reclaim(self, config=None):
        """
        Returns the pending VWAP Reclaim signal if triggered on the last candle close.
        Clears the signal upon reading so it only triggers once per setup.
        """
        if self.reclaim_signal is not None:
            sig = self.reclaim_signal
            self.reclaim_signal = None
            return sig
        return None

    def check_midday_reversion(self, config=None):
        """
        Returns the pending Midday Mean-Reversion signal if triggered on the last candle close.
        Clears the signal upon reading so it only triggers once per setup.
        """
        if self.midday_signal is not None:
            sig = self.midday_signal
            self.midday_signal = None
            return sig
        return None

    def check_crossover(self, current_price, min_close_pct=0.60, min_vol_ratio=0.80):
        """
        Evaluates the crossing constraint, the daily pullback constraint,
        plus the bullish candle close and volume participation edge filters.
        """
        if self.prev_ema_9 == 0.0 or self.prev_vwap == 0.0:
            return False
            
        cross_up = (self.ema_9 > self.vwap) and (self.prev_ema_9 <= self.prev_vwap)
        clean_slope = self.ema_9 > self.prev_ema_9
        
        if cross_up and clean_slope:
            # 1. Daily SMA Pullback Context
            near_5 = abs(current_price - self.sma_5) / self.sma_5 < 0.03 if self.sma_5 else False
            near_10 = abs(current_price - self.sma_10) / self.sma_10 < 0.03 if self.sma_10 else False
            if not (near_5 or near_10):
                return False

            # 2. Bullish Close Filter (Must close in upper 40% of candle, rejecting topping wicks)
            if self.last_candle_high > self.last_candle_low:
                c_range = self.last_candle_high - self.last_candle_low
                close_pct = (self.last_candle_close - self.last_candle_low) / c_range
                if close_pct < min_close_pct:
                    return False

            # 3. Volume Participation Filter (>= min_vol_ratio x 10-bar SMA)
            if len(self.recent_volumes) >= 5:
                vol_sma = sum(self.recent_volumes) / len(self.recent_volumes)
                if vol_sma > 0 and (self.last_candle_vol / vol_sma) < min_vol_ratio:
                    return False

            return True
            
        return False