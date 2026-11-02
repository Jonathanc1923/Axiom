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
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "PRAC-V2-460183-45875636").strip()
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
# Configuración de Estrategia: RECHAZO DE MECHA EN VWAP (SL 50% BANDA / TP 60% BANDA / BE VWAP+2T @ 50%)
# ==========================================
STRATEGY_NAME = "VWAP Wick Rejection (SL 50% Banda | TP 60% | Dinámico BE VWAP+2t @ 50%)"

# Intervalo central de monitoreo y actualización de mercado (cada 1.0 segundo estricto)
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "1.0"))

# Sincronización oficial autoritativa con TopstepX API en tiempo real (segundos)
TOPSTEP_SYNC_INTERVAL_SECONDS = float(os.getenv("TOPSTEP_SYNC_INTERVAL_SECONDS", "5.0"))

# Stop Loss Inicial: 50% de distancia entre el VWAP y la banda contraria (+músculo de aguante)
SL_VWAP_BAND_RATIO = float(os.getenv("SL_VWAP_BAND_RATIO", "0.50"))

# Take Profit Inicial: 60% de distancia entre el VWAP y la siguiente banda
TP_VWAP_BAND_RATIO = float(os.getenv("TP_VWAP_BAND_RATIO", "0.60"))

# Dinamismo de SL: Al avanzar a favor >= 50% del TP, asegurar VWAP + 2 ticks a favor
DYNAMIC_BE_PROFIT_PCT = float(os.getenv("DYNAMIC_BE_PROFIT_PCT", "50.0"))
DYNAMIC_BE_PROFIT_TICKS = int(os.getenv("DYNAMIC_BE_PROFIT_TICKS", "2"))
DYNAMIC_BE_REFERENCE = os.getenv("DYNAMIC_BE_REFERENCE", "VWAP").upper()

# Filtro de Entrada Tarde: Máximo % del TP consumido antes de entrar (evita entradas agotadas)
MAX_ENTRY_TP_CONSUMED_PCT = float(os.getenv("MAX_ENTRY_TP_CONSUMED_PCT", "50.0"))
# Mínimo ratio Riesgo/Beneficio requerido (TP restante / SL riesgo)
MIN_SETUP_RR_RATIO = float(os.getenv("MIN_SETUP_RR_RATIO", "0.40"))
# Mínimo de ticks de TP para abrir la operación (evita micro-ganancias que se van en comisiones)
MIN_TP_TICKS = int(os.getenv("MIN_TP_TICKS", "6"))

# Compatibilidad con módulos y variables previas
CHECK_WINDOW_START_MINUTE = 0
VWAP_TOUCH_RETREAT_TICKS = 0
SL_VWAP_RATIO = SL_VWAP_BAND_RATIO
SL_VWAP_BUFFER_TICKS = 0
SYMBOL_COOLDOWN_MINUTES = 0
SYMBOL_COOLDOWN_SECONDS = 0

# Máximo de operaciones simultáneas permitidas por símbolo
MAX_POSITIONS_PER_SYMBOL = int(os.getenv("MAX_POSITIONS_PER_SYMBOL", "1"))

# Horarios: Calendario oficial CME (Apertura Domingo 18:00 ET hasta Viernes 17:00 ET; Pausa diaria 17:00 a 18:00 ET)
SESSION_START_HOUR_PERU = 18
SESSION_START_MINUTE_PERU = 0
CME_OPEN_BUFFER_MINUTES = 0

# Candados de sesión y canasta desactivados
USE_DAILY_PROFIT_LOCK = False
USE_SESSION_PROFIT_TARGET = False
USE_BASKET_TP = False
SESSION_PROFIT_TARGET_USD = 0.0
TRAILING_DAILY_PROFIT_TRIGGER_USD = 0.0
TRAILING_DAILY_PROFIT_RETAIN_PCT = 0.0
BASKET_TP_TARGET_USD = 0.0
BASKET_TP_HARD_USD = 0.0
BASKET_COOLDOWN_SECONDS = 0

# Trailing y filtros previos eliminados
INITIAL_SL_OPPOSITE_BAND_RATIO = 0.0
INITIAL_TP_TARGET_BAND_RATIO = 0.0
USE_DYNAMIC_TRAILING = False
TRIGGER_TP_ADVANCE_PCT = 0.0
LOCK_PROFIT_TICKS = 0
EXPAND_TP_PCT = 0.0
STEP_SL_PCT = 0.0
MIN_PREV_CANDLE_DIFF_TICKS = 0
USE_VWAP_SLOPE_FILTER = False
VWAP_SLOPE_LOOKBACK_BARS = 0
MIN_VWAP_SLOPE_TICKS = 0
INITIAL_SL_TICKS = 35
INITIAL_SL_TICKS_ES = 35
INITIAL_SL_TICKS_NQ = 90
INITIAL_SL_TICKS_YM = 50
INITIAL_SL_TICKS_RTY = 40
INITIAL_SL_TICKS_CL = 35
INITIAL_SL_TICKS_GC = 35
VWAP_TRIGGER_DISTANCE_TICKS = 0
VWAP_TRIGGER_DISTANCE_TICKS_ES = 0
RUNNER_TRAILING_TICKS = 0
RUNNER_STEP_PCT = 0.0
HITO_MICRO_TRIGGER_PCT = 0.0
HITO_MICRO_SL_TICKS = 0
HITO_1_TRIGGER_PCT = 0.0
HITO_2_TRIGGER_PCT = 0.0
HITO_2_SL_PCT = 0.0
HITO_2_TP_EXPAND_PCT = 0.0
HITO_3_TRIGGER_PCT = 0.0
HITO_3_SL_PCT = 0.0
HITO_3_TP_EXPAND_PCT = 0.0
HITO_4_TRIGGER_PCT = 0.0
HITO_4_SL_PCT = 0.0
HITO_4_TP_EXPAND_PCT = 0.0

