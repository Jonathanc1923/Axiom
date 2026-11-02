import os
import sys
import io
import csv
import time
import threading
import requests
import re
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import numpy as np

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import (
    CSV_PATH, CSV_DIR, SUPPORTED_SYMBOLS, DEFAULT_SYMBOL, DATA_DIR,
    INITIAL_SL_TICKS, get_default_sl_ticks, get_micro_shield_sl_ticks, DEFAULT_ENABLED_SYMBOLS,
    get_vwap_trigger_distance_ticks, VWAP_TRIGGER_DISTANCE_TICKS_ES,
    POLL_INTERVAL_SECONDS, VWAP_SLOPE_LOOKBACK_BARS,
    MIN_VWAP_SLOPE_TICKS, get_min_slope_points, BASKET_TP_TARGET_USD, BASKET_TP_HARD_USD, BASKET_COOLDOWN_SECONDS,
    INITIAL_SL_OPPOSITE_BAND_RATIO, INITIAL_TP_TARGET_BAND_RATIO,
    TRIGGER_TP_ADVANCE_PCT, LOCK_PROFIT_TICKS, EXPAND_TP_PCT, STEP_SL_PCT,
    MAX_POSITIONS_PER_SYMBOL, MIN_PREV_CANDLE_DIFF_TICKS, USE_VWAP_SLOPE_FILTER,
    USE_DYNAMIC_TRAILING,
    SESSION_PROFIT_TARGET_USD,
    HITO_MICRO_TRIGGER_PCT, HITO_MICRO_SL_TICKS,
    HITO_1_TRIGGER_PCT, HITO_2_TRIGGER_PCT, HITO_2_SL_PCT, HITO_2_TP_EXPAND_PCT,
    HITO_3_TRIGGER_PCT, HITO_3_SL_PCT, HITO_3_TP_EXPAND_PCT,
    HITO_4_TRIGGER_PCT, HITO_4_SL_PCT, HITO_4_TP_EXPAND_PCT, RUNNER_TRAILING_TICKS, RUNNER_STEP_PCT,
    SESSION_START_HOUR_PERU, SESSION_START_MINUTE_PERU,
    CME_OPEN_BUFFER_MINUTES, TRAILING_DAILY_PROFIT_TRIGGER_USD, TRAILING_DAILY_PROFIT_RETAIN_PCT,
    STRATEGY_NAME, CHECK_WINDOW_START_MINUTE, SL_VWAP_RATIO, SL_VWAP_BAND_RATIO, TP_VWAP_BAND_RATIO,
    VWAP_TOUCH_RETREAT_TICKS, SL_VWAP_BUFFER_TICKS,
    SYMBOL_COOLDOWN_MINUTES, SYMBOL_COOLDOWN_SECONDS, USE_BASKET_TP, USE_DAILY_PROFIT_LOCK,
    DYNAMIC_BE_PROFIT_PCT, DYNAMIC_BE_PROFIT_TICKS, DYNAMIC_BE_REFERENCE,
    MAX_ENTRY_TP_CONSUMED_PCT, MIN_SETUP_RR_RATIO, MIN_TP_TICKS,
    TOPSTEP_SYNC_INTERVAL_SECONDS
)
from topstep_client import TopstepClient

