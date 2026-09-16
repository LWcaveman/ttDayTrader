import math
from tastytrade import Account
from tastytrade.instruments import Equity
from tastytrade.order import MarketOrder, OrderAction

async def route_fractional_market_order(prod_session, ticker, total_shares, action="BUY"):
    if not prod_session:
        raise ValueError("Production session required for live routing.")
        
    account = (await Account.get(prod_session))[0]
    symbol = await Equity.get(prod_session, ticker)
    
    whole_shares = math.floor(total_shares)
    fractional_shares = round(total_shares - whole_shares, 4)
    
    order_action = OrderAction.BUY_TO_OPEN if action == "BUY" else OrderAction.SELL_TO_CLOSE
    
    # Send Whole Shares Payload
    if whole_shares > 0:
        leg_whole = symbol.build_leg(whole_shares, order_action)
        order_whole = MarketOrder(legs=[leg_whole])
        await account.place_order(prod_session, order_whole, dry_run=False)
        
    # Send Fractional Payload immediately after
    if fractional_shares > 0:
        leg_fractional = symbol.build_leg(fractional_shares, order_action)
        order_fractional = MarketOrder(legs=[leg_fractional])
        await account.place_order(prod_session, order_fractional, dry_run=False)