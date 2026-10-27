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
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "PRAC-V2-460183-67680195").strip()
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
# Institutional Strategy Configuration: Pure VWAP Reversion & Trailing
# ==========================================
# Stop Loss Inicial universal y por activo (ticks)
INITIAL_SL_TICKS = int(os.getenv("INITIAL_SL_TICKS", "35"))
INITIAL_SL_TICKS_ES = int(os.getenv("INITIAL_SL_TICKS_ES", "35"))     # 8.75 pts ($437.50 en ES / $43.75 en MES)
INITIAL_SL_TICKS_NQ = int(os.getenv("INITIAL_SL_TICKS_NQ", "90"))     # 22.50 pts ($450.00 en NQ / $45.00 en MNQ)
INITIAL_SL_TICKS_YM = int(os.getenv("INITIAL_SL_TICKS_YM", "50"))     # 50.0 pts ($250.00 en YM / $25.00 en MYM)
INITIAL_SL_TICKS_RTY = int(os.getenv("INITIAL_SL_TICKS_RTY", "40"))   # 4.0 pts ($200.00 en RTY / $20.00 en M2K)
INITIAL_SL_TICKS_CL = int(os.getenv("INITIAL_SL_TICKS_CL", "35"))     # 0.35 pts ($350.00 en CL / $35.00 en MCL)
INITIAL_SL_TICKS_GC = int(os.getenv("INITIAL_SL_TICKS_GC", "35"))     # 3.5 pts ($350.00 en GC / $35.00 en MGC)

# Lookback de velas de 5m para evaluar la pendiente direccional del VWAP institucional (7 velas = 35 minutos)
VWAP_SLOPE_LOOKBACK_BARS = int(os.getenv("VWAP_SLOPE_LOOKBACK_BARS", "7"))

# Pendiente mínima requerida en ticks (mantiene compatibilidad con variables previas)
MIN_VWAP_SLOPE_TICKS = int(os.getenv("MIN_VWAP_SLOPE_TICKS", "2"))

# ==========================================
# Institutional Minimum Slope Thresholds by Asset (Pure VWAP 35 min)
# ==========================================
MIN_SLOPE_POINTS_ES = float(os.getenv("MIN_SLOPE_POINTS_ES", "1.0"))      # 1.0 punto (4 ticks)
MIN_SLOPE_POINTS_NQ = float(os.getenv("MIN_SLOPE_POINTS_NQ", "1.5"))      # 1.5 puntos (6 ticks)
MIN_SLOPE_POINTS_YM = float(os.getenv("MIN_SLOPE_POINTS_YM", "2.0"))      # 2.0 puntos (2 ticks)
MIN_SLOPE_POINTS_RTY = float(os.getenv("MIN_SLOPE_POINTS_RTY", "0.30"))   # 0.30 puntos (3 ticks)
MIN_SLOPE_POINTS_OIL = float(os.getenv("MIN_SLOPE_POINTS_OIL", "0.03"))   # 0.03 puntos (3 ticks)
MIN_SLOPE_POINTS_GOLD = float(os.getenv("MIN_SLOPE_POINTS_GOLD", "1.0"))  # 1.0 punto (10 ticks)

MIN_SLOPE_POINTS_BY_SYMBOL = {
    "ES": MIN_SLOPE_POINTS_ES,
    "MES": MIN_SLOPE_POINTS_ES,
    "NQ": MIN_SLOPE_POINTS_NQ,
    "MNQ": MIN_SLOPE_POINTS_NQ,
    "YM": MIN_SLOPE_POINTS_YM,
    "MYM": MIN_SLOPE_POINTS_YM,
    "RTY": MIN_SLOPE_POINTS_RTY,
    "M2K": MIN_SLOPE_POINTS_RTY,
    "CL": MIN_SLOPE_POINTS_OIL,
    "MCL": MIN_SLOPE_POINTS_OIL,
    "GC": MIN_SLOPE_POINTS_GOLD,
    "MGC": MIN_SLOPE_POINTS_GOLD,
}

def get_min_slope_points(symbol: str) -> float:
    """Devuelve la pendiente mínima requerida del VWAP en 35 minutos para el símbolo especificado."""
    s = (symbol or "").upper()
    return MIN_SLOPE_POINTS_BY_SYMBOL.get(s, 1.0)

# Filtro Horario de Inicio de Sesión CME: Bloqueo de 2:30h tras apertura (18:00 a 20:30 ET)
CME_OPEN_BUFFER_MINUTES = int(os.getenv("CME_OPEN_BUFFER_MINUTES", "150"))

# Intervalo central de monitoreo y actualización de mercado (2.0 segundos)
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2.0"))