def safe_parse_iso_timestamp(ts_str: str) -> datetime:
    """Parsea timestamps ISO 8601 de TopstepX de forma segura."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        s = ts_str.replace("Z", "+00:00")
        match = re.search(r'\.(\d+)([+-]\d{2}:\d{2})?$', s)
        if match:
            frac = match.group(1)[:6].ljust(6, '0')
            tz = match.group(2) or "+00:00"
            s = re.sub(r'\.\d+([+-]\d{2}:\d{2})?$', f'.{frac}{tz}', s)
        return datetime.fromisoformat(s)
    except Exception:
        try:
            clean_s = ts_str.split('.')[0].replace("T", " ")
            return datetime.strptime(clean_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

def safe_float(val: Any, default: float = 0.0) -> float:
    """Convierte de forma segura strings o números a float sin fallar en 'N/A' o None."""
    if val is None or val == "" or str(val).strip().upper() == "N/A":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def round_to_tick(val: float, tick_size: float) -> float:
    """Redondea un precio al múltiplo exacto de tick_size eliminando artefactos de punto flotante."""
    if tick_size <= 0:
        return round(val, 2)
    s = f"{tick_size:.6f}".rstrip('0')
    decimals = len(s.split('.')[1]) if '.' in s else 0
    ticks = round(val / tick_size)
    return round(ticks * tick_size, decimals)

class AxiomEngine:
    def __init__(self, client: TopstepClient, default_symbol: str = DEFAULT_SYMBOL, csv_path: Optional[str] = None):
        self.client = client
        self.active_symbol = default_symbol.upper()
        self.account_id: Optional[int] = None
        self.account_name: str = "N/A"
        
        # State flags
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.trade_monitor_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        
        # Multi-Symbol Registry
        self.symbol_states: Dict[str, Dict[str, Any]] = {}
        for sym, spec in SUPPORTED_SYMBOLS.items():
            self.symbol_states[sym] = {
                "symbol": sym,
                "name": spec.get("name", sym),
                "enabled": (sym in DEFAULT_ENABLED_SYMBOLS), # Minis y Micros habilitados simultáneamente por defecto
                "contract_id": None,
                "contract_name": "N/A",
                "tick_size": spec.get("tickSize", 0.25),
                "point_value": spec.get("pointValue", 50.0),
                "telemetry": {},
                "latest_df": pd.DataFrame(),
                "cooldown_until": 0,
                "latest_candle_time": None,
                "last_executed_candle_ts": None,
                "current_active_candle_ts": None,
                "active_trade": None,
                "has_open_position": False,
                "last_inoperable_reason": None,
                "last_poll_time": 0.0,
                "last_5m_fetch_time": 0.0,
                "stats": {"trades": 0, "longs": 0, "shorts": 0, "executed": 0, "rejected": 0}
            }
            
        # Dedicated Real-time Logs per Symbol (Retención 24 Horas)
        self.csv_path: str = csv_path or CSV_PATH
        self.logs_file_path = os.path.join(DATA_DIR, "logs_history_24h.jsonl")
        self.symbol_logs: Dict[str, List[Dict[str, Any]]] = {sym: [] for sym in SUPPORTED_SYMBOLS}
        self.log_history: List[Dict[str, Any]] = []
        self.max_logs: int = 50000
        self.log_listeners: List[Callable[[Dict[str, Any]], None]] = []
        
        # Cargar historial previo de logs
        self.load_persisted_logs()
        
        # Sincronización oficial en tiempo real con TopstepX
        self.last_sync_time: float = 0
        self.sync_interval_seconds: float = TOPSTEP_SYNC_INTERVAL_SECONDS

        # Consolidado de métricas
        self.stats: Dict[str, Any] = {
            "total_trades": 0,
            "executed_trades": 0,
            "rejected_trades": 0,
            "longs": 0,
            "shorts": 0,
            "real_balance": 0.0,
            "gross_pnl_usd": 0.0,
            "total_fees_usd": 0.0,
            "net_pnl_usd": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "winning_trades": 0,
            "losing_trades": 0,
            "last_sync": "Pendiente",
            "next_sync": "Pendiente",
            "by_symbol": {}
        }

        # Cache de hitos por orden
        self.trade_milestones_cache: Dict[str, Dict[str, Any]] = {}

        # Candado de Ganancia Diaria Trailing (Trailing Daily Profit Lock)
        self.daily_lock_active: bool = False
        self.daily_peak_pnl: float = 0.0
        self.daily_floor_pnl: float = 0.0
        self.daily_lock_reason: Optional[str] = None
        self.current_trading_day: Optional[str] = None

    @staticmethod
    def format_milestone(stage: float) -> str:
        """Formatea el estado del dinamismo de SL."""
        if stage >= 1.0:
            return "BE+2t (Protegido)"
        return "Inicial (SL VWAP)"

    # ==========================================
    # LOGGING & 24-HOUR TERMINAL EVENT BROADCASTER
    # ==========================================
    def load_persisted_logs(self):
        """Carga en memoria los logs de las últimas 24 horas guardados en disco."""
        if not os.path.isfile(self.logs_file_path):
            return
            
        cutoff = time.time() - 86400 # 24 horas
        valid_entries = []
        try:
            with open(self.logs_file_path, "r", encoding="utf-8") as lf:
                for line in lf:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("epoch", 0) >= cutoff:
                            valid_entries.append(entry)
                    except Exception:
                        continue
        except Exception as e:
            print(f"⚠️ Error cargando logs previos: {e}")
            
        with self.lock:
            self.log_history = valid_entries[-self.max_logs:]
            for entry in self.log_history:
                sym = entry.get("symbol")
                if sym and sym in self.symbol_logs:
                    self.symbol_logs[sym].append(entry)
                    
        print(f"💾 {len(self.log_history)} eventos institucionales de las últimas 24h restaurados en memoria.")

    def log(self, message: str, level: str = "INFO", symbol: Optional[str] = None):
        """
        Emite logs en tiempo real hacia la consola de Python, los WebSockets y el stream de 24h.
        """
        now = datetime.now()
        timestamp_str = now.strftime("%H:%M:%S")
        epoch_now = time.time()
        
        target_sym = (symbol or self.active_symbol).upper()
        if target_sym not in SUPPORTED_SYMBOLS and target_sym != "ALL":
            target_sym = self.active_symbol
            
        prefix_sym = f"[{target_sym}] " if target_sym != "ALL" else "[SYSTEM] "
        
        # Consola del servidor
        print(f"[{timestamp_str}] [{level}] {prefix_sym}{message}", flush=True)
        
        log_entry = {
            "timestamp": timestamp_str,
            "epoch": epoch_now,
            "level": level,
            "symbol": target_sym,
            "message": message
        }
        
        cutoff = epoch_now - 86400
        with self.lock:
            self.log_history.append(log_entry)
            while self.log_history and self.log_history[0].get("epoch", 0) < cutoff:
                self.log_history.pop(0)
                
            targets = [target_sym] if target_sym in self.symbol_logs else list(self.symbol_logs.keys())
            for s_sym in targets:
                self.symbol_logs[s_sym].append(log_entry)
                while self.symbol_logs[s_sym] and self.symbol_logs[s_sym][0].get("epoch", 0) < cutoff:
                    self.symbol_logs[s_sym].pop(0)
                    
            listeners_copy = list(self.log_listeners)
            
        for listener in listeners_copy:
            try:
                listener(log_entry)
            except Exception:
                pass

        try:
            with open(self.logs_file_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(log_entry) + "\n")
        except Exception:
            pass

    def add_log_listener(self, callback: Callable[[Dict[str, Any]], None]):
        with self.lock:
            self.log_listeners.append(callback)

    def remove_log_listener(self, callback: Callable[[Dict[str, Any]], None]):
        with self.lock:
            if callback in self.log_listeners:
                self.log_listeners.remove(callback)

    # ==========================================
    # INITIALIZATION & CONTRACT DISCOVERY
    # ==========================================
    def initialize(self):
        """Autentica y resuelve contratos para todos los símbolos registrados."""
        self.log("🚀 Inicializando Gateway Cuantitativo Axiom (Modo VWAP Puro 24/7)...", "SYSTEM", "ALL")
        self.client.ensure_authenticated()
        
        acc = self.client.get_practice_account()
        self.account_id = acc["id"]
        self.account_name = acc["name"]
        self.log(f"✅ Cuenta vinculada: {self.account_name} (ID: {self.account_id})", "SYSTEM", "ALL")
        
        for sym in SUPPORTED_SYMBOLS.keys():
            try:
                c = self.client.resolve_contract(sym)
                self.symbol_states[sym]["contract_id"] = c["id"]
                self.symbol_states[sym]["contract_name"] = c["name"]
                status_txt = "ACTIVO" if self.symbol_states[sym]["enabled"] else "INACTIVO"
                self.log(f"Contrato listo: {c['name']} (ID: {c['id']}) | Trading: {status_txt}", "SYSTEM", sym)
            except Exception as e:
                self.log(f"⚠️ No se pudo resolver contrato {sym}: {e}", "ERROR", sym)
                
        try:
            self.sync_real_data_from_topstep()
        except Exception as e:
            self.log(f"⚠️ Alerta en sincronización inicial Topstep: {e}", "ERROR", "ALL")
            self.load_operations_history()

    def toggle_symbol(self, symbol_key: str, enabled: Optional[bool] = None) -> bool:
        """Activa o desactiva las operaciones autónomas para un símbolo específico."""
        sym = symbol_key.upper()
        if sym not in self.symbol_states:
            raise ValueError(f"Símbolo no soportado: {sym}")
            
        with self.lock:
            current = self.symbol_states[sym]["enabled"]
            new_state = (not current) if enabled is None else bool(enabled)
            self.symbol_states[sym]["enabled"] = new_state
            if new_state:
                self.symbol_states[sym]["latest_candle_time"] = None
                self.symbol_states[sym]["last_poll_time"] = 0.0
            
        estado_str = "ACTIVO (Operando)" if new_state else "INACTIVO (Solo Monitoreo)"
        self.log(f"⚙️ Estado de trading actualizado: {estado_str}", "SYSTEM", sym)
        return new_state

    def switch_active_view(self, symbol_key: str):
        """Cambia el símbolo que se muestra en el gráfico."""
        sym = symbol_key.upper()
        if sym not in SUPPORTED_SYMBOLS:
            raise ValueError(f"Símbolo no soportado: {sym}")
        with self.lock:
            self.active_symbol = sym
            if sym in self.symbol_states:
                self.symbol_states[sym]["last_poll_time"] = 0.0
        self.log(f"🖥️ Vista de gráfico cambiada a: {sym}", "SYSTEM", sym)

    # ==========================================
    # MATHEMATICAL ENGINE: SESSION VWAP & BANDS
    # ==========================================
    @staticmethod
    def calcular_vwap_sesion(df: pd.DataFrame) -> pd.DataFrame:
        """
        Cálculo institucional de VWAP de Sesión con bandas de desviación estándar (+/-1 STD).
        La sesión oficial de futuros CME inicia/resetea cada día a las 18:00 ET (New York).
        Calcula el VWAP de la sesión completa desde su inicio acumulando velas de 5 minutos.
        """
        if df is None or df.empty:
            return df
            
        df = df.copy()
        if df['Timestamp'].dt.tz is None:
            df['NY_Time'] = df['Timestamp'].dt.tz_localize('UTC').dt.tz_convert('America/New_York')
        else:
            df['NY_Time'] = df['Timestamp'].dt.tz_convert('America/New_York')

        df['Session'] = (df['NY_Time'] + pd.Timedelta(hours=6)).dt.date
        
        # Filtrar para conservar estrictamente las velas de la sesión actual de CME (desde 18:00 ET)
        sesion_actual = df['Session'].iloc[-1]
        df_sesion = df[df['Session'] == sesion_actual].copy()
        
        df_sesion['HLC3'] = (df_sesion['High'] + df_sesion['Low'] + df_sesion['Close']) / 3.0
        df_sesion['PxV'] = df_sesion['HLC3'] * df_sesion['Volume']
        df_sesion['Px2xV'] = (df_sesion['HLC3'] ** 2) * df_sesion['Volume']
        
        df_sesion['Cum_Vol'] = df_sesion['Volume'].cumsum()
        df_sesion['Cum_PxV'] = df_sesion['PxV'].cumsum()
        df_sesion['Cum_Px2xV'] = df_sesion['Px2xV'].cumsum()
        
        safe_cum_vol = df_sesion['Cum_Vol'].replace(0, np.nan)
        df_sesion['VWAP'] = np.where(df_sesion['Cum_Vol'] > 0, df_sesion['Cum_PxV'] / safe_cum_vol, df_sesion['HLC3'])
        
        varianza = (df_sesion['Cum_Px2xV'] / safe_cum_vol) - (df_sesion['VWAP'] ** 2)
        df_sesion['Variance'] = np.maximum(varianza.fillna(0), 0)
        df_sesion['Std_Dev'] = np.sqrt(df_sesion['Variance']).fillna(0)
        df_sesion['Upper_Band'] = df_sesion['VWAP'] + df_sesion['Std_Dev']
        df_sesion['Lower_Band'] = df_sesion['VWAP'] - df_sesion['Std_Dev']
        
        return df_sesion.reset_index(drop=True)

    # ==========================================
    # STRATEGY ENGINE: RECHAZO DE MECHA EN VWAP + DINAMISMO BE+2T
    # ==========================================
    def verificar_setup_vwap(self, df_5m: pd.DataFrame, sym: str, current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        NUEVA ESTRATEGIA INSTITUCIONAL (RECHAZO DE MECHA EN VWAP):
        1. Vela t-2 (anterior previa): Completamente del lado del VWAP donde predecimos rechazo.
           - SHORT: Toda la vela t-2 por completo debajo del VWAP (high < vwap).
           - LONG: Toda la vela t-2 por completo arriba del VWAP (low > vwap).
        2. Vela t-1 (vela que finaliza): Finaliza con mecha tocando o pasando el VWAP (solo mecha, no cuerpo).
           - SHORT: high >= vwap, max(open, close) < vwap.
           - LONG: low <= vwap, min(open, close) > vwap.
        3. Vela t (actual recién iniciada): No inició pasando el VWAP.
           - SHORT: open < vwap.
           - LONG: open > vwap.
        4. Stop Loss Inicial: 50% de distancia del VWAP hacia la banda contraria (+músculo de aguante).
        5. Take Profit Inicial: 60% de distancia entre el VWAP y la siguiente banda.
        """
        if df_5m is None or len(df_5m) < 3:
            return None

        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = float(spec.get("tickSize", 0.25))

        bar_t2 = df_5m.iloc[-3]
        bar_t1 = df_5m.iloc[-2]
        curr_bar = df_5m.iloc[-1]

        t2_high = float(bar_t2['High'])
        t2_low = float(bar_t2['Low'])
        t2_open = float(bar_t2['Open'])
        t2_close = float(bar_t2['Close'])
        t2_vwap = float(bar_t2['VWAP'])

        t1_high = float(bar_t1['High'])
        t1_low = float(bar_t1['Low'])
        t1_open = float(bar_t1['Open'])
        t1_close = float(bar_t1['Close'])
        t1_vwap = float(bar_t1['VWAP'])

        curr_open = float(curr_bar['Open'])
        curr_close = float(current_price if current_price is not None else curr_bar['Close'])
        curr_vwap = float(curr_bar['VWAP'])
        curr_upper = float(curr_bar['Upper_Band'])
        curr_lower = float(curr_bar['Lower_Band'])

        if pd.isna(curr_vwap) or pd.isna(curr_upper) or pd.isna(curr_lower):
            return None

        std_dev = (curr_upper - curr_lower) / 2.0
        if std_dev <= 0:
            return None

        setup_type = None

        # -------------------------------------------------------------
        # CONDICIÓN SHORT:
        # t-2 completamente debajo de VWAP
        # t-1 mecha toca o pasa VWAP, pero cuerpo debajo
        # t actual abrió debajo de VWAP
        # -------------------------------------------------------------
        t2_below_vwap = (t2_high < t2_vwap) and (t2_open < t2_vwap) and (t2_close < t2_vwap)
        t1_wick_short = (t1_high >= t1_vwap)
        t1_body_short = max(t1_open, t1_close) < t1_vwap
        t_open_short = (curr_open < curr_vwap)

        if t2_below_vwap and t1_wick_short and t1_body_short and t_open_short:
            setup_type = "SHORT"

        # -------------------------------------------------------------
        # CONDICIÓN LONG:
        # t-2 completamente arriba de VWAP
        # t-1 mecha toca o pasa VWAP, pero cuerpo arriba
        # t actual abrió arriba de VWAP
        # -------------------------------------------------------------
        t2_above_vwap = (t2_low > t2_vwap) and (t2_open > t2_vwap) and (t2_close > t2_vwap)
        t1_wick_long = (t1_low <= t1_vwap)
        t1_body_long = min(t1_open, t1_close) > t1_vwap
        t_open_long = (curr_open > curr_vwap)

        if t2_above_vwap and t1_wick_long and t1_body_long and t_open_long:
            setup_type = "LONG"

        if not setup_type:
            return None

        # -------------------------------------------------------------
        # STOP LOSS INICIAL: 50% DE DISTANCIA DEL VWAP A LA BANDA (+MÚSCULO DE AGUANTE)
        # -------------------------------------------------------------
        d_sl = round_to_tick(SL_VWAP_BAND_RATIO * std_dev, tick_size)
        if setup_type == "SHORT":
            precio_sl = round_to_tick(curr_vwap + d_sl, tick_size)
            if precio_sl <= curr_close:
                precio_sl = round_to_tick(curr_close + (2 * tick_size), tick_size)
            banda_objetivo = curr_lower
        else: # LONG
            precio_sl = round_to_tick(curr_vwap - d_sl, tick_size)
            if precio_sl >= curr_close:
                precio_sl = round_to_tick(curr_close - (2 * tick_size), tick_size)
            banda_objetivo = curr_upper

        # -------------------------------------------------------------
        # TAKE PROFIT INICIAL: 60% DE DISTANCIA VWAP A BANDA
        # -------------------------------------------------------------
        d_tp = round_to_tick(TP_VWAP_BAND_RATIO * std_dev, tick_size)
        if d_tp < 2 * tick_size:
            d_tp = 2 * tick_size

        if setup_type == "SHORT":
            precio_tp = round_to_tick(curr_vwap - d_tp, tick_size)
            # Descartar si el precio ya alcanzó o superó el TP hacia abajo
            if curr_close <= precio_tp:
                return None
            dist_avanzada_tp = max(0.0, curr_vwap - curr_close)
        else: # LONG
            precio_tp = round_to_tick(curr_vwap + d_tp, tick_size)
            # Descartar si el precio ya alcanzó o superó el TP hacia arriba
            if curr_close >= precio_tp:
                return None
            dist_avanzada_tp = max(0.0, curr_close - curr_vwap)

        # -------------------------------------------------------------
        # FILTRO DE ENTRADA TARDE / RECHAZO AGOTADO:
        # 1. Si la vela previa ya consumió más del 50% del TP hacia la banda.
        # 2. Si el TP restante es menor a MIN_TP_TICKS (evita micro-ganancias).
        # 3. Si el ratio R:R (TP restante / SL riesgo) es inferior al mínimo (0.60).
        # -------------------------------------------------------------
        pct_consumido = (dist_avanzada_tp / d_tp) * 100.0 if d_tp > 0 else 100.0
        if pct_consumido > MAX_ENTRY_TP_CONSUMED_PCT:
            return None

        d_tp_restante = abs(precio_tp - curr_close)
        d_sl_riesgo = abs(curr_close - precio_sl)

        tp_ticks = round(d_tp_restante / tick_size)
        sl_ticks = max(2, round(d_sl_riesgo / tick_size))

        if tp_ticks < MIN_TP_TICKS:
            return None

        if d_sl_riesgo > 0 and (d_tp_restante / d_sl_riesgo) < MIN_SETUP_RR_RATIO:
            return None

        d_base = d_tp_restante

        now_utc = datetime.now(timezone.utc)
        condicion_desc = (
            f"VWAP Wick Rejection ({setup_type}) | "
            f"Vela t-2 lado limpio | Vela t-1 mecha VWAP | Vela t open limpio | "
            f"SL {int(SL_VWAP_BAND_RATIO * 100)}% Banda ({precio_sl:.2f} / {sl_ticks}t) | TP {int(TP_VWAP_BAND_RATIO * 100)}% ({precio_tp:.2f} / {tp_ticks}t)"
        )

        return {
            "simbolo": sym,
            "direccion": setup_type,
            "precio_entrada": curr_close,
            "precio_sl": precio_sl,
            "precio_tp": precio_tp,
            "sl_ticks": sl_ticks,
            "tp_ticks": tp_ticks,
            "vwap_cruce": curr_vwap,
            "banda_objetivo": banda_objetivo,
            "upper_band": curr_upper,
            "lower_band": curr_lower,
            "std_dev": std_dev,
            "d_base": d_base,
            "condicion_tecnica": condicion_desc,
            "entry_window": f"{now_utc.strftime('%H:%M:%S')} UTC"
        }

    # ==========================================
    # RISK FILTERS & SCHEDULE
    # ==========================================
    def es_horario_operable(self) -> tuple[bool, str]:
        """
        Evalúa si el mercado CME está activo y es operable según las reglas institucionales:
        1. Cierre de fin de semana: Viernes 17:00 ET a Domingo 18:00 ET (Cerrado).
        2. Pausa diaria de descanso CME: Lunes a Jueves 17:00 a 18:00 ET (Cerrado).
        3. Filtro de 2 horas post-pausa / post-apertura:
           - Lunes a Jueves de 18:00 a 20:00 ET (primeras 2 horas luego del descanso del día -> Pausado).
           - Domingos de 18:00 a 20:00 ET (primeras 2 horas luego de la apertura del domingo -> Pausado).
           - Inicio oficial de operaciones a las 20:00 ET (8:00 PM ET).
        4. Sesión regular activa en el resto de horarios (hasta el cierre diario de las 17:00 ET).
        """
        if self.daily_lock_active:
            return False, f"Candado de Ganancia Diaria activado: {self.daily_lock_reason}"

        try:
            ny_time = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            utc_now = datetime.now(timezone.utc)
            ny_time = utc_now - timedelta(hours=4)
            
        try:
            if getattr(ny_time, "tzinfo", None) is not None:
                peru_time = ny_time.astimezone(ZoneInfo("America/Lima"))
            else:
                peru_time = datetime.now(ZoneInfo("America/Lima"))
        except Exception:
            try:
                peru_time = datetime.now(ZoneInfo("America/Lima"))
            except Exception:
                utc_now = datetime.now(timezone.utc)
                peru_time = utc_now - timedelta(hours=5)

        weekday = ny_time.weekday() # 0=Lunes, 4=Viernes, 5=Sábado, 6=Domingo
        hour = ny_time.hour
        minute = ny_time.minute
        time_str = f"{hour:02d}:{minute:02d} ET"
        peru_time_str = f"{peru_time.hour:02d}:{peru_time.minute:02d} PET"
        
        # 1. Fin de semana
        if weekday == 5: # Sábado completo
            return False, f"Mercado CME cerrado por fin de semana (Sábado - {time_str})"
        if weekday == 6 and hour < 18: # Domingo antes de la apertura oficial de las 18:00 ET
            return False, f"Mercado CME cerrado por fin de semana (Domingo pre-apertura 18:00 ET - {time_str})"
        if weekday == 4 and hour >= 17: # Viernes después del cierre de las 17:00 ET
            return False, f"Mercado CME cerrado por fin de semana (Viernes post-cierre 17:00 ET - {time_str})"
            
        # 2. Pausa diaria de descanso CME (Lunes a Jueves 17:00 a 18:00 ET)
        if weekday in (0, 1, 2, 3) and hour == 17:
            return False, f"Pausa diaria de descanso CME (17:00 a 18:00 ET - {time_str})"

            
        return True, f"Sesión regular activa ({time_str} / {peru_time_str})"

    def get_open_position_size(self, sym: str, cached_positions: Optional[List[Dict[str, Any]]] = None) -> int:
        """Obtiene la cantidad neta de contratos abiertos para el símbolo especificado."""
        state = self.symbol_states.get(sym, {})
        contract_id = state.get("contract_id")
        try:
            positions = cached_positions
            if positions is None and self.account_id:
                positions = self.client.get_open_positions(self.account_id)
            if positions is not None:
                for pos in positions:
                    pos_cid = str(pos.get("contractId", "")).strip().upper()
                    target_cid = str(contract_id).strip().upper()
                    pos_sym = self.resolve_symbol_from_contract(pos_cid)
                    if pos_cid == target_cid or pos_sym == sym:
                        return abs(int(pos.get("size", 0)))
                trade = state.get("active_trade")
                if trade and (time.time() - trade.get("entry_timestamp", 0.0) < 5.0):
                    return 1
                return 0
        except Exception:
            return 1 if state.get("has_open_position", False) else 0
        return 1 if state.get("has_open_position", False) else 0

    def tiene_posicion_abierta(self, sym: str, cached_positions: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Verifica si el símbolo ya tiene al menos una posición abierta activa."""
        return self.get_open_position_size(sym, cached_positions) > 0

    def puede_abrir_posicion(self, sym: str, cached_positions: Optional[List[Dict[str, Any]]] = None) -> tuple[bool, str]:
        """
        Evalúa si se puede abrir una nueva operación en el símbolo:
        - 1 sola posición permitida por símbolo.
        - Se debe respetar obligatoriamente el enfriamiento mínimo de 9 minutos (540s) tras ejecutar una operación.
        """
        cur_size = self.get_open_position_size(sym, cached_positions)
        if cur_size >= 1:
            return False, f"Posición ya abierta en {sym}"

        state = self.symbol_states.get(sym, {})
        if state.get("active_trade"):
            return False, f"Operación en seguimiento activo en {sym}"

        now_ts = time.time()
        cd_until = float(state.get("cooldown_until", 0.0))
        if now_ts < cd_until:
            rem = int(cd_until - now_ts)
            mins = rem // 60
            secs = rem % 60
            return False, f"Enfriamiento obligatorio de 9 min activo ({mins:02d}:{secs:02d} restantes)"

        return True, "Apertura permitida (sin posiciones y fuera de cooldown)."

    # ==========================================
    # ORDER EXECUTION ENGINE
    # ==========================================
    def ejecutar_orden(self, setup: Dict[str, Any], sym: str, ignore_schedule: bool = False):
        """
        Ejecuta la orden a mercado en TopstepX con brackets OCO fijos:
        - SL Inicial: 45% de distancia entre la línea VWAP y la banda contraria (Fijo).
        - TP Inicial: 50% de distancia entre la línea VWAP y la banda objetivo (Fijo).
        - El que alcanza primero cierra automáticamente la operación (brackets OCO nativos).
        """
        if setup.get("simbolo") != sym:
            self.log(f"❌ Discrepancia de símbolo setup={setup.get('simbolo')} vs sym={sym}", "ERROR", sym)
            return

        if not ignore_schedule:
            is_operable, motivo_horario = self.es_horario_operable()
            if not is_operable:
                self.log(f"⏸️ [{sym}] Orden suspendida: {motivo_horario}.", "SETUP", sym)
                return

        can_open, reason_open = self.puede_abrir_posicion(sym)
        if not can_open:
            self.log(f"🛡️ [{sym}] Orden descartada: {reason_open}", "SETUP", sym)
            return
            
        direccion = setup["direccion"]
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        contract_id = self.symbol_states[sym]["contract_id"]
        precio_entrada = round_to_tick(float(setup["precio_entrada"]), tick_size)
        vwap_cruce = round_to_tick(float(setup.get("vwap_cruce", precio_entrada)), tick_size)
        d_base = float(setup.get("d_base", 15 * tick_size))
        
        # SL y TP definidos por el setup (limpio para recibir la nueva ley)
        precio_sl = setup.get("precio_sl")
        precio_tp = setup.get("precio_tp")
        
        if precio_sl is None or precio_tp is None:
            self.log(f"⚠️ [{sym}] Orden suspendida: SL y TP no definidos en setup (en espera de nueva ley).", "SETUP", sym)
            return

        d_tp_ini = abs(precio_tp - precio_entrada)

        if direccion == "LONG":
            side = 0
            ticks_sl_order_val = max(round((precio_entrada - precio_sl) / tick_size), 4)
            ticks_tp = max(round((precio_tp - precio_entrada) / tick_size), 4)
            sl_ticks_order = -ticks_sl_order_val
            tp_ticks_order = ticks_tp
        else: # SHORT
            side = 1
            ticks_sl_order_val = max(round((precio_sl - precio_entrada) / tick_size), 4)
            ticks_tp = max(round((precio_entrada - precio_tp) / tick_size), 4)
            sl_ticks_order = ticks_sl_order_val
            tp_ticks_order = -ticks_tp

        trigger_ticks = setup.get("trigger_ticks", get_vwap_trigger_distance_ticks(sym))
        condicion_str = (
            f"VWAP Reversion. Entrada: {precio_entrada:.2f} | "
            f"VWAP: {vwap_cruce:.2f} | SL ({int(SL_VWAP_BAND_RATIO * 100)}% a Banda): {ticks_sl_order_val}t ({precio_sl:.2f}) | "
            f"TP ({int(TP_VWAP_BAND_RATIO * 100)}% a Banda): {ticks_tp}t ({precio_tp:.2f} | D_ini: {d_tp_ini:.2f} pts)"
        )
        self.log(f"🚨 DISPARANDO {direccion} A MERCADO | {condicion_str}", "ORDER", sym)
        
        try:
            res = self.client.place_order(
                account_id=self.account_id,
                contract_id=contract_id,
                side=side,
                size=1,
                sl_ticks=sl_ticks_order,
                tp_ticks=tp_ticks_order,
                custom_tag=f"axiom-vwap-{sym.lower()}-{int(time.time())}"
            )
            
            if res.get("success"):
                order_id = res.get("orderId", "N/A")
                self.symbol_states[sym]["has_open_position"] = True
                
                now_entry = datetime.now()
                hora_apertura = now_entry.strftime('%H:%M:%S')
                fecha_apertura = now_entry.strftime('%Y-%m-%d')
                
                # Inicializar seguimiento activo
                self.symbol_states[sym]["active_trade"] = {
                    "symbol": sym,
                    "contract_id": contract_id,
                    "direction": direccion,
                    "entry_price": precio_entrada,
                    "vwap_cruce": vwap_cruce,
                    "banda_objetivo": setup.get("banda_objetivo"),
                    "current_price": precio_entrada,
                    "highest_price": precio_entrada,
                    "lowest_price": precio_entrada,
                    "initial_sl_price": precio_sl,
                    "current_sl_price": precio_sl,
                    "initial_tp_price": precio_tp,
                    "current_tp_price": precio_tp,
                    "d_tp_ini": d_tp_ini,
                    "base_tp_pts": d_tp_ini,
                    "initial_sl_ticks": ticks_sl_order_val,
                    "tp_ticks": ticks_tp,
                    "order_id": order_id,
                    "sl_stage": 0,
                    "max_pct_avance": 0.0,
                    "tp_max_alcanzado_str": "0.00 pts (0.0%)",
                    "sl_max_alcanzado_str": "0.00 pts",
                    "entry_timestamp": time.time(),
                    "entry_time_str": hora_apertura,
                    "entry_date_str": fecha_apertura,
                    "last_check_time": time.time()
                }
                
                ticks_sl_vwap = round(abs(precio_sl - vwap_cruce) / tick_size)
                self.log(f"🚀 [{sym}] Orden EJECUTADA | Apertura: {hora_apertura} | {direccion} @ {precio_entrada:.2f} | Order ID: {order_id} | SL: {ticks_sl_order_val}t ({precio_sl:.2f}) | TP: {ticks_tp}t ({precio_tp:.2f})", "ORDER", sym)
                self.registrar_en_csv(
                    simbolo=sym,
                    direccion=direccion,
                    precio=precio_entrada,
                    ticks_sl=ticks_sl_order_val,
                    ticks_tp=ticks_tp,
                    condicion=condicion_str,
                    exito=True,
                    mensaje_servidor="Orden colocada con éxito en TopstepX",
                    error_code="0",
                    order_id=order_id,
                    vwap=vwap_cruce,
                    sl_price=precio_sl,
                    tp_price=precio_tp,
                    banda=setup.get("banda_objetivo")
                )
                self.symbol_states[sym]["stats"]["executed"] += 1
                # Enfriamiento obligatorio de 9 minutos tras abrir operación en este símbolo
                self.symbol_states[sym]["cooldown_until"] = time.time() + SYMBOL_COOLDOWN_SECONDS
            else:
                error_msg = res.get('errorMessage') or res.get('message') or res.get('error') or 'Rechazo de API'
                error_code = str(res.get('errorCode') or res.get('code') or 'REJECTED')
                self.symbol_states[sym]["has_open_position"] = False
                self.log(f"❌ Orden RECHAZADA por TopstepX en {sym}: {error_msg} (Código: {error_code})", "ERROR", sym)
                self.registrar_en_csv(
                    simbolo=sym,
                    direccion=direccion,
                    precio=precio_entrada,
                    ticks_sl=ticks_sl_order_val,
                    ticks_tp=ticks_tp,
                    condicion=condicion_str,
                    exito=False,
                    mensaje_servidor=error_msg,
                    error_code=error_code,
                    order_id="N/A",
                    vwap=vwap_cruce,
                    sl_price=precio_sl,
                    tp_price=precio_tp,
                    banda=setup.get("banda_objetivo")
                )
                self.symbol_states[sym]["stats"]["rejected"] += 1
                
        except Exception as e:
            self.symbol_states[sym]["has_open_position"] = False
            self.log(f"❌ Excepción crítica al colocar orden en {sym}: {e}", "ERROR", sym)
            self.registrar_en_csv(
                simbolo=sym,
                direccion=direccion,
                precio=precio_entrada,
                ticks_sl=ticks_sl_order_val,
                ticks_tp=ticks_tp,
                condicion=condicion_str,
                exito=False,
                mensaje_servidor=str(e),
                error_code="EXCEPTION",
                order_id="N/A"
            )

    # ==========================================
    # FAST 2-SECOND POSITION TRACKER & TRAILING LADDER
    # ==========================================
    def monitorear_posicion_abierta(self, sym: str, current_price: Optional[float] = None, df_5m: Optional[pd.DataFrame] = None, cached_positions: Optional[List[Dict[str, Any]]] = None):
        """
        Monitorea la posición cada 2 segundos y ejecuta la escalera dinámica de hitos:
        - Hito 1 (Avance >= 40% de D_base): Mueve SL a Entrada + 1 tick (Break-Even protegido).
        - Hito 2 (Avance >= 70% de D_base): Mueve SL al 45% de D_base y sube TP al 140% de D_base.
        - Hito 3 (Avance >= 120% de D_base): Mueve SL al 108% de D_base y sube TP al 170% de D_base.
        - Hito 4 (Avance >= 135% de D_base): Mueve SL al 128% de D_base y sube TP al 200% de D_base.
        - Fail-Safe Guard: Cierre forzado si el precio perfora el SL protegido.
        """
        state = self.symbol_states.get(sym, {})
        trade = state.get("active_trade")
        
        if not trade:
            trade = self.adoptar_posicion_abierta(sym)
            if not trade:
                return

        now = time.time()
        trade["last_check_time"] = now

        # Verificar si la posición sigue activa en TopstepX (utilizando consulta en bloque)
        is_open = self.tiene_posicion_abierta(sym, cached_positions=cached_positions)
        if not is_open:
            self.cancelar_ordenes_abiertas_contrato(trade["contract_id"], sym)
            self.registrar_cierre_posicion(sym, trade, motivo_cierre="Posición concluida en TopstepX")
            state["active_trade"] = None
            state["has_open_position"] = False
            state["cooldown_until"] = max(float(state.get("cooldown_until", 0.0)), time.time() + 60.0)
            return

        precio_actual = current_price
        if precio_actual is None and df_5m is not None and not df_5m.empty:
            precio_actual = float(df_5m.iloc[-1]['Close'])
        elif precio_actual is None and state.get("telemetry", {}).get("close"):
            precio_actual = float(state["telemetry"]["close"])

        if precio_actual is None:
            return

        trade["current_price"] = precio_actual
        direccion = trade["direction"]
        precio_entrada = trade["entry_price"]
        precio_tp = trade["current_tp_price"]
        sl_actual = trade["current_sl_price"]
        sl_stage = trade.get("sl_stage", 0)
        d_base = trade["base_tp_pts"]
        
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)

        # Rastrear precio más favorable alcanzado estrictamente DESPUÉS de la entrada (sin wicks del pasado)
        d_tp_ini = float(trade.get("d_tp_ini", abs(trade.get("initial_tp_price", trade.get("current_tp_price", precio_entrada)) - precio_entrada)))
        if d_tp_ini <= 0:
            d_tp_ini = 10 * tick_size

        if direccion == "LONG":
            highest_since_entry = max(trade.get("highest_price", precio_entrada), precio_actual)
            lowest_since_entry = min(trade.get("lowest_price", precio_entrada), precio_actual)
            trade["highest_price"] = highest_since_entry
            trade["lowest_price"] = lowest_since_entry
            
            ganancia_pts = highest_since_entry - precio_entrada
            drawdown_pts = precio_entrada - lowest_since_entry
            pct_avance = (ganancia_pts / d_tp_ini) * 100.0 if d_tp_ini > 0 else 0.0
            
            trade["tp_max_alcanzado_str"] = f"+{ganancia_pts:.2f} pts ({pct_avance:.1f}%)"
            trade["sl_max_alcanzado_str"] = f"-{drawdown_pts:.2f} pts"
        else: # SHORT
            lowest_since_entry = min(trade.get("lowest_price", precio_entrada), precio_actual)
            highest_since_entry = max(trade.get("highest_price", precio_entrada), precio_actual)
            trade["lowest_price"] = lowest_since_entry
            trade["highest_price"] = highest_since_entry
            
            ganancia_pts = precio_entrada - lowest_since_entry
            drawdown_pts = highest_since_entry - precio_entrada
            pct_avance = (ganancia_pts / d_tp_ini) * 100.0 if d_tp_ini > 0 else 0.0
            
            trade["tp_max_alcanzado_str"] = f"+{ganancia_pts:.2f} pts ({pct_avance:.1f}%)"
            trade["sl_max_alcanzado_str"] = f"-{drawdown_pts:.2f} pts"

        max_pct = max(trade.get("max_pct_avance", 0.0), pct_avance)
        trade["max_pct_avance"] = max_pct

        ganancia_ticks = round(ganancia_pts / tick_size) if tick_size > 0 else 0
        stage_str = f" [{self.format_milestone(sl_stage)}]" if sl_stage > 0 else " [Inicial]"

        self.log(
            f"🎯 [{sym}] {direccion}{stage_str} | In: {precio_entrada:.2f} | Actual: {precio_actual:.2f} "
            f"({ganancia_ticks:+d}t | {pct_avance:.1f}% TP_ini | Max: {max_pct:.1f}%) | SL: {sl_actual:.2f} | TP: {precio_tp:.2f}",
            "TELEMETRY", sym
        )

        contract_id = trade["contract_id"]
        order_id = str(trade.get("order_id", "N/A"))

        # ==========================================
        # DINAMISMO DE SL A PROTECCIÓN VWAP + 2 TICKS A FAVOR:
        # Cuando el precio avance a favor pasando el 50% de ganancia hacia el TP:
        # El SL debe posicionarse a nuestro favor al precio VWAP + 2 ticks (o Entrada según config).
        # ==========================================
        if not trade.get("be_plus_2_active", False) and pct_avance >= DYNAMIC_BE_PROFIT_PCT:
            vwap_ref = trade.get("vwap_cruce", precio_entrada)
            base_ref = vwap_ref if DYNAMIC_BE_REFERENCE == "VWAP" else precio_entrada
            ref_name = "VWAP" if DYNAMIC_BE_REFERENCE == "VWAP" else "Entrada"

            if direccion == "LONG":
                nuevo_sl = round_to_tick(base_ref + (DYNAMIC_BE_PROFIT_TICKS * tick_size), tick_size)
                debe_mover = (nuevo_sl > sl_actual)
            else: # SHORT
                nuevo_sl = round_to_tick(base_ref - (DYNAMIC_BE_PROFIT_TICKS * tick_size), tick_size)
                debe_mover = (nuevo_sl < sl_actual)

            if debe_mover:
                self.log(
                    f"🛡️ [{sym}] ¡DINAMISMO ACTIVADO! Avance: {pct_avance:.1f}% (>= {DYNAMIC_BE_PROFIT_PCT}%) | "
                    f"Asegurando SL a favor ({ref_name} {'+' if direccion=='LONG' else '-'} {DYNAMIC_BE_PROFIT_TICKS}t = {nuevo_sl:.2f})...",
                    "ORDER", sym
                )
                exito_mov = self.mover_sl_en_topstep(contract_id, nuevo_sl, sym)
                if exito_mov:
                    trade["current_sl_price"] = nuevo_sl
                    trade["be_plus_2_active"] = True
                    trade["sl_stage"] = 1.0
                    self.registrar_evento_be_en_csv(
                        sym=sym,
                        trade=trade,
                        nuevo_sl=nuevo_sl,
                        pct_avance=pct_avance,
                        motivo=f"Dinamismo {DYNAMIC_BE_PROFIT_PCT}% TP -> {ref_name} {'+' if direccion=='LONG' else '-'} {DYNAMIC_BE_PROFIT_TICKS}t asegurado"
                    )

        # SALVAGUARDA DE CIERRE AUTOMÁTICO (SL / TP Fijo)
        # El que alcanza primero cierra automáticamente la operación (Fail-Safe por Software)
        sl_fijo = trade.get("current_sl_price")
        tp_fijo = trade.get("current_tp_price")
        motivo_alcance = None

        if direccion == "LONG":
            if sl_fijo is not None and precio_actual <= sl_fijo:
                motivo_alcance = f"Stop Loss alcanzado (Fijo {sl_fijo:.2f})"
            elif tp_fijo is not None and precio_actual >= tp_fijo:
                motivo_alcance = f"Take Profit alcanzado (Fijo {tp_fijo:.2f})"
        else: # SHORT
            if sl_fijo is not None and precio_actual >= sl_fijo:
                motivo_alcance = f"Stop Loss alcanzado (Fijo {sl_fijo:.2f})"
            elif tp_fijo is not None and precio_actual <= tp_fijo:
                motivo_alcance = f"Take Profit alcanzado (Fijo {tp_fijo:.2f})"

        if motivo_alcance:
            self.log(f"⚡ [{sym}] {motivo_alcance} (Precio actual: {precio_actual:.2f}). Liquidando y asegurando cierre...", "ORDER", sym)
            try:
                self.client.close_contract_position(self.account_id, trade["contract_id"])
            except Exception as ce:
                self.log(f"⚠️ Aviso al liquidar posición en {sym}: {ce}", "WARNING", sym)
            self.cancelar_ordenes_abiertas_contrato(trade["contract_id"], sym)
            self.registrar_cierre_posicion(sym, trade, precio_salida=precio_actual, motivo_cierre=motivo_alcance)
            state["active_trade"] = None
            state["has_open_position"] = False
            state["cooldown_until"] = max(float(state.get("cooldown_until", 0.0)), time.time() + 60.0)
            return

    def ajustar_bracket_en_topstep(self, contract_id: str, nuevo_sl: Optional[float], nuevo_tp: Optional[float], sym: str, current_price: Optional[float] = None) -> bool:
        """
        Modifica de forma atómica y en una sola consulta de órdenes abiertas el SL y/o TP en TopstepX.
        Utiliza el método oficial Swagger (/api/Order/modify):
        - Stop Order (Tipo 4): stopPrice
        - Limit Order (Tipo 1): limitPrice
        - Evita modificaciones redundantes si la orden ya está en el precio objetivo.
        """
        if not self.account_id or (nuevo_sl is None and nuevo_tp is None):
            return False
            
        success = True
        orders_found = 0
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)

        try:
            open_orders = self.client.search_open_orders(self.account_id)
            for o in open_orders:
                cid = str(o.get("contractId", "")).strip().upper()
                target_cid = str(contract_id).strip().upper()
                order_sym = self.resolve_symbol_from_contract(cid)
                otype = int(o.get("type", 0))
                order_id = o.get("id")
                size = o.get("size", 1)
                
                if cid == target_cid or order_sym == sym:
                    # Type 4 = Stop Order (SL)
                    if otype == 4 and nuevo_sl is not None:
                        orders_found += 1
                        curr_stop = o.get("stopPrice")
                        target_sl = round_to_tick(nuevo_sl, tick_size)

                        # Salvaguarda final contra precios de stop inválidos en TopstepX / CME
                        last_p = current_price or self.symbol_states.get(sym, {}).get("telemetry", {}).get("close")
                        order_side = int(o.get("side", -1))
                        if last_p is not None and float(last_p) > 0:
                            p_float = float(last_p)
                            if order_side == 1 and target_sl >= p_float: # Sell Stop
                                target_sl = round_to_tick(p_float - tick_size, tick_size)
                            elif order_side == 0 and target_sl <= p_float: # Buy Stop
                                target_sl = round_to_tick(p_float + tick_size, tick_size)

                        if curr_stop is not None and abs(float(curr_stop) - float(target_sl)) < (0.01 * tick_size):
                            continue # Ya está en el nivel deseado

                        res = self.client.modify_order(
                            account_id=self.account_id,
                            order_id=order_id,
                            stop_price=target_sl,
                            size=size
                        )
                        if res.get("success"):
                            self.log(f"✅ [{sym}] Orden Stop #{order_id} ajustada en TopstepX a {target_sl:.2f}.", "ORDER", sym)
                        else:
                            success = False
                            err_msg = res.get('errorMessage') or f"Código {res.get('errorCode')}"
                            self.log(f"⚠️ [{sym}] Error al ajustar Stop #{order_id}: {err_msg}", "ERROR", sym)
                            
                    # Type 1 = Limit Order (TP)
                    elif otype == 1 and nuevo_tp is not None:
                        orders_found += 1
                        curr_limit = o.get("limitPrice")
                        if curr_limit is not None and abs(float(curr_limit) - float(nuevo_tp)) < (0.01 * tick_size):
                            continue # Ya está en el nivel deseado

                        target_tp = round_to_tick(nuevo_tp, tick_size)
                        res = self.client.modify_order(
                            account_id=self.account_id,
                            order_id=order_id,
                            limit_price=target_tp,
                            size=size
                        )
                        if res.get("success"):
                            self.log(f"✅ [{sym}] Orden Limit TP #{order_id} ampliada en TopstepX a {target_tp:.2f}.", "ORDER", sym)
                        else:
                            success = False
                            err_msg = res.get('errorMessage') or f"Código {res.get('errorCode')}"
                            self.log(f"⚠️ [{sym}] Error al ampliar TP #{order_id}: {err_msg}", "ERROR", sym)

            if orders_found == 0:
                # No había órdenes en TopstepX para modificar, retornar True para que el fail-safe en software proteja el trade
                return True
        except Exception as e:
            self.log(f"⚠️ Error al ajustar brackets en TopstepX: {e}", "ERROR", sym)
            return False
            
        return success

    def mover_sl_en_topstep(self, contract_id: str, nuevo_sl: float, sym: str) -> bool:
        """Compatibilidad: Modifica la orden Stop abierta en TopstepX con el nuevo nivel de stopPrice."""
        return self.ajustar_bracket_en_topstep(contract_id, nuevo_sl=nuevo_sl, nuevo_tp=None, sym=sym)

    def mover_tp_en_topstep(self, contract_id: str, nuevo_tp: float, sym: str) -> bool:
        """Compatibilidad: Modifica la orden Limit (Take Profit) abierta en TopstepX con el nuevo limitPrice."""
        return self.ajustar_bracket_en_topstep(contract_id, nuevo_sl=None, nuevo_tp=nuevo_tp, sym=sym)

    def cancelar_ordenes_abiertas_contrato(self, contract_id: str, sym: str):
        """Cancela todas las órdenes abiertas residuales asociadas al contrato para evitar órdenes huérfanas."""
        if not self.account_id:
            return
        try:
            open_orders = self.client.search_open_orders(self.account_id)
            for o in open_orders:
                if str(o.get("contractId", "")) == str(contract_id):
                    oid = o.get("id")
                    try:
                        self.client.cancel_order(self.account_id, oid)
                        self.log(f"🧹 [{sym}] Orden residual #{oid} cancelada en TopstepX.", "SYSTEM", sym)
                    except Exception as ce:
                        self.log(f"⚠️ No se pudo cancelar orden #{oid}: {ce}", "ERROR", sym)
        except Exception as e:
            self.log(f"⚠️ Error buscando órdenes para cancelar en {sym}: {e}", "ERROR", sym)

    def adoptar_posicion_abierta(self, sym: str) -> Optional[Dict[str, Any]]:
        """Adopta cualquier posición abierta existente en TopstepX para el símbolo dado."""
        if not self.account_id:
            return None
            
        state = self.symbol_states.get(sym, {})
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        
        try:
            positions = self.client.get_open_positions(self.account_id)
            target_pos = None
            for p in positions:
                cid = str(p.get("contractId", ""))
                psym = self.resolve_symbol_from_contract(cid)
                psize = abs(int(p.get("size", 0)))
                if psize > 0 and (psym == sym or cid == state.get("contract_id")):
                    target_pos = p
                    break
                    
            if not target_pos:
                state["has_open_position"] = False
                return None
                
            contract_id = target_pos.get("contractId")
            entry_price = float(target_pos.get("averagePrice", 0.0))
            raw_size = int(target_pos.get("size", 0))
            pos_type = int(target_pos.get("type", 0))
            
            if pos_type == 1:
                direccion = "LONG"
            elif pos_type == 2:
                direccion = "SHORT"
            else:
                direccion = "LONG" if raw_size > 0 else "SHORT"
            
            # Buscar órdenes Stop y Limit asociadas
            open_orders = self.client.search_open_orders(self.account_id)
            sl_order = None
            tp_order = None
            
            for o in open_orders:
                if str(o.get("contractId", "")) == str(contract_id):
                    otype = int(o.get("type", 0))
                    if otype == 4:
                        sl_order = o
                    elif otype == 1:
                        tp_order = o
                        
            if sl_order and sl_order.get("stopPrice") is not None:
                current_sl_price = float(sl_order["stopPrice"])
            else:
                sl_fallback = get_default_sl_ticks(sym)
                current_sl_price = (entry_price - sl_fallback * tick_size) if direccion == "LONG" else (entry_price + sl_fallback * tick_size)
                
            if tp_order and tp_order.get("limitPrice") is not None:
                tp_price = float(tp_order["limitPrice"])
            else:
                tp_price = (entry_price + 50 * tick_size) if direccion == "LONG" else (entry_price - 50 * tick_size)
                
            recorrido_tp_pts = abs(tp_price - entry_price)
            tp_ticks = round(recorrido_tp_pts / tick_size) if tick_size > 0 else 0
            sl_ticks = round(abs(entry_price - current_sl_price) / tick_size) if tick_size > 0 else 0
            
            now_dt = datetime.now()
            hora_str = now_dt.strftime('%H:%M:%S')
            fecha_str = now_dt.strftime('%Y-%m-%d')
            
            adopted_trade = {
                "symbol": sym,
                "contract_id": contract_id,
                "direction": direccion,
                "entry_price": entry_price,
                "current_price": entry_price,
                "highest_price": entry_price,
                "lowest_price": entry_price,
                "initial_sl_price": current_sl_price,
                "current_sl_price": current_sl_price,
                "initial_tp_price": tp_price,
                "current_tp_price": tp_price,
                "base_tp_pts": recorrido_tp_pts,
                "initial_sl_ticks": sl_ticks,
                "tp_ticks": tp_ticks,
                "order_id": target_pos.get("id", "ADOPTADO"),
                "sl_stage": 0,
                "max_pct_avance": 0.0,
                "entry_timestamp": time.time(),
                "entry_time_str": hora_str,
                "entry_date_str": fecha_str,
                "last_check_time": time.time()
            }
            
            state["active_trade"] = adopted_trade
            state["has_open_position"] = True
            state["contract_id"] = contract_id
            
            self.log(f"🎯 [{sym}] Posición abierta adoptada en TopstepX | Apertura: {hora_str} | {direccion} @ {entry_price:.2f}.", "ORDER", sym)
            
            # Registrar posición adoptada en CSV para visibilidad inmediata en panel
            self._guardar_o_actualizar_en_csv({
                'Fecha': fecha_str,
                'Hora_Apertura': hora_str,
                'Hora_Cierre': "EN CURSO",
                'Hora': hora_str,
                'Simbolo': sym,
                'Estado': "EN CURSO (OCO)",
                'Direccion': direccion,
                'Precio_Mercado': f"{entry_price:.2f}",
                'Precio_Entrada_Real': f"{entry_price:.2f}",
                'Precio_Salida_Real': "PENDIENTE",
                'SL_Ticks': str(sl_ticks),
                'TP_Ticks': str(tp_ticks),
                'Hito_Alcanzado': "Inicial",
                'Max_Pct_TP': "0.0%",
                'PnL_Bruto_USD': "0.00",
                'Comisiones_USD': "0.00",
                'PnL_Neto_USD': "0.00",
                'Order_ID': str(target_pos.get("id", "ADOPTADO")),
                'Exit_Order_ID': "PENDIENTE",
                'Motivo_Rechazo': "N/A",
                'Codigo_Error': "0",
                'Condicion_Tecnica': "Posición adoptada en vivo desde TopstepX",
                'Ultima_Actualizacion': now_dt.strftime('%Y-%m-%d %H:%M:%S')
            }, match_order_id=str(target_pos.get("id", "ADOPTADO")))
            return adopted_trade
        except Exception as e:
            self.log(f"⚠️ Error al adoptar posición en {sym}: {e}", "ERROR", sym)
            return None

    # ==========================================
    # CSV AUDITING & TOPSTEP RECONCILIATION
    # ==========================================
    def _guardar_o_actualizar_en_csv(self, row_data: Dict[str, Any], match_order_id: Optional[str] = None):
        """
        Inserta una nueva fila o actualiza en su lugar la existente (por Order_ID)
        para evitar duplicar registros abiertos cuando se actualizan hitos o se cierran operaciones.
        Preserva de manera inmutable la hora de apertura original.
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.csv_path)), exist_ok=True)
            fieldnames = [
                'Fecha', 'Hora_Apertura', 'Hora_Cierre', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                'Precio_Entrada_Real', 'Precio_Salida_Real', 'VWAP_Entrada', 'Banda_Objetivo',
                'SL_Inicial', 'SL_Max_Alcanzado', 'TP_Inicial', 'TP_Max_Alcanzado',
                'SL_Ticks', 'TP_Ticks',
                'Hito_Alcanzado', 'Max_Pct_TP',
                'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
            ]
            
            rows = []
            if os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                with open(self.csv_path, mode='r', encoding='utf-8') as file:
                    reader = csv.DictReader(file, delimiter=';')
                    for r in reader:
                        # Auto-poblar Hora_Apertura y Hora_Cierre si faltan de versiones anteriores
                        if not r.get('Hora_Apertura') or r.get('Hora_Apertura') in ('N/A', ''):
                            r['Hora_Apertura'] = r.get('Hora', 'N/A')
                        if not r.get('Hora_Cierre') or r.get('Hora_Cierre') in ('N/A', ''):
                            estado = (r.get('Estado') or '').upper()
                            if 'CERRADA' in estado:
                                r['Hora_Cierre'] = r.get('Ultima_Actualizacion', '').split(' ')[1] if ' ' in r.get('Ultima_Actualizacion', '') else r.get('Hora', 'N/A')
                            else:
                                r['Hora_Cierre'] = 'EN CURSO'
                        rows.append(r)
            
            updated = False
            target_oid = str(match_order_id).strip() if match_order_id and str(match_order_id) not in ["N/A", ""] else None
            
            if target_oid:
                for i, r in enumerate(rows):
                    r_oid = str(r.get("Order_ID", "")).strip()
                    if r_oid == target_oid:
                        for k, v in row_data.items():
                            if v is not None and v != "":
                                # Proteger Hora_Apertura y Hora para no sobreescribir la entrada original al cerrar
                                if k in ('Hora_Apertura', 'Hora') and r.get(k) and r.get(k) not in ('N/A', ''):
                                    continue
                                r[k] = v
                        rows[i] = r
                        updated = True
                        break
            
            if not updated:
                complete_row = {fn: row_data.get(fn, "N/A") for fn in fieldnames}
                rows.append(complete_row)
            
            with open(self.csv_path, mode='w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=';')
                writer.writeheader()
                for r in rows:
                    writer.writerow({fn: r.get(fn, "N/A") for fn in fieldnames})
                    
        except Exception as e:
            self.log(f"❌ Error al guardar/actualizar CSV: {e}", "ERROR")

    def registrar_cierre_posicion(self, sym: str, trade: Dict[str, Any], precio_salida: Optional[float] = None, motivo_cierre: str = "Cierre de mercado"):
        """Registra el cierre definitivo de una operación con cálculo exacto de PnL y estado institucional."""
        try:
            now_dt = datetime.now()
            fecha_actual = now_dt.strftime('%Y-%m-%d')
            hora_actual = now_dt.strftime('%H:%M:%S')
            
            precio_entrada = float(trade.get("entry_price", 0.0))
            direccion = trade.get("direction", "LONG")
            order_id = str(trade.get("order_id", "N/A"))
            hora_apertura = trade.get("entry_time_str") or trade.get("Hora_Apertura") or hora_actual
            stage = trade.get("sl_stage", 0)
            hito_desc = self.format_milestone(stage)
            max_pct = trade.get("max_pct_avance", 0.0)
            contract_id = str(trade.get("contract_id", ""))
            
            p_salida = precio_salida
            gross_pnl = None
            comms = None
            exit_order_id = "MKT_EXIT"
            
            if self.account_id:
                try:
                    recent_trades = self.client.search_trades(self.account_id, days_back=1)
                    if recent_trades:
                        for fill in reversed(recent_trades):
                            f_cid = str(fill.get("contractId", ""))
                            if f_cid == contract_id and fill.get("profitAndLoss") is not None:
                                p_salida = float(fill.get("price", p_salida or precio_entrada))
                                gross_pnl = float(fill.get("profitAndLoss", 0.0))
                                f_fees = float(fill.get("fees") or 0.0)
                                f_comms = float(fill.get("commissions") or 0.0)
                                comms = round(f_fees + f_comms, 2)
                                exit_order_id = str(fill.get("orderId", "MKT_EXIT"))
                                break
                except Exception as te:
                    self.log(f"⚠️ Error consultando fills recientes para {sym}: {te}", "ERROR", sym)
                    
            if p_salida is None:
                p_salida = float(trade.get("current_price", precio_entrada))

            spec = SUPPORTED_SYMBOLS.get(sym, {"pointValue": 50.0})
            pt_val = spec.get("pointValue", 50.0)
            
            if gross_pnl is None:
                diff_pts = (p_salida - precio_entrada) if direccion == "LONG" else (precio_entrada - p_salida)
                gross_pnl = round(diff_pts * pt_val, 2)
                
            if comms is None:
                comms = 1.22 if sym.startswith("M") else 3.78
                
            net_pnl = round(gross_pnl - comms, 2)
            sign_str = "+" if net_pnl >= 0 else ""
            estado_cierre = f"CERRADA ({sign_str}${net_pnl:.2f} USD)"

            if "Posición concluida en TopstepX" in motivo_cierre:
                if gross_pnl is not None and gross_pnl > 0:
                    motivo_cierre = "Take Profit alcanzado en TopstepX (Fijo)"
                elif gross_pnl is not None and gross_pnl < 0:
                    motivo_cierre = "Stop Loss alcanzado en TopstepX (Fijo)"
                elif gross_pnl == 0:
                    motivo_cierre = "Break-Even alcanzado en TopstepX"

            if USE_DYNAMIC_TRAILING:
                # Asegurar que max_pct y stage reflejen al menos el recorrido favorable de salida
                d_base = float(trade.get("base_tp_pts", 0.0))
                if p_salida is not None and precio_entrada > 0:
                    diff_pts = (p_salida - precio_entrada) if direccion == "LONG" else (precio_entrada - p_salida)
                    if d_base > 0 and diff_pts > 0:
                        exit_pct = (diff_pts / d_base) * 100.0
                        max_pct = max(max_pct, exit_pct)
                        if max_pct >= HITO_1_TRIGGER_PCT and stage < 1:
                            stage = 1
                        if max_pct >= HITO_2_TRIGGER_PCT and stage < 2:
                            stage = 2
                        if max_pct >= HITO_3_TRIGGER_PCT and stage < 3:
                            stage = 3
                        if max_pct >= HITO_4_TRIGGER_PCT:
                            runner_tier = 4 + int((max_pct - HITO_4_TRIGGER_PCT) // RUNNER_STEP_PCT)
                            if runner_tier > stage:
                                stage = runner_tier
                hito_desc = self.format_milestone(stage)
            else:
                if gross_pnl is not None and gross_pnl > 0:
                    hito_desc = "Take Profit (Fijo)"
                elif gross_pnl is not None and gross_pnl < 0:
                    hito_desc = "Stop Loss (Fijo)"
                else:
                    hito_desc = "Break-Even (Fijo)"
            
            self.log(
                f"🏁 [{sym}] Operación CERRADA ({direccion}) | Apertura: {hora_apertura} | Cierre: {hora_actual} | In: {precio_entrada:.2f} | Out: {p_salida:.2f} | "
                f"PnL: {sign_str}${net_pnl:.2f} USD | Hito final: {hito_desc} (Max: {max_pct:.1f}%) | Motivo: {motivo_cierre}",
                "ORDER", sym
            )
            
            row_data = {
                'Fecha': fecha_actual,
                'Hora_Apertura': hora_apertura,
                'Hora_Cierre': hora_actual,
                'Hora': hora_apertura,
                'Simbolo': sym,
                'Estado': estado_cierre,
                'Direccion': direccion,
                'Precio_Mercado': f"{p_salida:.2f}",
                'Precio_Entrada_Real': f"{precio_entrada:.2f}",
                'Precio_Salida_Real': f"{p_salida:.2f}",
                'VWAP_Entrada': f"{float(trade.get('vwap_cruce', 0.0)):.2f}" if trade.get('vwap_cruce') else "N/A",
                'Banda_Objetivo': f"{float(trade.get('banda_objetivo', 0.0)):.2f}" if trade.get('banda_objetivo') else "N/A",
                'SL_Inicial': f"{float(trade.get('initial_sl_price', 0.0)):.2f}" if trade.get('initial_sl_price') else "N/A",
                'SL_Max_Alcanzado': trade.get('sl_max_alcanzado_str') or trade.get('SL_Max_Alcanzado') or "0.00 pts",
                'TP_Inicial': f"{float(trade.get('initial_tp_price', 0.0)):.2f}" if trade.get('initial_tp_price') else "N/A",
                'TP_Max_Alcanzado': trade.get('tp_max_alcanzado_str') or trade.get('TP_Max_Alcanzado') or f"{max_pct:.1f}%",
                'SL_Ticks': str(trade.get("initial_sl_ticks", 25)),
                'TP_Ticks': str(trade.get("tp_ticks", "Dinámico")),
                'Hito_Alcanzado': hito_desc,
                'Max_Pct_TP': f"{max_pct:.1f}%",
                'PnL_Bruto_USD': f"{gross_pnl:.2f}",
                'Comisiones_USD': f"{comms:.2f}",
                'PnL_Neto_USD': f"{net_pnl:.2f}",
                'Order_ID': order_id,
                'Exit_Order_ID': exit_order_id,
                'Motivo_Rechazo': "N/A",
                'Codigo_Error': "0",
                'Condicion_Tecnica': f"{motivo_cierre} | Recorrido Max: {max_pct:.1f}%",
                'Ultima_Actualizacion': f"{fecha_actual} {hora_actual}"
            }
            self._guardar_o_actualizar_en_csv(row_data, match_order_id=order_id)
            
            with self.lock:
                self.stats["total_trades"] = max(self.stats.get("total_trades", 0), 1)
                if net_pnl >= 0:
                    self.stats["winning_trades"] = self.stats.get("winning_trades", 0) + 1
                else:
                    self.stats["losing_trades"] = self.stats.get("losing_trades", 0) + 1
                self.stats["gross_pnl_usd"] = round(self.stats.get("gross_pnl_usd", 0.0) + gross_pnl, 2)
                self.stats["total_fees_usd"] = round(self.stats.get("total_fees_usd", 0.0) + comms, 2)
                self.stats["net_pnl_usd"] = round(self.stats.get("net_pnl_usd", 0.0) + net_pnl, 2)
                
                tot = self.stats["winning_trades"] + self.stats["losing_trades"]
                if tot > 0:
                    self.stats["win_rate"] = round((self.stats["winning_trades"] / tot) * 100.0, 1)
                    
        except Exception as e:
            self.log(f"❌ Error al registrar cierre de posición en {sym}: {e}", "ERROR", sym)

    def registrar_evento_be_en_csv(self, sym: str, trade: Dict[str, Any], nuevo_sl: float, pct_avance: float, motivo: str):
        """Actualiza el avance de hitos en el archivo CSV institucional."""
        try:
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            stage = trade.get("sl_stage", 1)
            hito_desc = self.format_milestone(stage)
            order_id = str(trade.get("order_id", "N/A"))
            
            row_data = {
                'Simbolo': sym,
                'Estado': f"EN CURSO ({hito_desc})",
                'Precio_Mercado': f"{trade.get('current_price', nuevo_sl):.2f}",
                'Hito_Alcanzado': hito_desc,
                'Max_Pct_TP': f"{pct_avance:.1f}%",
                'Condicion_Tecnica': motivo,
                'Ultima_Actualizacion': f"{fecha_actual} {hora_actual}"
            }
            self._guardar_o_actualizar_en_csv(row_data, match_order_id=order_id)
        except Exception as e:
            self.log(f"❌ Error al escribir hito en CSV: {e}", "ERROR", sym)

    def registrar_en_csv(self, simbolo, direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id, vwap=None, sl_price=None, tp_price=None, banda=None):
        """Guarda la orden emitida en el archivo CSV institucional."""
        try:
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            
            if exito:
                estado = "EN CURSO (OCO)"
                motivo = "Aceptada"
                cod_err = "0"
                hito_alcanzado = "Inicial"
                max_pct_tp = "0.0%"
            else:
                motivo = str(mensaje_servidor) if mensaje_servidor else "Rechazada por TopstepX"
                cod_err = str(error_code) if error_code else "N/A"
                estado = f"RECHAZADA: {motivo}"
                hito_alcanzado = "N/A"
                max_pct_tp = "N/A"
            
            row_data = {
                'Fecha': fecha_actual,
                'Hora_Apertura': hora_actual,
                'Hora_Cierre': "EN CURSO" if exito else "RECHAZADA",
                'Hora': hora_actual,
                'Simbolo': simbolo,
                'Estado': estado,
                'Direccion': direccion,
                'Precio_Mercado': f"{precio:.2f}",
                'Precio_Entrada_Real': f"{precio:.2f}" if exito else "N/A",
                'Precio_Salida_Real': "PENDIENTE" if exito else "N/A",
                'VWAP_Entrada': f"{float(vwap):.2f}" if vwap else "N/A",
                'Banda_Objetivo': f"{float(banda):.2f}" if banda else "N/A",
                'SL_Inicial': f"{float(sl_price):.2f}" if sl_price else "N/A",
                'SL_Max_Alcanzado': "0.00 pts",
                'TP_Inicial': f"{float(tp_price):.2f}" if tp_price else "N/A",
                'TP_Max_Alcanzado': "0.00 pts (0.0%)",
                'SL_Ticks': str(ticks_sl),
                'TP_Ticks': str(ticks_tp),
                'Hito_Alcanzado': hito_alcanzado,
                'Max_Pct_TP': max_pct_tp,
                'PnL_Bruto_USD': "0.00",
                'Comisiones_USD': "0.00",
                'PnL_Neto_USD': "0.00",
                'Order_ID': str(order_id),
                'Exit_Order_ID': "PENDIENTE" if exito else "N/A",
                'Motivo_Rechazo': motivo if not exito else "N/A",
                'Codigo_Error': cod_err if not exito else "N/A",
                'Condicion_Tecnica': condicion,
                'Ultima_Actualizacion': f"{fecha_actual} {hora_actual}"
            }
            self._guardar_o_actualizar_en_csv(row_data, match_order_id=str(order_id) if exito else None)
        except Exception as e:
            self.log(f"❌ Error al escribir en CSV: {e}", "ERROR", simbolo)

    def resolve_symbol_from_contract(self, contract_id: str) -> str:
        """Determina el símbolo estándar a partir del contractId de TopstepX."""
        c = str(contract_id).upper()
        for sym, state in self.symbol_states.items():
            if state.get("contract_id") and str(state["contract_id"]).upper() == c:
                return sym
                
        prefix_map = [
            (".EP.", "ES"), (".MES.", "MES"), (".ENQ.", "NQ"), (".MNQ.", "MNQ"),
            (".YM.", "YM"), (".MYM.", "MYM"), (".RTY.", "RTY"), (".M2K.", "M2K"),
            (".GCE.", "GC"), (".MGC.", "MGC"), (".CLE.", "CL"), (".MCLE.", "MCL"),
            ("MCLE", "MCL"), ("CLE", "CL"), ("MGC", "MGC"), ("GCE", "GC"),
            ("M2K", "M2K"), ("RTY", "RTY"), ("MYM", "MYM"), ("YM", "YM"),
            ("MNQ", "MNQ"), ("ENQ", "NQ"), ("MES", "MES"), ("EP", "ES"), ("MCL", "MCL")
        ]
        for pfx, s in prefix_map:
            if pfx in c:
                return s
        return "ES"

    def _recalcular_stats_desde_csv(self):
        """Recalcula las métricas estadísticas globales a partir de las operaciones del CSV."""
        if not os.path.isfile(self.csv_path):
            return
        try:
            total = 0
            executed = 0
            rejected = 0
            longs = 0
            shorts = 0
            winning = 0
            losing = 0
            gross_pnl = 0.0
            gross_wins = 0.0
            gross_losses = 0.0
            fees = 0.0
            net_pnl = 0.0
            
            with open(self.csv_path, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter=';')
                for row in reader:
                    total += 1
                    estado = (row.get("Estado") or "").upper()
                    direc = (row.get("Direccion") or "").upper()
                    
                    if "LONG" in direc:
                        longs += 1
                    elif "SHORT" in direc:
                        shorts += 1
                        
                    if "RECHAZADA" in estado:
                        rejected += 1
                    else:
                        executed += 1
                        
                    if "CERRADA" in estado:
                        try:
                            net = safe_float(row.get("PnL_Neto_USD"), 0.0)
                            gross = safe_float(row.get("PnL_Bruto_USD"), 0.0)
                            comms = safe_float(row.get("Comisiones_USD"), 0.0)
                            gross_pnl += gross
                            fees += comms
                            net_pnl += net
                            if gross > 0:
                                winning += 1
                                gross_wins += gross
                            elif gross < 0:
                                losing += 1
                                gross_losses += abs(gross)
                            else:
                                if net >= 0:
                                    winning += 1
                                else:
                                    losing += 1
                        except (ValueError, TypeError):
                            pass
                            
            closed_total = winning + losing
            win_rate = round((winning / closed_total * 100.0), 1) if closed_total > 0 else 0.0
            if gross_losses > 0:
                profit_factor = round(gross_wins / gross_losses, 2)
            elif gross_wins > 0:
                profit_factor = 99.9
            else:
                profit_factor = 0.0
            
            with self.lock:
                self.stats["total_trades"] = total
                self.stats["executed_trades"] = executed
                self.stats["rejected_trades"] = rejected
                self.stats["longs"] = longs
                self.stats["shorts"] = shorts
                if not self.stats.get("sync_api_authoritative", False):
                    self.stats["winning_trades"] = winning
                    self.stats["losing_trades"] = losing
                    self.stats["gross_pnl_usd"] = round(gross_pnl, 2)
                    self.stats["total_fees_usd"] = round(fees, 2)
                    self.stats["net_pnl_usd"] = round(net_pnl, 2)
                    self.stats["win_rate"] = win_rate
                    self.stats["profit_factor"] = profit_factor
        except Exception as e:
            self.log(f"⚠️ Error al recalcular métricas desde CSV: {e}", "ERROR")

    def sync_real_data_from_topstep(self) -> Dict[str, Any]:
        """
        Sincroniza balance real y audita las operaciones en CSV contrastándolas con TopstepX:
        - Solo mantiene como 'EN CURSO' las posiciones que efectivamente existan en TopstepX.
        - Si una orden figura 'EN CURSO' o 'SL PROTEGIDO' pero ya no está abierta en TopstepX, la concilia y marca como 'CERRADA'.
        """
        if not self.account_id:
            return self.stats
            
        try:
            balance = self.client.get_account_balance(self.account_id)
            self.stats["real_balance"] = balance
            
            open_positions = self.client.get_open_positions(self.account_id)
            raw_trades = self.client.search_trades(self.account_id, days_back=7)
            
            open_contract_ids = {str(p.get("contractId", "")).strip() for p in open_positions if abs(int(p.get("size", 0))) > 0}
            open_symbols = {self.resolve_symbol_from_contract(cid) for cid in open_contract_ids}
            
            fieldnames = [
                'Fecha', 'Hora_Apertura', 'Hora_Cierre', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                'Precio_Entrada_Real', 'Precio_Salida_Real', 'VWAP_Entrada', 'Banda_Objetivo',
                'SL_Inicial', 'SL_Max_Alcanzado', 'TP_Inicial', 'TP_Max_Alcanzado',
                'SL_Ticks', 'TP_Ticks',
                'Hito_Alcanzado', 'Max_Pct_TP',
                'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
            ]

            # Detección de Cambio de Cuenta / Reset:
            # Si el account_id cambió (ej. de 28966620 a 29034856 tras reset en TopstepX) y la nueva cuenta está en $150k con 0 trades,
            # archivamos el CSV de la cuenta anterior para que la nueva cuenta inicie en 0 de forma limpia.
            account_marker_file = os.path.join(os.path.dirname(os.path.abspath(self.csv_path)), ".current_account_id")
            previous_acc_id = None
            if os.path.isfile(account_marker_file):
                try:
                    with open(account_marker_file, "r") as af:
                        previous_acc_id = af.read().strip()
                except Exception:
                    pass

            current_acc_id_str = str(self.account_id)
            account_changed = (previous_acc_id is not None) and (previous_acc_id != current_acc_id_str)

            if account_changed and not raw_trades and balance >= 150000.0:
                if os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                    backup_name = os.path.join(
                        os.path.dirname(os.path.abspath(self.csv_path)),
                        f"registro_operaciones_cuenta_{previous_acc_id}_{int(time.time())}.csv"
                    )
                    try:
                        import shutil
                        shutil.copy2(self.csv_path, backup_name)
                        with open(self.csv_path, mode='w', newline='', encoding='utf-8') as file:
                            writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=';')
                            writer.writeheader()
                        self.log(f"🧹 [RESET] Cambio de cuenta detectado ({previous_acc_id} -> {current_acc_id_str}). Historial anterior respaldado en {os.path.basename(backup_name)}.", "SYSTEM")
                    except Exception as ce:
                        self.log(f"⚠️ Error archivando CSV anterior: {ce}", "ERROR")

            try:
                with open(account_marker_file, "w") as af:
                    af.write(current_acc_id_str)
            except Exception:
                pass

            closed_fills = [t for t in raw_trades if t.get("profitAndLoss") is not None]

            if os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                rows = []
                with open(self.csv_path, mode='r', encoding='utf-8') as file:
                    reader = csv.DictReader(file, delimiter=';')
                    for r in reader:
                        # Auto-completar Hora_Apertura y Hora_Cierre si faltan
                        if not r.get('Hora_Apertura') or r.get('Hora_Apertura') in ('N/A', ''):
                            r['Hora_Apertura'] = r.get('Hora', 'N/A')
                        if not r.get('Hora_Cierre') or r.get('Hora_Cierre') in ('N/A', ''):
                            estado = (r.get('Estado') or '').upper()
                            if 'CERRADA' in estado:
                                r['Hora_Cierre'] = r.get('Ultima_Actualizacion', '').split(' ')[1] if ' ' in r.get('Ultima_Actualizacion', '') else r.get('Hora', 'N/A')
                            else:
                                r['Hora_Cierre'] = 'EN CURSO'
                        rows.append(r)
                        
                cambios = False
                matched_exit_order_ids = set()

                for r in rows:
                    estado_actual = (r.get("Estado") or "").upper()
                    exit_oid_existing = str(r.get("Exit_Order_ID") or "").strip()
                    if exit_oid_existing and exit_oid_existing not in ("N/A", "SYNC_CLOSE", ""):
                        matched_exit_order_ids.add(exit_oid_existing)

                    if "EN CURSO" in estado_actual or "SL PROTEGIDO" in estado_actual or "ABIERTA" in estado_actual:
                        sym = (r.get("Simbolo") or "ES").upper()
                        if sym not in open_symbols:
                            p_salida = None
                            gross_pnl = None
                            comms = None
                            exit_oid = "SYNC_CLOSE"
                            exit_time_str = datetime.now().strftime('%H:%M:%S')

                            for fill in reversed(closed_fills):
                                f_oid = str(fill.get("orderId", ""))
                                if f_oid in matched_exit_order_ids:
                                    continue
                                f_sym = self.resolve_symbol_from_contract(fill.get("contractId", ""))
                                if f_sym == sym:
                                    p_salida = float(fill.get("price", 0.0))
                                    gross_pnl = float(fill.get("profitAndLoss", 0.0))
                                    f_fees = float(fill.get("fees") or 0.0) + float(fill.get("commissions") or 0.0)
                                    comms = round(f_fees * 2.0, 2)
                                    exit_oid = f_oid
                                    matched_exit_order_ids.add(f_oid)
                                    fill_ts = fill.get("creationTimestamp", "")
                                    if fill_ts and "T" in fill_ts:
                                        try:
                                            exit_time_str = fill_ts.split("T")[1].split(".")[0]
                                        except Exception:
                                            pass
                                    break
                                    
                            p_entrada = safe_float(r.get("Precio_Entrada_Real"), safe_float(r.get("Precio_Mercado"), 0.0))
                            if p_salida is None or p_salida == 0.0:
                                p_salida = p_entrada
                                
                            if gross_pnl is None:
                                spec = SUPPORTED_SYMBOLS.get(sym, {"pointValue": 50.0})
                                pt_val = spec.get("pointValue", 50.0)
                                diff_pts = (p_salida - p_entrada) if (r.get("Direccion") or "LONG").upper() == "LONG" else (p_entrada - p_salida)
                                gross_pnl = round(diff_pts * pt_val, 2)
                                
                            if comms is None:
                                comms = 1.92 if sym.startswith("M") else 4.32
                                
                            net_pnl = round(gross_pnl - comms, 2)
                            sign = "+" if net_pnl >= 0 else ""
                            
                            if gross_pnl > 0:
                                r["Hito_Alcanzado"] = "Take Profit (Fijo)"
                            elif gross_pnl < 0:
                                r["Hito_Alcanzado"] = "Stop Loss (Fijo)"
                            else:
                                r["Hito_Alcanzado"] = "Break-Even (Fijo)"
                            
                            r["Estado"] = f"CERRADA ({sign}${net_pnl:.2f} USD)"
                            r["Precio_Salida_Real"] = f"{p_salida:.2f}"
                            r["PnL_Bruto_USD"] = f"{gross_pnl:.2f}"
                            r["Comisiones_USD"] = f"{comms:.2f}"
                            r["PnL_Neto_USD"] = f"{net_pnl:.2f}"
                            r["Exit_Order_ID"] = exit_oid
                            r["Hora_Cierre"] = exit_time_str
                            if not r.get("Hora_Apertura") or r.get("Hora_Apertura") in ("N/A", ""):
                                r["Hora_Apertura"] = r.get("Hora", "N/A")
                            r["Ultima_Actualizacion"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            cambios = True

                # Auditoría inversa: Asegurar que todo fill de cierre oficial de Topstep esté presente en el CSV
                for fill in closed_fills:
                    f_oid = str(fill.get("orderId", "")).strip()
                    if f_oid and f_oid not in matched_exit_order_ids:
                        f_sym = self.resolve_symbol_from_contract(fill.get("contractId", ""))
                        f_gross = float(fill.get("profitAndLoss", 0.0))
                        f_fee_single = float(fill.get("fees") or 0.0) + float(fill.get("commissions") or 0.0)
                        f_comms = round(f_fee_single * 2.0, 2)
                        f_net = round(f_gross - f_comms, 2)
                        f_price = float(fill.get("price", 0.0))
                        f_side = int(fill.get("side", 0))
                        f_dir = "SHORT" if f_side == 0 else "LONG"
                        ts_str = fill.get("creationTimestamp", "")
                        f_date = ts_str.split("T")[0] if "T" in ts_str else datetime.now().strftime('%Y-%m-%d')
                        f_time = ts_str.split("T")[1].split(".")[0] if "T" in ts_str else datetime.now().strftime('%H:%M:%S')
                        sign = "+" if f_net >= 0 else ""
                        
                        new_row = {
                            'Fecha': f_date,
                            'Hora_Apertura': f_time,
                            'Hora_Cierre': f_time,
                            'Hora': f_time,
                            'Simbolo': f_sym,
                            'Estado': f"CERRADA ({sign}${f_net:.2f} USD)",
                            'Direccion': f_dir,
                            'Precio_Mercado': f"{f_price:.2f}",
                            'Precio_Entrada_Real': f"{f_price:.2f}",
                            'Precio_Salida_Real': f"{f_price:.2f}",
                            'VWAP_Entrada': "N/A",
                            'Banda_Objetivo': "N/A",
                            'SL_Inicial': "N/A",
                            'SL_Max_Alcanzado': "0.00 pts",
                            'TP_Inicial': "N/A",
                            'TP_Max_Alcanzado': "0.00 pts (0.0%)",
                            'SL_Ticks': "0",
                            'TP_Ticks': "0",
                            'Hito_Alcanzado': "Take Profit (Fijo)" if f_gross > 0 else ("Stop Loss (Fijo)" if f_gross < 0 else "Break-Even"),
                            'Max_Pct_TP': "100.0%" if f_gross > 0 else "0.0%",
                            'PnL_Bruto_USD': f"{f_gross:.2f}",
                            'Comisiones_USD': f"{f_comms:.2f}",
                            'PnL_Neto_USD': f"{f_net:.2f}",
                            'Order_ID': f_oid,
                            'Exit_Order_ID': f_oid,
                            'Motivo_Rechazo': "N/A",
                            'Codigo_Error': "0",
                            'Condicion_Tecnica': "TopstepX Operación Sincronizada",
                            'Ultima_Actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        }
                        rows.append(new_row)
                        matched_exit_order_ids.add(f_oid)
                        cambios = True

                if cambios:
                    with open(self.csv_path, mode='w', newline='', encoding='utf-8') as file:
                        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=';')
                        writer.writeheader()
                        for r in rows:
                            writer.writerow({fn: r.get(fn, "N/A") for fn in fieldnames})

            # Recalcular métricas de trades
            self._recalcular_stats_desde_csv()
            
            # Sincronización autoritativa directa de PnL y comisiones reales desde TopstepX API
            if raw_trades:
                topstep_winning = 0
                topstep_losing = 0
                topstep_breakeven = 0
                topstep_gross = 0.0
                topstep_fees = 0.0
                gross_wins = 0.0
                gross_losses = 0.0
                by_sym_stats = {}

                for t in raw_trades:
                    cid = t.get("contractId", "")
                    t_sym = self.resolve_symbol_from_contract(cid) or "OTHER"
                    if t_sym not in by_sym_stats:
                        by_sym_stats[t_sym] = {
                            "total": 0, "gross": 0.0, "fees": 0.0, "net_pnl": 0.0,
                            "winning": 0, "losing": 0, "breakeven": 0,
                            "gross_wins": 0.0, "gross_losses": 0.0,
                            "win_rate": 0.0, "profit_factor": 0.0
                        }
                    f_fee = float(t.get("fees") or 0.0) + float(t.get("commissions") or 0.0)
                    topstep_fees += f_fee
                    by_sym_stats[t_sym]["fees"] += f_fee

                for t in raw_trades:
                    pnl = t.get("profitAndLoss")
                    if pnl is not None:
                        val = float(pnl)
                        topstep_gross += val
                        cid = t.get("contractId", "")
                        t_sym = self.resolve_symbol_from_contract(cid) or "OTHER"
                        by_sym_stats[t_sym]["gross"] += val
                        by_sym_stats[t_sym]["total"] += 1
                        if val > 0:
                            topstep_winning += 1
                            gross_wins += val
                            by_sym_stats[t_sym]["winning"] += 1
                            by_sym_stats[t_sym]["gross_wins"] += val
                        elif val < 0:
                            topstep_losing += 1
                            gross_losses += abs(val)
                            by_sym_stats[t_sym]["losing"] += 1
                            by_sym_stats[t_sym]["gross_losses"] += abs(val)
                        else:
                            topstep_breakeven += 1
                            by_sym_stats[t_sym]["breakeven"] += 1

                for s_sym, s_data in by_sym_stats.items():
                    s_data["fees"] = round(s_data["fees"], 2)
                    s_data["gross"] = round(s_data["gross"], 2)
                    s_data["net_pnl"] = round(s_data["gross"] - s_data["fees"], 2)
                    s_tot = s_data["total"]
                    s_data["win_rate"] = round((s_data["winning"] / s_tot * 100.0), 1) if s_tot > 0 else 0.0
                    if s_data["gross_losses"] > 0:
                        s_data["profit_factor"] = round(s_data["gross_wins"] / s_data["gross_losses"], 2)
                    elif s_data["gross_wins"] > 0:
                        s_data["profit_factor"] = 99.9
                    else:
                        s_data["profit_factor"] = 0.0

                topstep_net = round(topstep_gross - topstep_fees, 2)
                topstep_closed = topstep_winning + topstep_losing + topstep_breakeven
                topstep_win_rate = round((topstep_winning / topstep_closed * 100.0), 1) if topstep_closed > 0 else 0.0
                if gross_losses > 0:
                    topstep_profit_factor = round(gross_wins / gross_losses, 2)
                elif gross_wins > 0:
                    topstep_profit_factor = 99.9
                else:
                    topstep_profit_factor = 0.0

                with self.lock:
                    self.stats["sync_api_authoritative"] = True
                    self.stats["gross_pnl_usd"] = round(topstep_gross, 2)
                    self.stats["total_fees_usd"] = round(topstep_fees, 2)
                    self.stats["net_pnl_usd"] = topstep_net
                    self.stats["winning_trades"] = topstep_winning
                    self.stats["losing_trades"] = topstep_losing
                    self.stats["total_trades"] = topstep_closed
                    self.stats["executed_trades"] = topstep_closed
                    self.stats["win_rate"] = topstep_win_rate
                    self.stats["profit_factor"] = topstep_profit_factor
                    self.stats["by_symbol"] = by_sym_stats
            elif balance > 0:
                with self.lock:
                    self.stats["sync_api_authoritative"] = True
                    self.stats["total_trades"] = 0
                    self.stats["executed_trades"] = 0
                    self.stats["rejected_trades"] = 0
                    self.stats["longs"] = 0
                    self.stats["shorts"] = 0
                    self.stats["gross_pnl_usd"] = 0.0
                    self.stats["total_fees_usd"] = 0.0
                    self.stats["net_pnl_usd"] = 0.0
                    self.stats["winning_trades"] = 0
                    self.stats["losing_trades"] = 0
                    self.stats["win_rate"] = 0.0
                    self.stats["profit_factor"] = 0.0
                            
            now_dt = datetime.now()
            self.last_sync_time = time.time()
            self.stats["last_sync"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            self.stats["next_sync"] = (now_dt + timedelta(seconds=self.sync_interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            
            self.log(
                f"🔄 [SYNC] Sincronización oficial TopstepX completada. Balance: ${self.stats['real_balance']:,.2f} | "
                f"PnL Bruto: ${self.stats['gross_pnl_usd']:+,.2f} | Comisiones: ${self.stats['total_fees_usd']:,.2f} | "
                f"PnL Neto Real: ${self.stats['net_pnl_usd']:+,.2f} USD",
                "SYSTEM"
            )
            return self.stats
        except Exception as e:
            self.log(f"⚠️ Error al sincronizar con TopstepX: {e}", "ERROR")
            return self.stats

    def load_operations_history(self) -> List[Dict[str, Any]]:
        """Lee el historial completo de operaciones auditadas desde el archivo CSV."""
        if not os.path.isfile(self.csv_path):
            return []
        records = []
        try:
            with open(self.csv_path, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter=';')
                for row in reader:
                    # Garantizar compatibilidad con Hora_Apertura y Hora_Cierre
                    if not row.get('Hora_Apertura') or row.get('Hora_Apertura') in ('N/A', ''):
                        row['Hora_Apertura'] = row.get('Hora', 'N/A')
                    if not row.get('Hora_Cierre') or row.get('Hora_Cierre') in ('N/A', ''):
                        estado = (row.get('Estado') or '').upper()
                        if 'CERRADA' in estado:
                            row['Hora_Cierre'] = row.get('Ultima_Actualizacion', '').split(' ')[1] if ' ' in row.get('Ultima_Actualizacion', '') else row.get('Hora', 'N/A')
                        else:
                            row['Hora_Cierre'] = 'EN CURSO'
                    records.append(row)
        except Exception as e:
            self.log(f"⚠️ Error al leer operaciones de CSV: {e}", "ERROR")
        return records

    def get_symbols_ranking_and_stats(self) -> Dict[str, Any]:
        """
        Calcula el ranking cuantitativo consolidado de operaciones agrupadas por símbolo:
        - Ordenado de mayor a menor según PnL Neto (Ranking de rendimiento por activo).
        - Totales por símbolo: Operaciones, Ganadoras, Perdedoras, Win Rate %, PnL Bruto, Comisiones, PnL Neto, Profit Factor.
        - Detalle de cada operación con su TP máximo alcanzado, SL máximo alcanzado, hora de apertura, hora de cierre, precios de entrada y salida.
        - Gran Total Consolidado de la cuenta.
        """
        records = self.load_operations_history()
        
        # Inicializar estructura con todos los símbolos CME soportados
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for sym_key, spec in SUPPORTED_SYMBOLS.items():
            by_symbol[sym_key] = {
                "symbol": sym_key,
                "name": spec.get("name", sym_key),
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "breakeven_trades": 0,
                "win_rate": 0.0,
                "gross_pnl": 0.0,
                "commissions": 0.0,
                "net_pnl": 0.0,
                "profit_factor": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "trades": []
            }

        # Procesar registros históricos del CSV
        for r in records:
            sym = str(r.get("Simbolo") or "ES").upper()
            if sym not in by_symbol:
                by_symbol[sym] = {
                    "symbol": sym,
                    "name": SUPPORTED_SYMBOLS.get(sym, {}).get("name", sym),
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "breakeven_trades": 0,
                    "win_rate": 0.0,
                    "gross_pnl": 0.0,
                    "commissions": 0.0,
                    "net_pnl": 0.0,
                    "profit_factor": 0.0,
                    "best_trade": 0.0,
                    "worst_trade": 0.0,
                    "trades": []
                }
            
            estado = str(r.get("Estado") or "").upper()
            if "RECHAZADA" in estado:
                continue

            try:
                gross = float(r.get("PnL_Bruto_USD") or 0.0)
            except Exception:
                gross = 0.0
            try:
                comms = float(r.get("Comisiones_USD") or 0.0)
            except Exception:
                comms = 0.0
            try:
                net = float(r.get("PnL_Neto_USD") or 0.0)
            except Exception:
                net = round(gross - comms, 2)

            is_closed = "CERRADA" in estado
            is_open = "EN CURSO" in estado or "ABIERTA" in estado

            hora_apertura = r.get("Hora_Apertura") or r.get("Hora") or "--:--:--"
            hora_cierre = r.get("Hora_Cierre") or ("EN CURSO" if is_open else "--:--:--")

            p_in = r.get("Precio_Entrada_Real") or r.get("Precio_Mercado") or "--"
            p_out = r.get("Precio_Salida_Real") or ("PENDIENTE" if is_open else "--")

            tp_max = r.get("TP_Max_Alcanzado") or r.get("Max_Pct_TP") or "0.0%"
            sl_max = r.get("SL_Max_Alcanzado") or "0.00 pts"

            trade_entry = {
                "fecha": r.get("Fecha") or "--",
                "hora_apertura": hora_apertura,
                "hora_cierre": hora_cierre,
                "simbolo": sym,
                "direccion": r.get("Direccion") or "--",
                "estado": r.get("Estado") or "--",
                "precio_entrada": p_in,
                "precio_salida": p_out,
                "vwap_entrada": r.get("VWAP_Entrada") or "--",
                "sl_inicial": r.get("SL_Inicial") or "--",
                "sl_max_alcanzado": sl_max,
                "tp_inicial": r.get("TP_Inicial") or "--",
                "tp_max_alcanzado": tp_max,
                "pnl_bruto": gross,
                "comisiones": comms,
                "pnl_neto": net,
                "order_id": r.get("Order_ID") or "--",
                "condicion": r.get("Condicion_Tecnica") or "--"
            }

            s_data = by_symbol[sym]
            s_data["trades"].append(trade_entry)

            if is_closed:
                s_data["total_trades"] += 1
                s_data["gross_pnl"] = round(s_data["gross_pnl"] + gross, 2)
                s_data["commissions"] = round(s_data["commissions"] + comms, 2)
                s_data["net_pnl"] = round(s_data["net_pnl"] + net, 2)

                if net > 0:
                    s_data["winning_trades"] += 1
                elif net < 0:
                    s_data["losing_trades"] += 1
                else:
                    s_data["breakeven_trades"] += 1

                if s_data["total_trades"] == 1:
                    s_data["best_trade"] = net
                    s_data["worst_trade"] = net
                else:
                    s_data["best_trade"] = max(s_data["best_trade"], net)
                    s_data["worst_trade"] = min(s_data["worst_trade"], net)

        # Incluir trades activos en memoria si no están todavía en el CSV
        for sym_k, st in self.symbol_states.items():
            act = st.get("active_trade")
            if act:
                act_oid = str(act.get("order_id", "LIVE"))
                s_obj = by_symbol.get(sym_k)
                if s_obj and not any(t.get("order_id") == act_oid for t in s_obj["trades"]):
                    p_ent = act.get("entry_price", 0.0)
                    s_obj["trades"].insert(0, {
                        "fecha": act.get("entry_date_str") or datetime.now().strftime("%Y-%m-%d"),
                        "hora_apertura": act.get("entry_time_str") or datetime.now().strftime("%H:%M:%S"),
                        "hora_cierre": "EN CURSO",
                        "simbolo": sym_k,
                        "direccion": act.get("direction", "LONG"),
                        "estado": "EN CURSO (OCO)",
                        "precio_entrada": f"{p_ent:.2f}",
                        "precio_salida": "PENDIENTE",
                        "vwap_entrada": f"{float(act.get('vwap_cruce', 0.0)):.2f}" if act.get("vwap_cruce") else "--",
                        "sl_inicial": f"{float(act.get('initial_sl_price', 0.0)):.2f}" if act.get("initial_sl_price") else "--",
                        "sl_max_alcanzado": act.get("sl_max_alcanzado_str", "0.00 pts"),
                        "tp_inicial": f"{float(act.get('initial_tp_price', 0.0)):.2f}" if act.get("initial_tp_price") else "--",
                        "tp_max_alcanzado": act.get("tp_max_alcanzado_str", "0.00 pts (0.0%)"),
                        "pnl_bruto": 0.0,
                        "comisiones": 0.0,
                        "pnl_neto": 0.0,
                        "order_id": act_oid,
                        "condicion": "Operación activa en curso"
                    })

        ranked_list = []
        grand_total = {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "breakeven_trades": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "commissions": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0
        }

        total_gross_wins = 0.0
        total_gross_losses = 0.0

        for sym_k, data in by_symbol.items():
            tot = data["total_trades"]
            wins = data["winning_trades"]
            data["win_rate"] = round((wins / tot * 100.0), 1) if tot > 0 else 0.0

            gross_wins = sum(t["pnl_bruto"] for t in data["trades"] if t["pnl_bruto"] > 0)
            gross_losses = abs(sum(t["pnl_bruto"] for t in data["trades"] if t["pnl_bruto"] < 0))
            if gross_losses > 0:
                data["profit_factor"] = round(gross_wins / gross_losses, 2)
            elif gross_wins > 0:
                data["profit_factor"] = 99.9
            else:
                data["profit_factor"] = 0.0

            grand_total["total_trades"] += tot
            grand_total["winning_trades"] += wins
            grand_total["losing_trades"] += data["losing_trades"]
            grand_total["breakeven_trades"] += data["breakeven_trades"]
            grand_total["gross_pnl"] = round(grand_total["gross_pnl"] + data["gross_pnl"], 2)
            grand_total["commissions"] = round(grand_total["commissions"] + data["commissions"], 2)
            grand_total["net_pnl"] = round(grand_total["net_pnl"] + data["net_pnl"], 2)

            total_gross_wins += gross_wins
            total_gross_losses += gross_losses

            ranked_list.append(data)

        # Ordenar: primero los símbolos con operaciones por PnL Neto descendente
        ranked_list.sort(key=lambda x: (x["total_trades"] > 0, x["net_pnl"], x["win_rate"]), reverse=True)

        for idx, item in enumerate(ranked_list, start=1):
            item["rank"] = idx

        g_tot = grand_total["total_trades"]
        grand_total["win_rate"] = round((grand_total["winning_trades"] / g_tot * 100.0), 1) if g_tot > 0 else 0.0
        if total_gross_losses > 0:
            grand_total["profit_factor"] = round(total_gross_wins / total_gross_losses, 2)
        elif total_gross_wins > 0:
            grand_total["profit_factor"] = 99.9
        else:
            grand_total["profit_factor"] = 0.0

        all_ops = []
        for r_item in ranked_list:
            all_ops.extend(r_item["trades"])
        all_ops.sort(key=lambda x: (x.get("fecha", ""), x.get("hora_apertura", "")), reverse=True)

        return {
            "ranking": ranked_list,
            "grand_total": grand_total,
            "all_trades": all_ops,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    # ==========================================
    # CENTRALIZED 2-SECOND UNIFIED ENGINE LOOP
    # ==========================================
    def run_trading_loop(self):
        """
        BUCLE CENTRAL SINCRONIZADO CADA 2 SEGUNDOS:
        1. Consulta en bloque las posiciones abiertas de la cuenta (1 sola llamada HTTP).
        2. Consulta en paralelo los datos de mercado de todos los símbolos activos.
        3. Si hay posición abierta: Monitorea y ejecuta ajustes de SL y TP (Escalera de hitos).
        4. Si NO hay posición: Evalúa setup VWAP (Filtro previo 5m + Toque/Cruce actual) y dispara orden.
        5. Actualiza telemetría para WebSockets y dashboard.
        """
        self.log(f"⚡ [MONEY MACHINE] Motor VWAP Puro iniciado (Latido: {POLL_INTERVAL_SECONDS}s).", "SYSTEM")

        while not self.stop_event.is_set():
            loop_start = time.time()

            try:
                # Sincronización horaria periódica
                if time.time() - self.last_sync_time >= self.sync_interval_seconds:
                    try:
                        self.sync_real_data_from_topstep()
                    except Exception as se:
                        self.log(f"⚠️ Error sincronización periódica: {se}", "ERROR")

                # 1. CONSULTA EN BLOQUE DE POSICIONES ABIERTAS (1 sola llamada para toda la cuenta)
                cached_positions = []
                if self.account_id:
                    try:
                        cached_positions = self.client.get_open_positions(self.account_id)
                    except Exception:
                        cached_positions = None

                # Extraer símbolos con posiciones abiertas vivas en TopstepX
                open_syms_in_topstep = set()
                if cached_positions:
                    for p in cached_positions:
                        if abs(int(p.get("size", 0))) > 0:
                            p_cid = str(p.get("contractId", "")).strip().upper()
                            s = self.resolve_symbol_from_contract(p_cid)
                            if s:
                                open_syms_in_topstep.add(s)

                # Procesar símbolos habilitados, en pantalla, O CON POSICIONES ABIERTAS / TRADES ACTIVOS
                symbols_to_process = [
                    sym for sym, state in self.symbol_states.items()
                    if state.get("contract_id") and (
                        state.get("enabled", False)
                        or sym == self.active_symbol
                        or state.get("has_open_position", False)
                        or state.get("active_trade") is not None
                        or sym in open_syms_in_topstep
                    )
                ]

                # 2. OBTENCIÓN EN BLOQUE / PARALELO DE PRECIOS VIVOS Y VELAS PARA TODOS LOS SÍMBOLOS ACTIVOS
                now_ts = time.time()
                
                # Respetar pausa si hubo Rate Limit (429) recientemente
                if now_ts < getattr(self, "_rate_limited_until", 0.0):
                    time.sleep(1.0)
                    continue

                def fetch_symbol_market_data(sym_key):
                    st = self.symbol_states[sym_key]
                    c_id = st.get("contract_id")
                    need_5m = (now_ts - st.get("last_5m_fetch_time", 0.0)) >= (POLL_INTERVAL_SECONDS - 0.2) or st.get("latest_df").empty
                    
                    df_5m_res = st.get("latest_df")
                    if need_5m:
                        existing_df = st.get("latest_df")
                        # Sincronización completa de la sesión CME (300 velas de 5m = 25h) en inicio o cada 120s; luego ligero (5 velas)
                        is_initial_or_stale = existing_df is None or existing_df.empty or (now_ts - st.get("last_full_sync_time", 0.0)) >= 120.0
                        limit_to_fetch = 300 if is_initial_or_stale else 5
                        days_back_fetch = 2 if is_initial_or_stale else 1

                        df_fetched = self.client.retrieve_bars(c_id, unit=2, unit_number=5, limit=limit_to_fetch, days_back=days_back_fetch)
                        if not df_fetched.empty:
                            if existing_df is not None and not existing_df.empty and not is_initial_or_stale:
                                df_combined = pd.concat([existing_df, df_fetched]).drop_duplicates(subset=['Timestamp'], keep='last').sort_values('Timestamp').reset_index(drop=True)
                            else:
                                df_combined = df_fetched

                            df_5m_res = self.calcular_vwap_sesion(df_combined)
                            with self.lock:
                                st["latest_df"] = df_5m_res
                                st["last_5m_fetch_time"] = now_ts
                                if is_initial_or_stale:
                                    st["last_full_sync_time"] = now_ts
                        elif df_5m_res is None or df_5m_res.empty:
                            self._rate_limited_until = now_ts + 4.0

                    return sym_key, df_5m_res

                market_data = {}
                if symbols_to_process:
                    max_w = min(len(symbols_to_process), 4)
                    with ThreadPoolExecutor(max_workers=max_w) as executor:
                        for s_key, df_5m_res in executor.map(fetch_symbol_market_data, symbols_to_process):
                            market_data[s_key] = df_5m_res

                for sym in symbols_to_process:
                    if self.stop_event.is_set():
                        break

                    state = self.symbol_states[sym]
                    contract_id = state.get("contract_id")
                    if not contract_id or sym not in market_data:
                        continue

                    is_enabled = state.get("enabled", False)
                    df_5m = market_data[sym]

                    if df_5m is None or df_5m.empty:
                        continue

                    spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
                    tick_size = spec.get("tickSize", 0.25)

                    live_bar = df_5m.iloc[-1]
                    current_price = float(live_bar['Close'])
                    live_high = float(live_bar['High'])
                    live_low = float(live_bar['Low'])

                    # Actualizar telemetría de símbolo
                    if df_5m is not None and not df_5m.empty:
                        vela_5m = df_5m.iloc[-1]
                        vwap_val = float(vela_5m['VWAP'])
                        upper_val = float(vela_5m['Upper_Band'])
                        lower_val = float(vela_5m['Lower_Band'])
                        dist_vwap = current_price - vwap_val
                        cd_left = max(0, int(state["cooldown_until"] - time.time()))

                        with self.lock:
                            state["telemetry"] = {
                                "symbol": sym,
                                "contract": state["contract_name"],
                                "timestamp": vela_5m['Timestamp'].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "close": current_price,
                                "open": float(vela_5m['Open']),
                                "high": max(float(vela_5m['High']), live_high),
                                "low": min(float(vela_5m['Low']), live_low),
                                "volume": int(vela_5m['Volume']),
                                "vwap": vwap_val,
                                "upper_band": upper_val,
                                "lower_band": lower_val,
                                "distance_vwap": dist_vwap,
                                "enabled": is_enabled,
                                "cooldown_remaining": cd_left
                            }

                    # RAMA A: GESTIÓN DE POSICIÓN ABIERTA (Usa posiciones consultadas en bloque)
                    has_open_pos = self.tiene_posicion_abierta(sym, cached_positions=cached_positions)
                    if has_open_pos or state.get("active_trade"):
                        self.monitorear_posicion_abierta(sym, current_price=current_price, df_5m=df_5m, cached_positions=cached_positions)

                    # RAMA B: BÚSQUEDA DE NUEVAS ENTRADAS (Permite hasta 2 operaciones si la 1ª ya está asegurada)
                    can_open, open_reason = self.puede_abrir_posicion(sym, cached_positions=cached_positions)
                    if can_open and is_enabled and df_5m is not None and not df_5m.empty:
                        # Respetar Cooldown y evitar reentradas repetidas en la misma vela
                        vela_5m_actual = df_5m.iloc[-1]
                        vela_ts = vela_5m_actual['Timestamp']
                        
                        if time.time() >= state["cooldown_until"] and state.get("last_executed_candle_ts") != vela_ts:
                            setup = self.verificar_setup_vwap(df_5m, sym, current_price=current_price)
                            if setup:
                                state["last_executed_candle_ts"] = vela_ts
                                trg_t = setup.get('trigger_ticks', 15)
                                dist_t = abs(current_price - setup['vwap_cruce']) / tick_size
                                self.log(
                                    f"⚡ [{sym}] ¡Señal activada! ({setup['direccion']}) | Entrada @ {current_price:.2f} | "
                                    f"VWAP: {setup['vwap_cruce']:.2f} | SL: {setup['precio_sl']:.2f} ({setup['sl_ticks']}t) | "
                                    f"TP: {setup['precio_tp']:.2f} ({setup['tp_ticks']}t)",
                                    "ORDER", sym
                                )
                                self.ejecutar_orden(setup, sym)

                # ==========================================
                # 3. VERIFICACIÓN DE CANASTA DE BENEFICIOS GLOBAL (DESACTIVADA POR REGLAS DE USUARIO)
                # ==========================================
                total_floating_pnl = 0.0
                active_open_count = 0
                basket_details = []

                for s_sym, s_state in self.symbol_states.items():
                    act_trade = s_state.get("active_trade")
                    if act_trade:
                        ent_p = float(act_trade.get("entry_price", 0.0))
                        dir_t = act_trade.get("direction", "LONG")
                        cur_p = act_trade.get("current_price") or s_state.get("telemetry", {}).get("close")
                        if cur_p and ent_p > 0:
                            pt_val = SUPPORTED_SYMBOLS.get(s_sym, {}).get("pointValue", 50.0)
                            diff = (cur_p - ent_p) if dir_t == "LONG" else (ent_p - cur_p)
                            pos_unrealized = round(diff * pt_val, 2)
                            total_floating_pnl += pos_unrealized
                            active_open_count += 1
                            basket_details.append(f"{s_sym}: {pos_unrealized:+.2f}")

                # Gatillo de seguridad (Solo si USE_BASKET_TP está habilitado):
                if USE_BASKET_TP and active_open_count > 0 and total_floating_pnl >= BASKET_TP_TARGET_USD:
                    self.log(
                        f"🏆🏆🏆 [BASKET TAKE-PROFIT] ¡CANASTA GLOBAL DE BENEFICIOS ALCANZADA! "
                        f"PnL Flotante Total: +${total_floating_pnl:.2f} USD (Meta: ${BASKET_TP_TARGET_USD:.2f} USD | "
                        f"{' | '.join(basket_details)}). "
                        f"Ejecutando CIERRE TOTAL INMEDIATO de todas las {active_open_count} posiciones...",
                        "ALERT"
                    )
                    
                    # Cierre de emergencia atómico de toda la cartera
                    self.emergency_flatten_all(reason=f"Basket TP Alcanzado (+${total_floating_pnl:.2f} USD)")
                    
                    # Pausa de seguridad (Cooldown) a todos los símbolos para no reingresar en el mismo movimiento
                    cooldown_ts = time.time() + BASKET_COOLDOWN_SECONDS
                    for s_st in self.symbol_states.values():
                        s_st["cooldown_until"] = max(s_st.get("cooldown_until", 0.0), cooldown_ts)
                        
                    self.log(
                        f"🔒 [BASKET TAKE-PROFIT] Cartera asegurada con éxito (+${total_floating_pnl:.2f} USD). "
                        f"Pausa de seguridad activada por {int(BASKET_COOLDOWN_SECONDS/60)} minutos.",
                        "SYSTEM"
                    )

                # ==========================================
                # 4. CANDADO DE GANANCIA DIARIA TRAILING (TRAILING DAILY PROFIT LOCK)
                # ==========================================
                self.verificar_candado_ganancia_diaria(total_floating_pnl)

                # Reporte periódico en terminal del estado de análisis y portafolio (cada 15s)
                if (now_ts - getattr(self, "_last_portfolio_log_time", 0.0)) >= 15.0:
                    self._last_portfolio_log_time = now_ts
                    open_summary = []
                    for s_k, s_st in self.symbol_states.items():
                        act_tr = s_st.get("active_trade")
                        if act_tr:
                            stg_desc = self.format_milestone(act_tr.get("sl_stage", 0))
                            mx_p = act_tr.get("max_pct_avance", 0.0)
                            open_summary.append(f"{s_k} {act_tr['direction']} @ {act_tr['entry_price']:.2f} [{stg_desc} (Max: {mx_p:.1f}%)]")
                    if open_summary:
                        self.log(f"💼 [PORTAFOLIO] Posiciones Activas ({len(open_summary)}): {' | '.join(open_summary)}", "MONITOR", "ALL")
                    else:
                        act_tel = self.symbol_states.get(self.active_symbol, {}).get("telemetry", {})
                        if act_tel:
                            p_close = act_tel.get("close", 0.0)
                            p_vwap = act_tel.get("vwap", 0.0)
                            p_dist = act_tel.get("distance_vwap", 0.0)
                            spec_act = SUPPORTED_SYMBOLS.get(self.active_symbol, {})
                            t_size = spec_act.get("tickSize", 0.25)
                            dist_ticks = round(p_dist / t_size) if t_size > 0 else 0
                            self.log(
                                f"🔍 [ANÁLISIS EN VIVO] {self.active_symbol} @ {p_close:.2f} | VWAP: {p_vwap:.2f} "
                                f"(Dist: {dist_ticks:+d}t / {p_dist:+.2f}) | "
                                f"Banda Sup: {act_tel.get('upper_band', 0.0):.2f} | Banda Inf: {act_tel.get('lower_band', 0.0):.2f} | "
                                f"12 símbolos CME analizados cada {int(POLL_INTERVAL_SECONDS)}s (Rechazo Mecha VWAP 5m | SL {int(SL_VWAP_BAND_RATIO * 100)}% Banda | TP {int(TP_VWAP_BAND_RATIO * 100)}% Banda | Dinámico BE {DYNAMIC_BE_REFERENCE}+2t @ {int(DYNAMIC_BE_PROFIT_PCT)}% TP)",
                                "SCANNER", self.active_symbol
                            )

            except requests.exceptions.HTTPError as he:
                if he.response is not None and he.response.status_code == 429:
                    self.log("⏸️ TopstepX Rate Limit temporal alcanzado (429). Pausando 4 segundos...", "SYSTEM")
                    time.sleep(4)
                else:
                    self.log(f"⚠️ Excepción HTTP en bucle central: {he}", "ERROR")
            except Exception as e:
                self.log(f"⚠️ Excepción en bucle central: {e}", "ERROR")

            # Control estricto de latido (2.0 segundos)
            elapsed = time.time() - loop_start
            sleep_time = max(0.1, POLL_INTERVAL_SECONDS - elapsed)
            if not self.stop_event.is_set():
                time.sleep(sleep_time)

        self.log("🛑 Bucle central del motor detenido correctamente.", "SYSTEM")

    def run_loop(self):
        """Punto de entrada compatible."""
        self.run_trading_loop()

    # ==========================================
    # LIFECYCLE MANAGEMENT
    # ==========================================
    def start(self):
        if self.running:
            self.log("⚠️ El motor ya está activo.", "SYSTEM")
            return
            
        self.stop_event.clear()
        self.running = True
        
        self.thread = threading.Thread(target=self.run_trading_loop, daemon=True, name="Axiom-CentralEngine")
        self.thread.start()
        
        self.log("⚡ Motor algorítmico iniciado exitosamente en hilo único de alta velocidad.", "SYSTEM")

    def stop(self):
        if not self.running:
            return
            
        self.log("🛑 Deteniendo motor algorítmico...", "SYSTEM")
        self.running = False
        self.stop_event.set()
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def emergency_flatten_all(self, reason: str = "KILL SWITCH GENERAL") -> Dict[str, Any]:
        """
        CIERRE DE EMERGENCIA TOTAL DE CARTERA (FLATTEN ALL CON ALTA SEGURIDAD):
        1. Consulta todas las posiciones abiertas activas directamente en TopstepX.
        2. Recorre cada contrato y ejecuta close_contract_position.
        3. Cancela todas las órdenes residuales (SL/TP) en la cuenta.
        4. Limpia el estado interno de todos los símbolos en memoria y registra el cierre en CSV.
        5. Realiza una verificación de doble confirmación para asegurar 0 posiciones abiertas.
        """
        self.log(f"🚨 [KILL SWITCH GENERAL] Disparando cierre total de cartera. Motivo: {reason}", "ALERT")
        results = []
        closed_symbols = []

        if not self.account_id:
            return {"success": False, "error": "No account ID available"}

        # 1. Obtener posiciones reales abiertas desde TopstepX
        try:
            open_positions = self.client.get_open_positions(self.account_id)
        except Exception as e:
            self.log(f"⚠️ Error al obtener posiciones abiertas en TopstepX: {e}", "ERROR")
            open_positions = []

        # Recopilar contratos a cerrar (desde API y desde memoria local)
        contracts_to_close = {}
        for p in open_positions:
            cid = str(p.get("contractId", "")).strip().upper()
            if cid and abs(int(p.get("size", 0))) > 0:
                contracts_to_close[cid] = self.resolve_symbol_from_contract(cid)

        for sym, state in self.symbol_states.items():
            if state.get("has_open_position") or state.get("active_trade"):
                cid = state.get("contract_id")
                if cid:
                    contracts_to_close[cid] = sym

        # 2. Ejecutar cierre de cada contrato y cancelar órdenes
        for cid, sym in contracts_to_close.items():
            sym_label = sym or self.resolve_symbol_from_contract(cid)
            try:
                res = self.client.close_contract_position(self.account_id, cid)
                self.log(f"🛡️ [FLATTEN] Cierre ejecutado en TopstepX para {sym_label} ({cid}): {res}", "ALERT", sym_label)
                results.append({"contract_id": cid, "symbol": sym_label, "res": res})
                closed_symbols.append(sym_label)
            except Exception as ce:
                self.log(f"❌ Error al cerrar posición {sym_label} ({cid}): {ce}", "ERROR", sym_label)

            # Cancelar órdenes residuales de este contrato
            self.cancelar_ordenes_abiertas_contrato(cid, sym_label)

            # Limpiar estado en memoria y registrar cierre en CSV
            state = self.symbol_states.get(sym_label, {})
            trade = state.get("active_trade")
            if trade:
                curr_price = trade.get("current_price") or state.get("telemetry", {}).get("close")
                self.registrar_cierre_posicion(sym_label, trade, precio_salida=curr_price, motivo_cierre=reason)
            state["has_open_position"] = False
            state["active_trade"] = None

        # 3. VERIFICACIÓN DE SEGURIDAD (Confirmar que la cuenta quedó 100% plana)
        time.sleep(0.5)
        try:
            remaining_positions = self.client.get_open_positions(self.account_id)
            active_remaining = [p for p in remaining_positions if abs(int(p.get("size", 0))) > 0]
            if active_remaining:
                self.log(f"⚠️ [SEGURIDAD] Reintento: Quedan {len(active_remaining)} posición(es) residuales. Forzando segundo cierre...", "ALERT")
                for rp in active_remaining:
                    rcid = str(rp.get("contractId", "")).strip().upper()
                    try:
                        self.client.close_contract_position(self.account_id, rcid)
                        self.cancelar_ordenes_abiertas_contrato(rcid, self.resolve_symbol_from_contract(rcid))
                    except Exception:
                        pass
        except Exception:
            pass

        self.log(f"✅ [KILL SWITCH GENERAL] Cierre total completado. Símbolos liquidados: {closed_symbols or 'Ninguno activo'}.", "SYSTEM")
        return {"success": True, "closed_symbols": closed_symbols, "results": results}

    def emergency_flatten(self, symbol_key: Optional[str] = None) -> Dict[str, Any]:
        """Cierra inmediatamente posiciones en el símbolo especificado o en TODA la cartera si no se especifica."""
        if not symbol_key or symbol_key.upper() in ["ALL", "CARTERA", "TOTAL"]:
            return self.emergency_flatten_all(reason="Kill Switch General / Flatten Portfolio")

        sym = symbol_key.upper()
        contract_id = self.symbol_states.get(sym, {}).get("contract_id")
        self.log(f"⚠️ [KILL SWITCH] Disparando cierre de emergencia individual para {sym}...", "ALERT", sym)
        try:
            res = self.client.close_contract_position(self.account_id, contract_id)
            self.log(f"🛡️ Cierre ejecutado en TopstepX: {res}", "ALERT", sym)
            self.cancelar_ordenes_abiertas_contrato(contract_id, sym)
            state = self.symbol_states.get(sym, {})
            trade = state.get("active_trade")
            if trade:
                curr_price = trade.get("current_price") or state.get("telemetry", {}).get("close")
                self.registrar_cierre_posicion(sym, trade, precio_salida=curr_price, motivo_cierre="Kill Switch Manual")
            state["has_open_position"] = False
            state["active_trade"] = None
            return res
        except Exception as e:
            self.log(f"❌ Error en cierre de emergencia para {sym}: {e}", "ERROR", sym)
            return {"success": False, "error": str(e)}

    def calculate_basket_floating_pnl(self) -> float:
        """Calcula el PnL flotante agregado (no realizado) de todas las posiciones activas en memoria."""
        total = 0.0
        for s_sym, s_st in self.symbol_states.items():
            act_tr = s_st.get("active_trade")
            if act_tr:
                ent = float(act_tr.get("entry_price", 0.0))
                direc = act_tr.get("direction", "LONG")
                curr = act_tr.get("current_price") or s_st.get("telemetry", {}).get("close")
                if curr and ent > 0:
                    pt_val = SUPPORTED_SYMBOLS.get(s_sym, {}).get("pointValue", 50.0)
                    diff = (curr - ent) if direc == "LONG" else (ent - curr)
                    total += diff * pt_val
        return round(total, 2)

    def verificar_candado_ganancia_diaria(self, total_floating_pnl: float):
        """
        Candado de Ganancia Diaria Trailing (Trailing Daily Profit Lock).
        Desactivado por regla explícita del usuario (sin topes de ganancia ni pérdida).
        """
        if not USE_DAILY_PROFIT_LOCK:
            return
        try:
            ny_time = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            utc_now = datetime.now(timezone.utc)
            ny_time = utc_now - timedelta(hours=4)

        # En futuros CME, a las 18:00 ET comienza la sesión operativa del día siguiente
        cme_trade_date = (ny_time + timedelta(hours=6)).strftime("%Y-%m-%d")
        
        # Reset de sesión cuando cambia el día operativo
        if self.current_trading_day != cme_trade_date:
            self.current_trading_day = cme_trade_date
            self.daily_lock_active = False
            self.daily_peak_pnl = 0.0
            self.daily_floor_pnl = 0.0
            self.daily_lock_reason = None

        if self.daily_lock_active:
            return

        # Calcular PnL neto realizado hoy desde CSV
        realized_today = 0.0
        try:
            records = self.load_operations_history()
            cal_date_today = ny_time.strftime("%Y-%m-%d")
            for r in records:
                r_fecha = r.get("Fecha", "")
                if r_fecha in (cme_trade_date, cal_date_today):
                    estado = (r.get("Estado") or "").upper()
                    if "CERRADA" in estado:
                        try:
                            realized_today += safe_float(r.get("PnL_Neto_USD"), 0.0)
                        except Exception:
                            pass
        except Exception:
            pass

        today_total_pnl = round(realized_today + total_floating_pnl, 2)

        # META TOTAL DE SESIÓN ($1,500 USD): Parar operaciones si se llega a tener ese profit en la sesión completa
        if today_total_pnl >= SESSION_PROFIT_TARGET_USD:
            self.daily_lock_active = True
            self.daily_lock_reason = (
                f"Meta Total de Sesión Alcanzada: PnL alcanzó +${today_total_pnl:.2f} USD "
                f"(Meta Objetivo: ${SESSION_PROFIT_TARGET_USD:.2f} USD). Sesión asegurada con éxito."
            )
            self.log(
                f"🏆🏆🏆 [META TOTAL DE SESIÓN] ¡BENEFICIO DE SESIÓN DE +${today_total_pnl:.2f} USD ALCANZADO! "
                f"Ejecutando CIERRE TOTAL de cartera y deteniendo operaciones por el resto del día.",
                "ALERT"
            )
            self.emergency_flatten_all(reason=self.daily_lock_reason)
            return

        # Actualizar pico diario
        if today_total_pnl > self.daily_peak_pnl:
            self.daily_peak_pnl = today_total_pnl

        # Si el pico diario alcanza o supera el umbral de activación (+$750 USD)
        if self.daily_peak_pnl >= TRAILING_DAILY_PROFIT_TRIGGER_USD:
            nuevo_piso = round(self.daily_peak_pnl * TRAILING_DAILY_PROFIT_RETAIN_PCT, 2)
            if nuevo_piso > self.daily_floor_pnl:
                self.daily_floor_pnl = nuevo_piso
                self.log(
                    f"🛡️ [PISO TRAILING DIARIO] Pico alcanzado: +${self.daily_peak_pnl:.2f} USD | "
                    f"Piso de protección 70% elevado a: +${self.daily_floor_pnl:.2f} USD (Permite hasta 30% de pullback para buscar más ganancia)",
                    "SYSTEM"
                )

            # Verificar si el beneficio total perforó el piso de protección
            if today_total_pnl < self.daily_floor_pnl:
                self.daily_lock_active = True
                self.daily_lock_reason = (
                    f"Piso Trailing Diario Alcanzado: PnL retrocedió a ${today_total_pnl:.2f} USD "
                    f"(Piso de seguridad: ${self.daily_floor_pnl:.2f} USD / 70% de Pico ${self.daily_peak_pnl:.2f} USD). "
                    f"Capital blindado con éxito."
                )
                self.log(
                    f"🔒🔒🔒 [DAILY PROFIT LOCK] ¡ALERTA DE PROTECCIÓN DE CAPITAL! {self.daily_lock_reason}. "
                    f"Ejecutando CIERRE TOTAL de cartera y congelando operaciones por el resto del día.",
                    "ALERT"
                )
                self.emergency_flatten_all(reason=self.daily_lock_reason)

    # ==========================================
    # DATA SERIALIZATION FOR FASTAPI & DASHBOARD
    # ==========================================
    def get_chart_data(self, symbol_key: Optional[str] = None) -> Dict[str, Any]:
        sym = (symbol_key or self.active_symbol).upper()
        state = self.symbol_states.get(sym, {})
        
        with self.lock:
            df = state.get("latest_df", pd.DataFrame())
            if df.empty:
                return {
                    "symbol": sym,
                    "contract": state.get("contract_name", "N/A"),
                    "enabled": state.get("enabled", False),
                    "candles": [], "vwap": [], "upper_band": [], "lower_band": []
                }
            df = df.copy()
            
        candles = []
        vwap_points = []
        upper_points = []
        lower_points = []
        
        for _, row in df.iterrows():
            ts = int(row['Timestamp'].timestamp())
            candles.append({
                "time": ts,
                "open": float(row['Open']),
                "high": float(row['High']),
                "low": float(row['Low']),
                "close": float(row['Close']),
                "volume": int(row['Volume'])
            })
            if not np.isnan(row['VWAP']):
                vwap_points.append({"time": ts, "value": float(row['VWAP'])})
            if not np.isnan(row['Upper_Band']):
                upper_points.append({"time": ts, "value": float(row['Upper_Band'])})
            if not np.isnan(row['Lower_Band']):
                lower_points.append({"time": ts, "value": float(row['Lower_Band'])})
                
        return {
            "symbol": sym,
            "contract": state.get("contract_name", "N/A"),
            "enabled": state.get("enabled", False),
            "tickSize": SUPPORTED_SYMBOLS.get(sym, {}).get("tickSize", 0.25),
            "candles": candles,
            "vwap": vwap_points,
            "upper_band": upper_points,
            "lower_band": lower_points
        }

    @property
    def latest_telemetry(self) -> Dict[str, Any]:
        return self.symbol_states.get(self.active_symbol, {}).get("telemetry", {})

    def get_status(self) -> Dict[str, Any]:
        active_state = self.symbol_states.get(self.active_symbol, {})
        
        matrix = {}
        for sym, s in self.symbol_states.items():
            t = s.get("telemetry", {})
            sym_stats = self.stats.get("by_symbol", {}).get(sym, {})
            matrix[sym] = {
                "symbol": sym,
                "name": s.get("name"),
                "enabled": s.get("enabled", False),
                "contract": s.get("contract_name"),
                "price": t.get("close", 0.0),
                "vwap": t.get("vwap", 0.0),
                "upper": t.get("upper_band", 0.0),
                "lower": t.get("lower_band", 0.0),
                "spread_vwap": t.get("distance_vwap", 0.0),
                "cooldown": t.get("cooldown_remaining", 0),
                "trades": sym_stats.get("total", 0),
                "net_pnl": sym_stats.get("net_pnl", 0.0)
            }
            
        is_operable, motivo_horario = self.es_horario_operable()
        return {
            "running": self.running,
            "active_symbol": self.active_symbol,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "telemetry": active_state.get("telemetry", {}),
            "stats": self.stats,
            "symbols_matrix": matrix,
            "supported_symbols": list(SUPPORTED_SYMBOLS.keys()),
            "last_sync": self.stats.get("last_sync", "N/A"),
            "next_sync": self.stats.get("next_sync", "N/A"),
            "schedule": {
                "operable": is_operable,
                "reason": motivo_horario
            },
            "basket_tp": {
                "target_usd": BASKET_TP_TARGET_USD,
                "hard_usd": BASKET_TP_HARD_USD,
                "current_floating_usd": self.calculate_basket_floating_pnl()
            },
            "daily_lock": {
                "active": self.daily_lock_active,
                "trading_day": self.current_trading_day,
                "peak_pnl_usd": self.daily_peak_pnl,
                "floor_pnl_usd": self.daily_floor_pnl,
                "trigger_threshold_usd": TRAILING_DAILY_PROFIT_TRIGGER_USD,
                "retain_pct": TRAILING_DAILY_PROFIT_RETAIN_PCT,
                "reason": self.daily_lock_reason
            }
        }