def get_vwap_trigger_distance_ticks(symbol: str) -> int:
    return 0

def get_min_slope_points(symbol: str) -> float:
    return 0.0

# ==========================================
# Supported Institutional CME Futures Specifications
# ==========================================
SUPPORTED_SYMBOLS = {
    "ES": {
        "name": "E-mini S&P 500",
        "symbolPrefix": "F.US.EP",
        "searchKey": "ES",
        "tickSize": 0.25,
        "tickValue": 12.50, # USD per tick per contract
        "pointValue": 50.0,
        "defaultSlTicks": INITIAL_SL_TICKS_ES,
    },
    "NQ": {
        "name": "E-mini Nasdaq-100",
        "symbolPrefix": "F.US.ENQ",
        "searchKey": "NQ",
        "tickSize": 0.25,
        "tickValue": 5.00,
        "pointValue": 20.0,
        "defaultSlTicks": INITIAL_SL_TICKS_NQ,
    },
    "YM": {
        "name": "E-mini Dow Jones ($5)",
        "symbolPrefix": "F.US.YM",
        "searchKey": "YM",
        "tickSize": 1.0,
        "tickValue": 5.00,
        "pointValue": 5.0,
        "defaultSlTicks": INITIAL_SL_TICKS_YM,
    },
    "RTY": {
        "name": "E-mini Russell 2000",
        "symbolPrefix": "F.US.RTY",
        "searchKey": "RTY",
        "tickSize": 0.1,
        "tickValue": 5.00,
        "pointValue": 50.0,
        "defaultSlTicks": INITIAL_SL_TICKS_RTY,
    },
    "MES": {
        "name": "Micro E-mini S&P 500",
        "symbolPrefix": "F.US.MES",
        "searchKey": "MES",
        "tickSize": 0.25,
        "tickValue": 1.25,
        "pointValue": 5.0,
        "defaultSlTicks": INITIAL_SL_TICKS_ES,
    },
    "MNQ": {
        "name": "Micro E-mini Nasdaq-100",
        "symbolPrefix": "F.US.MNQ",
        "searchKey": "MNQ",
        "tickSize": 0.25,
        "tickValue": 0.50,
        "pointValue": 2.0,
        "defaultSlTicks": INITIAL_SL_TICKS_NQ,
    },
    "CL": {
        "name": "Crude Oil Futures",
        "symbolPrefix": "F.US.CLE",
        "searchKey": "CL",
        "tickSize": 0.01,
        "tickValue": 10.00,
        "pointValue": 1000.0,
        "defaultSlTicks": INITIAL_SL_TICKS_CL,
    },
    "GC": {
        "name": "Gold Futures",
        "symbolPrefix": "F.US.GCE",
        "searchKey": "GC",
        "tickSize": 0.1,
        "tickValue": 10.00,
        "pointValue": 100.0,
        "defaultSlTicks": INITIAL_SL_TICKS_GC,
    },
    "MYM": {
        "name": "Micro E-mini Dow Jones",
        "symbolPrefix": "F.US.MYM",
        "searchKey": "MYM",
        "tickSize": 1.0,
        "tickValue": 0.50,
        "pointValue": 0.50,
        "defaultSlTicks": INITIAL_SL_TICKS_YM,
    },
    "M2K": {
        "name": "Micro E-mini Russell 2000",
        "symbolPrefix": "F.US.M2K",
        "searchKey": "M2K",
        "tickSize": 0.1,
        "tickValue": 0.50,
        "pointValue": 5.0,
        "defaultSlTicks": INITIAL_SL_TICKS_RTY,
    },
    "MGC": {
        "name": "Micro Gold Futures",
        "symbolPrefix": "F.US.MGC",
        "searchKey": "MGC",
        "tickSize": 0.1,
        "tickValue": 1.00,
        "pointValue": 10.0,
        "defaultSlTicks": INITIAL_SL_TICKS_GC,
    },
    "MCL": {
        "name": "Micro Crude Oil Futures",
        "symbolPrefix": "F.US.MCLE",
        "searchKey": "MCL",
        "tickSize": 0.01,
        "tickValue": 1.00,
        "pointValue": 100.0,
        "defaultSlTicks": INITIAL_SL_TICKS_CL,
    }
}

def get_default_sl_ticks(symbol: str) -> int:
    """Devuelve el Stop Loss inicial en ticks configurado para el símbolo especificado."""
    s = (symbol or "").upper()
    if s in SUPPORTED_SYMBOLS:
        return int(SUPPORTED_SYMBOLS[s].get("defaultSlTicks", INITIAL_SL_TICKS))
    return INITIAL_SL_TICKS

def get_micro_shield_sl_ticks(symbol: str) -> int:
    """Devuelve los ticks de SL para el Micro-Escudo (reducido ~55-60% del SL inicial de cada activo)."""
    sl_init = get_default_sl_ticks(symbol)
    shield = round(sl_init * (15.0 / 35.0))
    return max(shield, 10)

# Símbolos habilitados por defecto en el arranque (NQ y MNQ pausados por defecto)
DEFAULT_ENABLED_SYMBOLS_STR = os.getenv("ENABLED_SYMBOLS", "ES,YM,RTY,CL,GC,MES,MYM,M2K,MCL,MGC")
DEFAULT_ENABLED_SYMBOLS = [s.strip().upper() for s in DEFAULT_ENABLED_SYMBOLS_STR.split(",") if s.strip()]