# ==========================================
# Escalera de Hitos Dinámicos Optimizada (Porcentajes sobre D_base)
# ==========================================
# Micro-Escudo Temprano: Al 20% de recorrido -> Reduce SL inicial a 15 ticks (corta el riesgo más del 55%)
HITO_MICRO_TRIGGER_PCT = float(os.getenv("HITO_MICRO_TRIGGER_PCT", "20.0"))
HITO_MICRO_SL_TICKS = int(os.getenv("HITO_MICRO_SL_TICKS", "15"))

# Hito 1: Al 35% de recorrido -> SL a BE + 1 tick (Riesgo Cero Garantizado)
HITO_1_TRIGGER_PCT = float(os.getenv("HITO_1_TRIGGER_PCT", "35.0"))

# Hito 2: Al 50% de recorrido -> Asegura 35% de D_base, TP ampliado al 120%
HITO_2_TRIGGER_PCT = float(os.getenv("HITO_2_TRIGGER_PCT", "50.0"))
HITO_2_SL_PCT = float(os.getenv("HITO_2_SL_PCT", "0.35"))
HITO_2_TP_EXPAND_PCT = float(os.getenv("HITO_2_TP_EXPAND_PCT", "1.20"))

# Hito 3: Al 75% de recorrido -> Asegura 65% de D_base, TP ampliado al 150%
HITO_3_TRIGGER_PCT = float(os.getenv("HITO_3_TRIGGER_PCT", "75.0"))
HITO_3_SL_PCT = float(os.getenv("HITO_3_SL_PCT", "0.65"))
HITO_3_TP_EXPAND_PCT = float(os.getenv("HITO_3_TP_EXPAND_PCT", "1.50"))

# Hito 4: Al 100% de recorrido (TP inicial alcanzado) -> Asegura 85% de D_base, TP ampliado al 180%
HITO_4_TRIGGER_PCT = float(os.getenv("HITO_4_TRIGGER_PCT", "100.0"))
HITO_4_SL_PCT = float(os.getenv("HITO_4_SL_PCT", "0.85"))
HITO_4_TP_EXPAND_PCT = float(os.getenv("HITO_4_TP_EXPAND_PCT", "1.80"))

# Modo Runner Trailing: Persigue al precio a 15 ticks fijos por detrás del máximo alcanzado tras superar el 100%
RUNNER_TRAILING_TICKS = int(os.getenv("RUNNER_TRAILING_TICKS", "15"))
RUNNER_STEP_PCT = float(os.getenv("RUNNER_STEP_PCT", "20.0"))

# ==========================================
# Candado de Ganancia Diaria Trailing (Trailing Daily Profit Lock)
# ==========================================
# Umbral de activación: Al superar +$750 USD de beneficio acumulado en el día
TRAILING_DAILY_PROFIT_TRIGGER_USD = float(os.getenv("TRAILING_DAILY_PROFIT_TRIGGER_USD", "750.0"))
# Porcentaje de retención obligatoria: Conserva el 70% del pico máximo alcanzado en el día
TRAILING_DAILY_PROFIT_RETAIN_PCT = float(os.getenv("TRAILING_DAILY_PROFIT_RETAIN_PCT", "0.70"))

# ==========================================
# Canasta de Ganancia Global (Portfolio Basket Take-Profit)
# ==========================================
# Umbral de recogida en bloque: si la suma flotante de todas las posiciones alcanza $850 USD, se asegura la ganancia
BASKET_TP_TARGET_USD = float(os.getenv("BASKET_TP_TARGET_USD", "850.0"))
BASKET_TP_HARD_USD = float(os.getenv("BASKET_TP_HARD_USD", "900.0"))
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

# Distancia máxima al VWAP para activación de entrada (en ticks)
# ES: 15 ticks (3.75 pts). Se escala proporcionalmente para cada símbolo según su SL base.
VWAP_TRIGGER_DISTANCE_TICKS_ES = int(os.getenv("VWAP_TRIGGER_DISTANCE_TICKS_ES", "15"))

def get_vwap_trigger_distance_ticks(symbol: str) -> int:
    """Devuelve la distancia máxima en ticks al VWAP para activar la entrada según la volatilidad del símbolo."""
    sl_init = get_default_sl_ticks(symbol)
    ratio = VWAP_TRIGGER_DISTANCE_TICKS_ES / float(INITIAL_SL_TICKS_ES)
    return max(round(sl_init * ratio), 1)

# Símbolos habilitados por defecto en el arranque (Minis y Micros habilitados simultáneamente)
DEFAULT_ENABLED_SYMBOLS_STR = os.getenv("ENABLED_SYMBOLS", "ES,NQ,MES,MNQ")
DEFAULT_ENABLED_SYMBOLS = [s.strip().upper() for s in DEFAULT_ENABLED_SYMBOLS_STR.split(",") if s.strip()]
