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
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "PRAC-V2-460183-28640537").strip()
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
# Institutional Strategy Configuration: Dynamic Band Reversion & Multi-Position
# ==========================================
# SL Inicial: 45% de distancia entre la línea VWAP y la banda contraria (Fijo, no dinámico)
INITIAL_SL_OPPOSITE_BAND_RATIO = float(os.getenv("INITIAL_SL_OPPOSITE_BAND_RATIO", "0.45"))

# TP Inicial: 50% de distancia entre línea VWAP y la banda objetivo (Fijo, no dinámico)
INITIAL_TP_TARGET_BAND_RATIO = float(os.getenv("INITIAL_TP_TARGET_BAND_RATIO", "0.50"))

# Trailing Dinámico: Desactivado por solicitud ("ya sin sl ni tp dinamicos, ahora son fijos")
USE_DYNAMIC_TRAILING = os.getenv("USE_DYNAMIC_TRAILING", "false").lower() in ("true", "1", "yes")

# Gatillo de protección (mantiene compatibilidad de hitos si se reactiva)
TRIGGER_TP_ADVANCE_PCT = float(os.getenv("TRIGGER_TP_ADVANCE_PCT", "25.0"))
LOCK_PROFIT_TICKS = int(os.getenv("LOCK_PROFIT_TICKS", "5"))
EXPAND_TP_PCT = float(os.getenv("EXPAND_TP_PCT", "25.0"))
STEP_SL_PCT = float(os.getenv("STEP_SL_PCT", "25.0"))

# Máximo de operaciones simultáneas permitidas por símbolo (1 operación por activo para gestión estricta)
MAX_POSITIONS_PER_SYMBOL = int(os.getenv("MAX_POSITIONS_PER_SYMBOL", "1"))

# Diferencia mínima en ticks requerida para la vela anterior (t-1) respecto al VWAP (1 tick mínimo)
MIN_PREV_CANDLE_DIFF_TICKS = int(os.getenv("MIN_PREV_CANDLE_DIFF_TICKS", "1"))

# Filtro de Tendencia (Pendiente VWAP): Activo con lookback de 7 velas (35m) y mínimo de 1 tick a favor
USE_VWAP_SLOPE_FILTER = os.getenv("USE_VWAP_SLOPE_FILTER", "true").lower() in ("true", "1", "yes")
VWAP_SLOPE_LOOKBACK_BARS = int(os.getenv("VWAP_SLOPE_LOOKBACK_BARS", "7"))
MIN_VWAP_SLOPE_TICKS = int(os.getenv("MIN_VWAP_SLOPE_TICKS", "1"))

# Stop Loss Inicial de respaldo universal y por activo (ticks) si no hay banda calculada
INITIAL_SL_TICKS = int(os.getenv("INITIAL_SL_TICKS", "35"))
INITIAL_SL_TICKS_ES = int(os.getenv("INITIAL_SL_TICKS_ES", "35"))
INITIAL_SL_TICKS_NQ = int(os.getenv("INITIAL_SL_TICKS_NQ", "90"))
INITIAL_SL_TICKS_YM = int(os.getenv("INITIAL_SL_TICKS_YM", "50"))
INITIAL_SL_TICKS_RTY = int(os.getenv("INITIAL_SL_TICKS_RTY", "40"))
INITIAL_SL_TICKS_CL = int(os.getenv("INITIAL_SL_TICKS_CL", "35"))
INITIAL_SL_TICKS_GC = int(os.getenv("INITIAL_SL_TICKS_GC", "35"))

# Distancia de activación de entrada al VWAP: 5 ticks exactos para todos los símbolos
VWAP_TRIGGER_DISTANCE_TICKS = int(os.getenv("VWAP_TRIGGER_DISTANCE_TICKS", "5"))
VWAP_TRIGGER_DISTANCE_TICKS_ES = VWAP_TRIGGER_DISTANCE_TICKS

def get_vwap_trigger_distance_ticks(symbol: str) -> int:
    """Devuelve la distancia máxima en ticks al VWAP para activar la entrada (5 ticks exactos)."""
    return VWAP_TRIGGER_DISTANCE_TICKS

# Umbrales de pendiente por símbolo: mínimo 1 tick del activo
def get_min_slope_points(symbol: str) -> float:
    s = (symbol or "").upper()
    spec = SUPPORTED_SYMBOLS.get(s, {})
    return float(spec.get("tickSize", 0.25) * MIN_VWAP_SLOPE_TICKS)

# Filtro Horario de Inicio de Sesión CME: 0 minutos (apertura inmediata a las 18:00 ET)
CME_OPEN_BUFFER_MINUTES = int(os.getenv("CME_OPEN_BUFFER_MINUTES", "0"))

# Intervalo central de monitoreo y actualización de mercado (2.0 segundos)
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2.0"))

# Modo Runner Trailing: Persigue al precio a 15 ticks fijos por detrás del máximo alcanzado
RUNNER_TRAILING_TICKS = int(os.getenv("RUNNER_TRAILING_TICKS", "15"))
RUNNER_STEP_PCT = float(os.getenv("RUNNER_STEP_PCT", "25.0"))

# Compatibilidad con definiciones previas de hitos
HITO_MICRO_TRIGGER_PCT = 20.0
HITO_MICRO_SL_TICKS = 15
HITO_1_TRIGGER_PCT = 25.0
HITO_2_TRIGGER_PCT = 50.0
HITO_2_SL_PCT = 0.25
HITO_2_TP_EXPAND_PCT = 1.25
HITO_3_TRIGGER_PCT = 75.0
HITO_3_SL_PCT = 0.50
HITO_3_TP_EXPAND_PCT = 1.50
HITO_4_TRIGGER_PCT = 100.0
HITO_4_SL_PCT = 0.75
HITO_4_TP_EXPAND_PCT = 1.75

# ==========================================
# Meta de Ganancia Total de la Sesión (Session Complete Profit Stop)
# ==========================================
# Parar operaciones si se llega a tener este profit en la sesión completa ($1,500 USD)
SESSION_PROFIT_TARGET_USD = float(os.getenv("SESSION_PROFIT_TARGET_USD", "1500.0"))
TRAILING_DAILY_PROFIT_TRIGGER_USD = float(os.getenv("TRAILING_DAILY_PROFIT_TRIGGER_USD", "1500.0"))
TRAILING_DAILY_PROFIT_RETAIN_PCT = float(os.getenv("TRAILING_DAILY_PROFIT_RETAIN_PCT", "0.70"))

# ==========================================
# Canasta de Ganancia Global (Portfolio Basket Take-Profit)
# ==========================================
# Recolectar todo cuando vayan ganando $750 USD en total de símbolos a la vez
BASKET_TP_TARGET_USD = float(os.getenv("BASKET_TP_TARGET_USD", "750.0"))
BASKET_TP_HARD_USD = float(os.getenv("BASKET_TP_HARD_USD", "800.0"))
BASKET_COOLDOWN_SECONDS = int(os.getenv("BASKET_COOLDOWN_SECONDS", "300")) # 5 minutos de pausa tras cerrar la canasta

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

# Símbolos habilitados por defecto en el arranque (Minis y Micros habilitados simultáneamente)
DEFAULT_ENABLED_SYMBOLS_STR = os.getenv("ENABLED_SYMBOLS", "ES,NQ,MES,MNQ")
DEFAULT_ENABLED_SYMBOLS = [s.strip().upper() for s in DEFAULT_ENABLED_SYMBOLS_STR.split(",") if s.strip()]
