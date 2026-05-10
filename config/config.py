import os

from dotenv import load_dotenv

load_dotenv()


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


# Spot: live api.binance.com when true; testnet when false (see .env.example).
PRODUCTION = _env_truthy("PRODUCTION")

if PRODUCTION:
    BINANCE_CONFIG = {
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_API_SECRET"),
        "sandbox": False,
        "options": {
            "defaultType": "spot",
        },
        "enableRateLimit": True,
    }
else:
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

# OKX spot — CCXT requires ``password`` (API passphrase).
if PRODUCTION:
    OKX_CONFIG = {
        "apiKey": os.getenv("OKX_API_KEY"),
        "secret": os.getenv("OKX_API_SECRET"),
        "password": os.getenv("OKX_PASSPHRASE", "") or os.getenv("OKX_PASSWORD", ""),
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
else:
    OKX_CONFIG = {
        "apiKey": os.getenv("OKX_TESTNET_API_KEY") or os.getenv("OKX_API_KEY"),
        "secret": os.getenv("OKX_TESTNET_SECRET") or os.getenv("OKX_API_SECRET"),
        "password": os.getenv("OKX_TESTNET_PASSPHRASE")
        or os.getenv("OKX_PASSPHRASE", "")
        or os.getenv("OKX_PASSWORD", ""),
        "sandbox": True,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
