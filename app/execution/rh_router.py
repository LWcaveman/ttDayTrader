import robin_stocks.robinhood as rh
import asyncio
import os

def _sync_login():
    """Authenticates with Robinhood using env variables or cached token."""
    # Updated to perfectly match your .env variables from rhTrader
    rh_user = os.environ.get('RH_USERNAME')
    rh_pass = os.environ.get('RH_PASSWORD')
    
    # If MFA is required, robin_stocks will automatically pause here 
    # and prompt you to type the SMS code directly into the terminal.
    if rh_user and rh_pass:
        rh.login(username=rh_user, password=rh_pass, store_session=True)
    else:
        # Fallback to cached pickle file in ~/.tokens/robinhood.pickle
        rh.login(store_session=True)
    print("[Robinhood] Execution Engine Authenticated.")

def _sync_get_buying_power():
    """Fetches real-time cash balance from Robinhood."""
    profile = rh.profiles.load_account_profile(info='buying_power')
    return float(profile) if profile else 0.0

def _sync_route_order(ticker, shares, action):
    """Executes a live fractional market order via Robinhood."""
    if action == "BUY":
        res = rh.orders.order_buy_fractional_by_quantity(ticker, shares)
    else:
        res = rh.orders.order_sell_fractional_by_quantity(ticker, shares)
    
    # Catch Robinhood's internal error dicts
    if type(res) is dict and ('error' in res or 'detail' in res):
        raise Exception(f"Robinhood Rejected: {res.get('error', res.get('detail'))}")
    return res

# --- Async Wrappers to protect the Tastytrade WebSocket thread ---

async def login_robinhood():
    await asyncio.to_thread(_sync_login)

async def get_rh_buying_power():
    return await asyncio.to_thread(_sync_get_buying_power)

async def route_rh_market_order(ticker, shares, action="BUY"):
    return await asyncio.to_thread(_sync_route_order, ticker, shares, action)