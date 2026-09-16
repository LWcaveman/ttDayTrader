import robin_stocks.robinhood as rh
import os
from dotenv import load_dotenv

load_dotenv()
# Uses your cached token from this morning seamlessly
rh.login(username=os.environ.get('RH_USERNAME'), password=os.environ.get('RH_PASSWORD'), store_session=True)

orders = rh.orders.get_all_stock_orders()

print("\n--- EXACT ROBINHOOD FILLS ---")
print(f"{'ACTION':<6} | {'TICKER':<6} | {'SHARES':<8} | {'FILL PRICE':<12} | {'STATE'}")
print("-" * 55)

for order in orders[:2]: # Grab the most recent BUY and SELL
    ticker = rh.stocks.get_symbol_by_url(order['instrument'])
    side = order['side'].upper()
    shares = order['cumulative_quantity']
    fill_price = order['average_price']
    state = order['state'].upper()
    
    print(f"{side:<6} | {ticker:<6} | {shares:<8} | ${fill_price:<11} | {state}")
