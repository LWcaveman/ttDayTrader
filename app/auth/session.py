import os
from tastytrade import Session

def get_sessions():
    """
    Returns (prod_session, cert_session) using Tastytrade's OAuth2 authentication.
    """
    # Production
    prod_secret = os.getenv("TT_PROD_CLIENT_SECRET")
    prod_refresh = os.getenv("TT_PROD_REFRESH_TOKEN")
    
    # Certification (Sandbox)
    cert_secret = os.getenv("TT_CERT_CLIENT_SECRET")
    cert_refresh = os.getenv("TT_CERT_REFRESH_TOKEN")
    
    prod_session = None
    cert_session = None
    
    if prod_secret and prod_refresh:
        print("Authenticating Production Session (Market Data) via OAuth...")
        # FIX: Changed client_secret to provider_secret
        prod_session = Session(provider_secret=prod_secret, refresh_token=prod_refresh)
        
    if cert_secret and cert_refresh:
        print("Authenticating Certification Session (Sandbox Orders) via OAuth...")
        # FIX: Changed client_secret to provider_secret
        cert_session = Session(
            provider_secret=cert_secret, 
            refresh_token=cert_refresh, 
            is_test=True
        )
        
    return prod_session, cert_session