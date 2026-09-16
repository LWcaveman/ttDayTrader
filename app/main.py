import asyncio
import yaml
from dotenv import load_dotenv
from app.auth.session import get_sessions
from app.scanner.premarket import run_screener
from app.data.stream import start_market_stream
from app.state.db import StateManager
from app.execution.rh_router import login_robinhood

async def main():
    print("Initializing Fashionably Late Daemon...")
    
    load_dotenv()
    with open("config.yaml", "r") as file:
        config = yaml.safe_load(file)
        
    db = StateManager()
    await db.initialize_db()
    
    prod_session, cert_session = get_sessions()
    if not prod_session:
        print("CRITICAL: No production session available for Market Data. Exiting...")
        return
        
    active_tickers = run_screener(config)
    if not active_tickers:
        print("No eligible tickers for today. Shutting down daemon.")
        return
        
    print("Authenticating Robinhood Execution Engine...")
    await login_robinhood()
    
    await start_market_stream(prod_session, cert_session, active_tickers, config, db)

if __name__ == "__main__":
    asyncio.run(main())