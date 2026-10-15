import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if it exists
base_dir = Path(__file__).resolve().parent
dotenv_path = base_dir / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)

# ==========================================
# Core TopstepX API Configuration
# ==========================================
TOPSTEP_USERNAME = os.getenv("TOPSTEP_USERNAME", "jonathanencina2020@gmail.com")
TOPSTEP_API_KEY = os.getenv("TOPSTEP_API_KEY", "O/Jvyz5VggKPKRLF69YDGJQjXIcxsKuxZbJg9rgwek0=")
TOPSTEP_BASE_URL = os.getenv("TOPSTEP_BASE_URL", "https://api.topstepx.com").rstrip("/")

# Practice / Safety Filter
ACCOUNT_FILTER = os.getenv("ACCOUNT_FILTER", "PRAC").upper()

# Default Symbol on Startup
DEFAULT_SYMBOL = os.getenv("DEFAULT_SYMBOL", "ES").upper()

# Autonomous Engine Autostart
AUTO_START_ENGINE = os.getenv("AUTO_START_ENGINE", "true").lower() in ("true", "1", "yes")

# Port for Web Server (Render injects PORT)
PORT = int(os.getenv("PORT", "8000"))

# Data directory and operations CSV file
DATA_DIR = os.getenv("DATA_DIR", str(base_dir))
CSV_DIR = DATA_DIR
CSV_PATH = os.path.join(DATA_DIR, "registro_operaciones.csv")

# ==========================================
# Supported Institutional CME Futures
# ==========================================
SUPPORTED_SYMBOLS = {
    "ES": {
        "name": "E-mini S&P 500",
        "symbolPrefix": "F.US.EP",
        "searchKey": "ES",
        "tickSize": 0.25,
        "tickValue": 12.50, # USD per tick per contract
        "pointValue": 50.0,
        "defaultSlPoints": 1.25, # 5 ticks
    },
    "NQ": {
        "name": "E-mini Nasdaq-100",
        "symbolPrefix": "F.US.ENQ",
        "searchKey": "NQ",
        "tickSize": 0.25,
        "tickValue": 5.00,
        "pointValue": 20.0,
        "defaultSlPoints": 3.00, # 12 ticks
    },
    "YM": {
        "name": "E-mini Dow Jones ($5)",
        "symbolPrefix": "F.US.YM",
        "searchKey": "YM",
        "tickSize": 1.0,
        "tickValue": 5.00,
        "pointValue": 5.0,
        "defaultSlPoints": 10.0, # 10 ticks
    },
    "RTY": {
        "name": "E-mini Russell 2000",
        "symbolPrefix": "F.US.RTY",
        "searchKey": "RTY",
        "tickSize": 0.1,
        "tickValue": 5.00,
        "pointValue": 50.0,
        "defaultSlPoints": 1.0, # 10 ticks
    },
    "MES": {
        "name": "Micro E-mini S&P 500",
        "symbolPrefix": "F.US.MES",
        "searchKey": "MES",
        "tickSize": 0.25,
        "tickValue": 1.25,
        "pointValue": 5.0,
        "defaultSlPoints": 1.25,
    },
    "MNQ": {
        "name": "Micro E-mini Nasdaq-100",
        "symbolPrefix": "F.US.MNQ",
        "searchKey": "MNQ",
        "tickSize": 0.25,
        "tickValue": 0.50,
        "pointValue": 2.0,
        "defaultSlPoints": 3.00,
    },
    "CL": {
        "name": "Crude Oil Futures",
        "symbolPrefix": "F.US.CLE",
        "searchKey": "CL",
        "tickSize": 0.01,
        "tickValue": 10.00,
        "pointValue": 1000.0,
        "defaultSlPoints": 0.15,
    },
    "GC": {
        "name": "Gold Futures",
        "symbolPrefix": "F.US.GCE",
        "searchKey": "GC",
        "tickSize": 0.1,
        "tickValue": 10.00,
        "pointValue": 100.0,
        "defaultSlPoints": 1.5,
    },
    "MYM": {
        "name": "Micro E-mini Dow Jones",
        "symbolPrefix": "F.US.MYM",
        "searchKey": "MYM",
        "tickSize": 1.0,
        "tickValue": 0.50,
        "pointValue": 0.50,
        "defaultSlPoints": 10.0,
    },
    "M2K": {
        "name": "Micro E-mini Russell 2000",
        "symbolPrefix": "F.US.M2K",
        "searchKey": "M2K",
        "tickSize": 0.1,
        "tickValue": 0.50,
        "pointValue": 5.0,
        "defaultSlPoints": 1.0,
    },
    "MGC": {
        "name": "Micro Gold Futures",
        "symbolPrefix": "F.US.MGC",
        "searchKey": "MGC",
        "tickSize": 0.1,
        "tickValue": 1.00,
        "pointValue": 10.0,
        "defaultSlPoints": 1.5,
    },
    "MCL": {
        "name": "Micro Crude Oil Futures",
        "symbolPrefix": "F.US.MCLE",
        "searchKey": "MCL",
        "tickSize": 0.01,
        "tickValue": 1.00,
        "pointValue": 100.0,
        "defaultSlPoints": 0.15,
    }
}
