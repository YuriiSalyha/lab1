import os

from dotenv import load_dotenv

load_dotenv()

BINANCE_CONFIG = {
    "apiKey": os.getenv("BINANCE_TESTNET_API_KEY"),
    "secret": os.getenv("BINANCE_TESTNET_SECRET"),
    "sandbox": True,
    "options": {
        "defaultType": "spot",
    },
    "enableRateLimit": True,
}

# Bybit unified account / testnet (ccxt uses sandbox + defaultType spot for spot testnet).
BYBIT_CONFIG = {
    "apiKey": os.getenv("BYBIT_TESTNET_API_KEY"),
    "secret": os.getenv("BYBIT_TESTNET_SECRET"),
    "sandbox": True,
    "options": {
        "defaultType": "spot",
    },
    "enableRateLimit": True,
}
