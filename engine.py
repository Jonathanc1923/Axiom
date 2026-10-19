import os
import sys
import io
import csv
import time
import threading
import requests

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

import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Callable
import pandas as pd
import numpy as np

from config import CSV_PATH, CSV_DIR, SUPPORTED_SYMBOLS, DEFAULT_SYMBOL, DATA_DIR
from topstep_client import TopstepClient

class AxiomEngine:
    def __init__(self, client: TopstepClient, default_symbol: str = DEFAULT_SYMBOL):
        self.client = client
        self.active_symbol = default_symbol.upper()
        self.account_id: Optional[int] = None
        self.account_name: str = "N/A"
        
        # State flags
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        
        # Multi-Symbol Registry
        # By default, ES and NQ start enabled for autonomous trading; others can be toggled on/off
        self.symbol_states: Dict[str, Dict[str, Any]] = {}
        for sym, spec in SUPPORTED_SYMBOLS.items():
            self.symbol_states[sym] = {
                "symbol": sym,
                "name": spec.get("name", sym),
                "enabled": (sym in ["ES", "NQ"]), # Trading active by default for core assets
                "contract_id": None,
                "contract_name": "N/A",
                "tick_size": spec.get("tickSize", 0.25),
                "point_value": spec.get("pointValue", 50.0),
                "telemetry": {},
                "latest_df": pd.DataFrame(),
                "cooldown_until": 0,
                "latest_candle_time": None,
                "last_executed_candle_ts": None,
                "has_open_position": False,
                "logged_pos_open": False,
                "last_inoperable_reason": None,
                "last_poll_time": 0.0,
                "stats": {"trades": 0, "longs": 0, "shorts": 0, "executed": 0, "rejected": 0}
            }
            
        # Dedicated Real-time Logs per Symbol (Retención Estricta de 24 Horas)
        self.logs_file_path = os.path.join(DATA_DIR, "logs_history_24h.jsonl")
        self.symbol_logs: Dict[str, List[Dict[str, Any]]] = {sym: [] for sym in SUPPORTED_SYMBOLS}
        self.log_history: List[Dict[str, Any]] = [] # Global combined stream
        self.max_logs: int = 50000 # Límite superior amplio para conservar 24 horas continuas
        self.log_listeners: List[Callable[[Dict[str, Any]], None]] = []
        
        # Cargar historial previo de logs de las últimas 24 horas desde disco
        self.load_persisted_logs()
        
        # Hourly Topstep API Synchronization Clock
        self.last_sync_time: float = 0
        self.next_sync_time: float = 0
        self.sync_interval_seconds: int = 3600  # Sincronización exacta cada 1 hora

        # Consolidated Platform Execution Statistics (Métricas Reales de TopstepX)
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
                        
            with self.lock:
                self.log_history = valid_entries[-30000:]
                for entry in valid_entries:
                    s = entry.get("symbol", "ALL")
                    if s == "ALL":
                        for sym_k in self.symbol_logs:
                            self.symbol_logs[sym_k].append(entry)
                    elif s in self.symbol_logs:
                        self.symbol_logs[s].append(entry)
                        
            # Reescribir archivo podado estrictamente a las últimas 24 horas
            with open(self.logs_file_path, "w", encoding="utf-8") as lf:
                for entry in valid_entries:
                    lf.write(json.dumps(entry) + "\n")
                    
            print(f"✅ [AXIOM LOGS] Restaurados {len(valid_entries)} logs de las últimas 24h.", flush=True)
        except Exception as e:
            print(f"⚠️ Error cargando logs persistidos de 24h: {e}", flush=True)

    def log(self, message: str, category: str = "INFO", symbol: Optional[str] = None):
        """Emite un log estructurado global y hacia la terminal dedicada del símbolo con retención de 24 horas."""
        now_dt = datetime.now()
        now_str = now_dt.strftime('%H:%M:%S')
        now_ts = time.time()
        
        if symbol is None:
            sym = "ALL" if category in ["SYSTEM", "ALERT", "ERROR"] else self.active_symbol.upper()
        else:
            sym = symbol.upper()
        
        log_entry = {
            "timestamp": now_str,
            "full_timestamp": now_dt.strftime('%Y-%m-%d %H:%M:%S'),
            "epoch": now_ts,
            "category": category,
            "message": message,
            "symbol": sym
        }
        
        print(f"[{now_str}] [{sym}] {message}", flush=True)
        
        cutoff = now_ts - 86400 # Retención estricta: 24 horas (86,400 segundos)
        
        with self.lock:
            # 1. Global log history (últimas 24h)
            self.log_history.append(log_entry)
            while self.log_history and self.log_history[0].get("epoch", 0) < cutoff:
                self.log_history.pop(0)
            if len(self.log_history) > self.max_logs:
                self.log_history.pop(0)
                
            # 2. Dedicated symbol log history (ALL distribuye a todos los tabs)
            target_syms = list(self.symbol_logs.keys()) if sym == "ALL" else ([sym] if sym in self.symbol_logs else [])
            for s_sym in target_syms:
                self.symbol_logs[s_sym].append(log_entry)
                while self.symbol_logs[s_sym] and self.symbol_logs[s_sym][0].get("epoch", 0) < cutoff:
                    self.symbol_logs[s_sym].pop(0)
                    
            listeners_copy = list(self.log_listeners)
            
        for listener in listeners_copy:
            try:
                listener(log_entry)
            except Exception:
                pass

        # Persistir a disco en tiempo real para evitar pérdidas de historial ante reinicios
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
        self.log("🚀 Inicializando Gateway Cuantitativo Axiom...", "SYSTEM", "ALL")
        self.client.ensure_authenticated()
        
        acc = self.client.get_practice_account()
        self.account_id = acc["id"]
        self.account_name = acc["name"]
        self.log(f"✅ Cuenta vinculada: {self.account_name} (ID: {self.account_id})", "SYSTEM", "ALL")
        
        # Resolver contratos TopstepX para todos los símbolos
        for sym in SUPPORTED_SYMBOLS.keys():
            try:
                c = self.client.resolve_contract(sym)
                self.symbol_states[sym]["contract_id"] = c["id"]
                self.symbol_states[sym]["contract_name"] = c["name"]
                status_txt = "ACTIVO" if self.symbol_states[sym]["enabled"] else "INACTIVO"
                self.log(f"Contrato listo: {c['name']} (ID: {c['id']}) | Trading: {status_txt}", "SYSTEM", sym)
            except Exception as e:
                self.log(f"⚠️ No se pudo resolver contrato {sym}: {e}", "ERROR", sym)
                
        # Sincronización inmediata de datos reales de operaciones y balance desde TopstepX
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
                # Reset latest_candle_time y last_poll_time para evaluar de inmediato
                self.symbol_states[sym]["latest_candle_time"] = None
                self.symbol_states[sym]["last_poll_time"] = 0.0
            
        estado_str = "ACTIVO (Operando)" if new_state else "INACTIVO (Solo Monitoreo)"
        self.log(f"⚙️ Estado de trading actualizado: {estado_str}", "SYSTEM", sym)
        if new_state:
            self.log(f"🚀 Escáner autónomo activado para {sym}. Monitoreando mechas vs VWAP institucional...", "SETUP", sym)
        else:
            self.log(f"⏸️ Escáner autónomo pausado para {sym}. Operaciones suspendidas.", "SYSTEM", sym)
        return new_state

    def switch_active_view(self, symbol_key: str):
        """Cambia el símbolo que se muestra primariamente en el gráfico."""
        sym = symbol_key.upper()
        if sym not in SUPPORTED_SYMBOLS:
            raise ValueError(f"Símbolo no soportado: {sym}")
        with self.lock:
            self.active_symbol = sym
            if sym in self.symbol_states:
                self.symbol_states[sym]["last_poll_time"] = 0.0
        self.log(f"🖥️ Vista de gráfico cambiada a: {sym}", "SYSTEM", sym)

    # ==========================================
    # MATHEMATICAL ENGINE: SESSION VWAP
    # ==========================================
    @staticmethod
    def calcular_vwap_sesion(df: pd.DataFrame) -> pd.DataFrame:
        """Cálculo estricto de VWAP institucional con bandas de desviación estándar (+/-1 STD)."""
        if df.empty:
            return df
            
        df = df.copy()
        df['NY_Time'] = df['Timestamp'].dt.tz_convert('America/New_York')
        # Sesión diaria de futuros CME resetea a las 18:00 (6:00 PM ET)
        df['Session'] = (df['NY_Time'] + pd.Timedelta(hours=6)).dt.date
        
        df['HLC3'] = (df['High'] + df['Low'] + df['Close']) / 3.0
        df['PxV'] = df['HLC3'] * df['Volume']
        df['Px2xV'] = (df['HLC3'] ** 2) * df['Volume']
        
        df['Cum_Vol'] = df.groupby('Session')['Volume'].cumsum()
        df['Cum_PxV'] = df.groupby('Session')['PxV'].cumsum()
        df['Cum_Px2xV'] = df.groupby('Session')['Px2xV'].cumsum()
        
        df['VWAP'] = np.where(df['Cum_Vol'] > 0, df['Cum_PxV'] / df['Cum_Vol'], df['HLC3'])
        
        varianza = (df['Cum_Px2xV'] / df['Cum_Vol']) - (df['VWAP'] ** 2)
        df['Variance'] = np.maximum(varianza, 0)
        df['Std_Dev'] = np.sqrt(df['Variance'])
        df['Upper_Band'] = df['VWAP'] + df['Std_Dev']
        df['Lower_Band'] = df['VWAP'] - df['Std_Dev']
        
        return df

    # ==========================================
    # LOGICAL ENGINE: REAL-TIME WICK REJECTION (WITH 2-CANDLE CONFIRMATION)
    # ==========================================
    def verificar_setup_vwap(self, df: pd.DataFrame, sym: str, ignore_time_window: bool = False) -> Optional[Dict[str, Any]]:
        """
        Detecta en tiempo real segundo a segundo, en cualquier momento durante la formación
        de la vela de 5m, la presencia confirmada de una MECHA que haya tocado o perforado el VWAP
        sin que el cuerpo haya cruzado al otro lado (bajas posibilidades de que sea un cuerpo).
        
        REGLAS INSTITUCIONALES:
        1. Confirmación de tendencia previa (2 velas cerradas anteriores t-2 y t-1 en toda su extensión):
           - LONG: Ambas velas previas estuvieron 100% por encima del VWAP (Low > VWAP).
           - SHORT: Ambas velas previas estuvieron 100% por debajo del VWAP (High < VWAP).
        2. Análisis continuo de la vela actual en formación (t):
           - Se evalúa en cualquier momento desde los primeros segundos de formación (elapsed >= 8s hasta 305s).
        3. Distinción matemática estricta: MECHA vs CUERPO:
           - LONG:
             * Abrió por encima de VWAP: Open >= VWAP - (0.5 * tick_size).
             * Tocó o perforó el VWAP: Low <= VWAP.
             * El cuerpo NO cruzó el VWAP: min(Open, Close) >= VWAP - (0.5 * tick_size).
             * Mecha formada sustancial: min(Open, Close) - Low >= 2 * tick_size.
             * Rebote activo confirmado: Close - Low >= 2 * tick_size y Close >= VWAP + (1 * tick_size).
             * Patrón de rechazo absorbido: Close >= Open o mecha_inferior >= tamano_cuerpo * 0.5.
           - SHORT:
             * Abrió por debajo de VWAP: Open <= VWAP + (0.5 * tick_size).
             * Tocó o perforó el VWAP: High >= VWAP.
             * El cuerpo NO cruzó el VWAP: max(Open, Close) <= VWAP + (0.5 * tick_size).
             * Mecha formada sustancial: High - max(Open, Close) >= 2 * tick_size.
             * Rechazo activo confirmado: High - Close >= 2 * tick_size y Close <= VWAP - (1 * tick_size).
             * Patrón de rechazo absorbido: Close <= Open o mecha_superior >= tamano_cuerpo * 0.5.
        """
        if len(df) < 4:
            return None
            
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        
        # Vela en proceso (la más reciente) y las 2 velas previas cerradas
        vela_actual = df.iloc[-1]
        vela_prev1 = df.iloc[-2]  # Vela previa (t-1)
        vela_prev2 = df.iloc[-3]  # Vela anteprevia (t-2)
        
        # 1. Filtro temporal: La vela debe tener al menos 8 segundos de vida y no haber expirado (> 305s)
        if not ignore_time_window:
            now_utc = datetime.now(timezone.utc)
            vela_ts = vela_actual['Timestamp']
            if getattr(vela_ts, 'tzinfo', None) is None:
                vela_ts = vela_ts.replace(tzinfo=timezone.utc)
            elapsed_sec = (now_utc - vela_ts).total_seconds()
            
            # Apertura operable en cualquier segundo desde que arranca la vela (mínimo 8s para asentar datos)
            if elapsed_sec < 8 or elapsed_sec > 305:
                return None
        
        vwap_val = float(vela_actual['VWAP'])
        open_val = float(vela_actual['Open'])
        close_val = float(vela_actual['Close'])
        low_val = float(vela_actual['Low'])
        high_val = float(vela_actual['High'])
        
        p1_vwap = float(vela_prev1['VWAP'])
        p2_vwap = float(vela_prev2['VWAP'])
        
        # 1. EVALUAR SETUP LONG (Compras)
        # 2 velas previas 100% por encima de VWAP en toda su extensión
        t2_arriba = float(vela_prev2['Low']) > p2_vwap
        t1_arriba = float(vela_prev1['Low']) > p1_vwap
        previas_confirman_long = t2_arriba and t1_arriba
        
        # A. La vela abrió por encima del VWAP
        abrio_arriba = (open_val >= vwap_val - (0.5 * tick_size))
        
        # B. En algún momento transcurrido, tocó o perforó el VWAP
        mecha_toco_vwap_long = (low_val <= vwap_val)
        
        # C. El cuerpo actual NO está cruzando el VWAP (todo el cuerpo está por encima)
        cuerpo_bajo = min(open_val, close_val)
        cuerpo_no_cruzo_long = (cuerpo_bajo >= vwap_val - (0.5 * tick_size))
        
        # D. Confirmación de MECHA vs CUERPO (Bajas posibilidades de que sea cuerpo):
        mecha_inferior = cuerpo_bajo - low_val
        mecha_sustancial_long = (mecha_inferior >= 2 * tick_size)
        rebote_confirmado_long = (close_val - low_val >= 2 * tick_size)
        precio_seguro_long = (close_val >= vwap_val + (1 * tick_size))
        tamano_cuerpo_long = abs(close_val - open_val)
        patron_rechazo_long = (close_val >= open_val) or (mecha_inferior >= tamano_cuerpo_long * 0.5)
        
        # E. Proximidad de entrada: el precio actual no se ha ido excesivamente lejos del VWAP
        en_zona_entrada_long = (close_val <= vwap_val + max(12 * tick_size, abs(float(vela_actual['Upper_Band']) - vwap_val) * 0.40))
        
        if (previas_confirman_long and abrio_arriba and mecha_toco_vwap_long and 
            cuerpo_no_cruzo_long and mecha_sustancial_long and rebote_confirmado_long and 
            precio_seguro_long and patron_rechazo_long and en_zona_entrada_long):
            extremo = min(low_val, vwap_val)
            return {
                "direccion": "LONG",
                "simbolo": sym,
                "vela_cruce": vela_actual,
                "extremo_precio": extremo,
                "vwap_cruce": vwap_val,
                "upper_band": float(vela_actual['Upper_Band']),
                "lower_band": float(vela_actual['Lower_Band']),
                "mecha_pts": mecha_inferior
            }

        # 2. EVALUAR SETUP SHORT (Ventas)
        # 2 velas previas 100% por debajo de VWAP en toda su extensión
        t2_abajo = float(vela_prev2['High']) < p2_vwap
        t1_abajo = float(vela_prev1['High']) < p1_vwap
        previas_confirman_short = t2_abajo and t1_abajo
        
        # A. La vela abrió por debajo del VWAP
        abrio_abajo = (open_val <= vwap_val + (0.5 * tick_size))
        
        # B. En algún momento transcurrido, tocó o perforó el VWAP
        mecha_toco_vwap_short = (high_val >= vwap_val)
        
        # C. El cuerpo actual NO está cruzando el VWAP (todo el cuerpo está por debajo)
        cuerpo_alto = max(open_val, close_val)
        cuerpo_no_cruzo_short = (cuerpo_alto <= vwap_val + (0.5 * tick_size))
        
        # D. Confirmación de MECHA vs CUERPO (Bajas posibilidades de que sea cuerpo):
        mecha_superior = high_val - cuerpo_alto
        mecha_sustancial_short = (mecha_superior >= 2 * tick_size)
        rechazo_confirmado_short = (high_val - close_val >= 2 * tick_size)
        precio_seguro_short = (close_val <= vwap_val - (1 * tick_size))
        tamano_cuerpo_short = abs(close_val - open_val)
        patron_rechazo_short = (close_val <= open_val) or (mecha_superior >= tamano_cuerpo_short * 0.5)
        
        # E. Proximidad de entrada: el precio actual no se ha ido excesivamente lejos del VWAP
        en_zona_entrada_short = (close_val >= vwap_val - max(12 * tick_size, abs(vwap_val - float(vela_actual['Lower_Band'])) * 0.40))
        
        if (previas_confirman_short and abrio_abajo and mecha_toco_vwap_short and 
            cuerpo_no_cruzo_short and mecha_sustancial_short and rechazo_confirmado_short and 
            precio_seguro_short and patron_rechazo_short and en_zona_entrada_short):
            extremo = max(high_val, vwap_val)
            return {
                "direccion": "SHORT",
                "simbolo": sym,
                "vela_cruce": vela_actual,
                "extremo_precio": extremo,
                "vwap_cruce": vwap_val,
                "upper_band": float(vela_actual['Upper_Band']),
                "lower_band": float(vela_actual['Lower_Band']),
                "mecha_pts": mecha_superior
            }

        return None

    # ==========================================
    # RISK ENGINE: SL A 10 TICKS DEL EXTREMO DE LA MECHA Y TP AL 40% DISTANCIA VWAP-BANDA
    # ==========================================
    def calcular_riesgo_dinamico(self, direccion: str, precio_entrada: float, extremo_precio: float,
                                 upper_band: float, lower_band: float, sym: str,
                                 vwap_val: Optional[float] = None) -> tuple[int, int, float, float]:
        """
        Calcula SL y TP:
        - SL: Exactamente 10 ticks sobre el extremo que alcanzó la mecha:
              LONG: 10 ticks por debajo del precio más bajo alcanzado por la mecha (precio_sl = low_mecha - 10 ticks).
              SHORT: 10 ticks por encima del precio más alto alcanzado por la mecha (precio_sl = high_mecha + 10 ticks).
        - TP: 40% de la distancia entre la línea VWAP y la siguiente banda
              si es superior a 10 ticks; de lo contrario, TP estático de 10 ticks.
        """
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        ref_vwap = vwap_val if vwap_val is not None else precio_entrada
        
        if direccion == "LONG":
            # SL: 10 ticks por debajo del precio más bajo que haya alcanzado la mecha
            ref_extremo = extremo_precio if extremo_precio is not None else (precio_entrada - (10 * tick_size))
            precio_sl = ref_extremo - (10 * tick_size)
            distancia_sl_puntos = abs(precio_entrada - precio_sl)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 10)
            
            # TP: 40% de distancia entre VWAP y Banda Superior (min 10 ticks)
            distancia_banda = abs(upper_band - ref_vwap)
            ticks_40_pct = round((distancia_banda * 0.40) / tick_size)
            ticks_tp_abs = ticks_40_pct if ticks_40_pct > 10 else 10
            precio_tp = precio_entrada + (ticks_tp_abs * tick_size)
            
            # Convención de signos TopstepX: LONG -> SL negativo (-), TP positivo (+)
            return -ticks_sl_abs, ticks_tp_abs, precio_sl, precio_tp
            
        else: # SHORT
            # SL: 10 ticks por encima del precio más alto que haya alcanzado la mecha
            ref_extremo = extremo_precio if extremo_precio is not None else (precio_entrada + (10 * tick_size))
            precio_sl = ref_extremo + (10 * tick_size)
            distancia_sl_puntos = abs(precio_sl - precio_entrada)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 10)
            
            # TP: 40% de distancia entre VWAP y Banda Inferior (min 10 ticks)
            distancia_banda = abs(ref_vwap - lower_band)
            ticks_40_pct = round((distancia_banda * 0.40) / tick_size)
            ticks_tp_abs = ticks_40_pct if ticks_40_pct > 10 else 10
            precio_tp = precio_entrada - (ticks_tp_abs * tick_size)
            
            # Convención de signos TopstepX: SHORT -> SL positivo (+), TP negativo (-)
            return ticks_sl_abs, -ticks_tp_abs, precio_sl, precio_tp

    # ==========================================
    # RISK FILTERS & EXECUTION GUARDS
    # ==========================================
    def es_horario_operable(self) -> tuple[bool, str]:
        """
        Evalúa si el mercado CME está en horario operable según las reglas institucionales:
        1. Cierre de fin de semana (Viernes 17:00 ET a Domingo 18:00 ET).
        2. Receso diario de mantenimiento CME (Lunes a Jueves 17:00 a 18:00 ET).
        3. Filtro de apertura de sesión: Primeras 2 horas tras la apertura (18:00 a 20:00 ET) suspendidas.
        """
        try:
            ny_time = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            utc_now = datetime.now(timezone.utc)
            ny_time = utc_now - timedelta(hours=4)
            
        weekday = ny_time.weekday() # 0=Lunes, 4=Viernes, 5=Sábado, 6=Domingo
        hour = ny_time.hour
        minute = ny_time.minute
        time_str = f"{hour:02d}:{minute:02d} ET"
        
        # 1. Fin de semana
        if weekday == 5:
            return False, f"Mercado CME cerrado por fin de semana (Sábado - {time_str})"
        if weekday == 6 and hour < 18:
            return False, f"Mercado CME cerrado por fin de semana (Domingo pre-apertura - {time_str})"
        if weekday == 4 and hour >= 17:
            return False, f"Mercado CME cerrado por fin de semana (Viernes post-cierre - {time_str})"
            
        # 2. Receso diario de mantenimiento CME (17:00 a 18:00 ET)
        if hour == 17:
            return False, f"Receso diario de mantenimiento CME (17:00-18:00 ET - {time_str})"
            
        # 3. Filtro de apertura institucional: Primeras 2 horas tras la apertura (18:00 a 20:00 ET)
        if 18 <= hour < 20:
            return False, f"Filtro de apertura de sesión activo (Primeras 2 horas: 18:00-20:00 ET | Actual: {time_str})"
            
        return True, f"Sesión regular activa ({time_str})"

    def tiene_posicion_abierta(self, sym: str) -> bool:
        """
        Verifica en tiempo real si el símbolo ya tiene una posición abierta activa
        para no permitir más de una operación abierta a la vez por símbolo.
        """
        state = self.symbol_states.get(sym, {})
        contract_id = state.get("contract_id")
        
        try:
            if self.account_id:
                positions = self.client.get_open_positions(self.account_id)
                for pos in positions:
                    pos_cid = str(pos.get("contractId", ""))
                    pos_size = abs(int(pos.get("size", 0)))
                    pos_sym = self.resolve_symbol_from_contract(pos_cid)
                    if pos_size > 0 and (pos_cid == contract_id or pos_sym == sym):
                        state["has_open_position"] = True
                        return True
                        
                # Si TopstepX respondió exitosamente y no hay posición con size > 0:
                state["has_open_position"] = False
                return False
        except Exception as e:
            # Fallback seguro: consultar estado local previo si la API tuvo un timeout transitorio
            return state.get("has_open_position", False)
            
        return state.get("has_open_position", False)

    # ==========================================
    # EXECUTION ENGINE: ORDER DISPATCH
    # ==========================================
    def ejecutar_orden(self, setup: Dict[str, Any], df: pd.DataFrame, sym: str):
        # Validación de seguridad estricta: el setup debe pertenecer al símbolo que se está ejecutando
        if setup.get("simbolo") != sym:
            self.log(f"❌ Error de seguridad crítico: discrepancia de símbolo setup={setup.get('simbolo')} vs sym={sym}", "ERROR", sym)
            return

        # Validación 1: Filtro de horario de sesión CME (primeras 2 horas de apertura suspendidas)
        is_operable, motivo_horario = self.es_horario_operable()
        if not is_operable:
            self.log(f"⏸️ [{sym}] Orden CANCELADA: {motivo_horario}.", "SETUP", sym)
            return

        # Validación 2: Máximo 1 operación abierta por símbolo a la vez
        if self.tiene_posicion_abierta(sym):
            self.log(f"🛡️ [{sym}] Orden CANCELADA: Ya existe una posición abierta en mercado para {sym} (Máx 1 permitida).", "SETUP", sym)
            return
            
        direccion = setup["direccion"]
        extremo_precio = setup["extremo_precio"]
        vwap_cruce = setup.get("vwap_cruce")
        contract_id = self.symbol_states[sym]["contract_id"]
        precio_entrada = float(df.iloc[-1]['Close'])
        
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.calcular_riesgo_dinamico(
            direccion, precio_entrada, extremo_precio,
            float(setup['upper_band']),
            float(setup['lower_band']),
            sym,
            vwap_cruce
        )
        
        info_sl = f"SL: {abs(ticks_sl)} ticks ({precio_sl:,.2f} | 10 ticks desde extremo de mecha)"
        info_tp = f"TP: {abs(ticks_tp)} ticks ({precio_tp:,.2f} | 40% distancia VWAP a Banda / Mín 10t)"
        self.log(f"🚨 DISPARANDO {direccion} | Entrada: {precio_entrada:,.2f} | {info_sl} | {info_tp}", "ORDER", sym)
        
        side = 1 if direccion == "SHORT" else 0
        condicion_str = f"Mecha VWAP {setup['vwap_cruce']:.2f} ({setup.get('mecha_pts', 0)/tick_size:.0f}t Mecha | Extremo {extremo_precio:.2f}). SL: 10t Mecha ({precio_sl:.2f}) | TP 40% Banda: {abs(ticks_tp)}t ({precio_tp:.2f})"
        
        try:
            res = self.client.place_order(
                account_id=self.account_id,
                contract_id=contract_id,
                side=side,
                size=1,
                sl_ticks=ticks_sl,
                tp_ticks=ticks_tp,
                custom_tag=f"axiom-{sym.lower()}-{int(time.time())}"
            )
            
            if res.get("success"):
                order_id = res.get("orderId", "N/A")
                self.symbol_states[sym]["has_open_position"] = True
                self.log(f"✅ Ejecución exitosa en TopstepX. Order ID: {order_id}", "ORDER", sym)
                self.registrar_en_csv(
                    simbolo=sym,
                    direccion=direccion,
                    precio=precio_entrada,
                    ticks_sl=ticks_sl,
                    ticks_tp=ticks_tp,
                    condicion=condicion_str,
                    exito=True,
                    mensaje_servidor="Orden colocada con éxito en TopstepX",
                    error_code="0",
                    order_id=order_id
                )
                self.symbol_states[sym]["stats"]["executed"] += 1
                self.symbol_states[sym]["cooldown_until"] = time.time() + 300
                self.log("⏸️ Pausa estratégica de 5 minutos activada para este símbolo.", "SYSTEM", sym)
            else:
                error_msg = res.get('errorMessage') or res.get('message') or res.get('error') or 'Rechazo no especificado por API'
                error_code = str(res.get('errorCode') or res.get('code') or 'REJECTED')
                self.symbol_states[sym]["has_open_position"] = False
                self.log(f"❌ Orden RECHAZADA por TopstepX API en {sym}: {error_msg} (Código API: {error_code})", "ERROR", sym)
                self.registrar_en_csv(
                    simbolo=sym,
                    direccion=direccion,
                    precio=precio_entrada,
                    ticks_sl=ticks_sl,
                    ticks_tp=ticks_tp,
                    condicion=condicion_str,
                    exito=False,
                    mensaje_servidor=error_msg,
                    error_code=error_code,
                    order_id="N/A"
                )
                self.symbol_states[sym]["stats"]["rejected"] += 1
                
        except Exception as e:
            self.symbol_states[sym]["has_open_position"] = False
            self.log(f"❌ Excepción crítica al colocar orden en {sym}: {e}", "ERROR", sym)
            self.registrar_en_csv(
                simbolo=sym,
                direccion=direccion,
                precio=precio_entrada,
                ticks_sl=ticks_sl,
                ticks_tp=ticks_tp,
                condicion=condicion_str,
                exito=False,
                mensaje_servidor=f"Excepción: {str(e)}",
                error_code="EXCEPTION",
                order_id="N/A"
            )

    # ==========================================
    # BLACK BOX RECORDING & REAL TOPSTEP SYNCHRONIZATION
    # ==========================================
    def resolve_symbol_from_contract(self, contract_id: str) -> str:
        """Determina el símbolo estándar (ES, NQ, GC, CL, etc.) a partir del contractId de TopstepX."""
        c = str(contract_id).upper()
        for sym, state in self.symbol_states.items():
            if state.get("contract_id") and state["contract_id"] == contract_id:
                return sym
                
        prefix_map = [
            (".EP.", "ES"), (".MES.", "MES"), (".ENQ.", "NQ"), (".MNQ.", "MNQ"),
            (".YM.", "YM"), (".MYM.", "MYM"), (".RTY.", "RTY"), (".M2K.", "M2K"),
            (".GCE.", "GC"), (".MGC.", "MGC"), (".CLE.", "CL"), (".MCLE.", "MCL")
        ]
        for pfx, s in prefix_map:
            if pfx in c:
                return s
        return "ES"

    def registrar_en_csv(self, simbolo, direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id):
        """Guarda la orden emitida en el archivo CSV institucional con motivo claro de rechazo según la API."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(CSV_PATH)), exist_ok=True)
            archivo_existe = os.path.isfile(CSV_PATH)
            
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            
            if exito:
                estado = "EN CURSO (OCO)"
                motivo = "Aceptada"
                cod_err = "0"
            else:
                motivo = str(mensaje_servidor) if mensaje_servidor else "Rechazada por TopstepX"
                cod_err = str(error_code) if error_code else "N/A"
                estado = f"RECHAZADA: {motivo}"
            
            fieldnames = [
                'Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                'Precio_Entrada_Real', 'Precio_Salida_Real', 'SL_Ticks', 'TP_Ticks',
                'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
            ]
            
            with open(CSV_PATH, mode='a', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=';')
                if not archivo_existe:
                    writer.writeheader()
                
                writer.writerow({
                    'Fecha': fecha_actual,
                    'Hora': hora_actual,
                    'Simbolo': simbolo,
                    'Estado': estado,
                    'Direccion': direccion,
                    'Precio_Mercado': f"{precio:.2f}",
                    'Precio_Entrada_Real': f"{precio:.2f}" if exito else "N/A",
                    'Precio_Salida_Real': "PENDIENTE" if exito else "N/A",
                    'SL_Ticks': ticks_sl,
                    'TP_Ticks': ticks_tp,
                    'PnL_Bruto_USD': "0.00",
                    'Comisiones_USD': "0.00",
                    'PnL_Neto_USD': "0.00",
                    'Order_ID': order_id,
                    'Exit_Order_ID': "PENDIENTE" if exito else "N/A",
                    'Motivo_Rechazo': motivo if not exito else "N/A",
                    'Codigo_Error': cod_err if not exito else "N/A",
                    'Condicion_Tecnica': condicion,
                    'Ultima_Actualizacion': f"{fecha_actual} {hora_actual}"
                })
                
            self.log(f"💾 Registro guardado en CSV ({estado}).", "SYSTEM", simbolo)
        except Exception as e:
            self.log(f"❌ Error al escribir en el CSV: {e}", "ERROR", simbolo)

    def sync_real_data_from_topstep(self) -> Dict[str, Any]:
        """
        Consulta periódica (cada 1 hora y bajo demanda) al API de TopstepX para actualizar
        el archivo CSV y las estadísticas con los datos reales de operaciones,
        precios de ejecución, comisiones, balance real y PnL neto/bruto.
        """
        now = time.time()
        self.last_sync_time = now
        self.next_sync_time = now + self.sync_interval_seconds
        sync_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        next_str = datetime.fromtimestamp(self.next_sync_time).strftime('%Y-%m-%d %H:%M:%S')

        if not self.account_id:
            try:
                acc = self.client.get_practice_account()
                self.account_id = acc["id"]
                self.account_name = acc["name"]
            except Exception as e:
                self.log(f"⚠️ Error al obtener cuenta para sincronización: {e}", "ERROR")
                return self.stats

        # 1. Obtener balance real desde TopstepX
        try:
            real_balance = self.client.get_account_balance(self.account_id)
        except Exception:
            real_balance = 0.0

        # 2. Obtener fills reales desde TopstepX (últimos 30 días)
        try:
            raw_trades = self.client.search_trades(self.account_id, days_back=30)
        except Exception as e:
            self.log(f"⚠️ Error al consultar Trade/search de TopstepX: {e}", "ERROR")
            raw_trades = []

        # 3. Emparejar operaciones (Round-Trips) cronológicamente
        raw_trades.sort(key=lambda x: x.get('creationTimestamp', ''))
        paired_trades = []
        open_fills_by_contract: Dict[str, List[Dict[str, Any]]] = {}

        for t in raw_trades:
            cid = t.get('contractId', '')
            pnl = t.get('profitAndLoss')
            
            if pnl is None:
                # Opening fill (inicia posición)
                if cid not in open_fills_by_contract:
                    open_fills_by_contract[cid] = []
                open_fills_by_contract[cid].append(t)
            else:
                # Closing fill (cierra posición con PnL realizado)
                opening = None
                if cid in open_fills_by_contract and len(open_fills_by_contract[cid]) > 0:
                    opening = open_fills_by_contract[cid].pop(0)
                    
                entry_price = float(opening['price']) if opening and opening.get('price') is not None else float(t['price'])
                entry_side = opening['side'] if opening and opening.get('side') is not None else (1 if t.get('side') == 0 else 0)
                entry_ts_str = opening.get('creationTimestamp', t.get('creationTimestamp', ''))
                order_id = str(opening['orderId']) if opening and opening.get('orderId') else "N/A"
                exit_order_id = str(t.get('orderId', 'N/A'))
                
                try:
                    dt_entry = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
                    fecha_str = dt_entry.strftime('%Y-%m-%d')
                    hora_str = dt_entry.strftime('%H:%M:%S')
                except Exception:
                    fecha_str = datetime.now().strftime('%Y-%m-%d')
                    hora_str = datetime.now().strftime('%H:%M:%S')

                fees = float(opening.get('fees', 0.0) if opening else 0.0) + float(t.get('fees', 0.0) or 0.0)
                comms = float(opening.get('commissions', 0.0) if opening else 0.0) + float(t.get('commissions', 0.0) or 0.0)
                total_fees_comms = round(fees + comms, 2)
                gross_pnl = float(pnl)
                net_pnl = round(gross_pnl - total_fees_comms, 2)
                
                direccion = "LONG" if entry_side == 0 else "SHORT"
                sym = self.resolve_symbol_from_contract(cid)
                
                if gross_pnl > 0:
                    estado_str = f"CERRADA (+${gross_pnl:,.2f} USD)"
                elif gross_pnl < 0:
                    estado_str = f"CERRADA (-${abs(gross_pnl):,.2f} USD)"
                else:
                    estado_str = "CERRADA ($0.00 USD)"

                paired_trades.append({
                    "Fecha": fecha_str,
                    "Hora": hora_str,
                    "Simbolo": sym,
                    "Estado": estado_str,
                    "Direccion": direccion,
                    "Precio_Mercado": f"{entry_price:.2f}",
                    "Precio_Entrada_Real": f"{entry_price:.2f}",
                    "Precio_Salida_Real": f"{float(t['price']):.2f}",
                    "SL_Ticks": "10",
                    "TP_Ticks": "Dinámico",
                    "PnL_Bruto_USD": f"{gross_pnl:.2f}",
                    "Comisiones_USD": f"{total_fees_comms:.2f}",
                    "PnL_Neto_USD": f"{net_pnl:.2f}",
                    "Order_ID": order_id,
                    "Exit_Order_ID": exit_order_id,
                    "Motivo_Rechazo": "N/A",
                    "Codigo_Error": "N/A",
                    "Condicion_Tecnica": "Rechazo VWAP TopstepX Confirmado",
                    "Ultima_Actualizacion": sync_str
                })

        # Incluir posiciones que siguen abiertas
        for cid, open_list in open_fills_by_contract.items():
            for opening in open_list:
                sym = self.resolve_symbol_from_contract(cid)
                side_str = "LONG" if opening.get('side') == 0 else "SHORT"
                p_in = float(opening.get('price', 0.0))
                oid = str(opening.get('orderId', 'N/A'))
                try:
                    dt_o = datetime.fromisoformat(opening.get('creationTimestamp', '').replace("Z", "+00:00"))
                    f_o = dt_o.strftime('%Y-%m-%d')
                    h_o = dt_o.strftime('%H:%M:%S')
                except Exception:
                    f_o = datetime.now().strftime('%Y-%m-%d')
                    h_o = datetime.now().strftime('%H:%M:%S')

                paired_trades.append({
                    "Fecha": f_o,
                    "Hora": h_o,
                    "Simbolo": sym,
                    "Estado": "ABIERTA (En Curso)",
                    "Direccion": side_str,
                    "Precio_Mercado": f"{p_in:.2f}",
                    "Precio_Entrada_Real": f"{p_in:.2f}",
                    "Precio_Salida_Real": "N/A",
                    "SL_Ticks": "10",
                    "TP_Ticks": "Dinámico",
                    "PnL_Bruto_USD": "0.00",
                    "Comisiones_USD": f"{(float(opening.get('fees', 0.0)) + float(opening.get('commissions', 0.0))):.2f}",
                    "PnL_Neto_USD": "0.00",
                    "Order_ID": oid,
                    "Exit_Order_ID": "PENDIENTE",
                    "Motivo_Rechazo": "N/A",
                    "Codigo_Error": "N/A",
                    "Condicion_Tecnica": "Posición en mercado TopstepX",
                    "Ultima_Actualizacion": sync_str
                })

        # 4. Sincronizar y actualizar con el CSV en disco
        existing_rows = []
        if os.path.isfile(CSV_PATH):
            try:
                with open(CSV_PATH, mode='r', encoding='utf-8') as f:
                    r = csv.DictReader(f, delimiter=';')
                    for row in r:
                        existing_rows.append(row)
            except Exception:
                existing_rows = []

        existing_by_order = {}
        rejected_rows = []
        for row in existing_rows:
            oid = str(row.get("Order_ID", "")).strip()
            if oid and oid != "N/A":
                existing_by_order[oid] = row
            elif "RECHAZADA" in (row.get("Estado") or "").upper():
                rejected_rows.append(row)

        final_csv_rows = []
        for pt in paired_trades:
            oid = pt["Order_ID"]
            if oid in existing_by_order:
                old = existing_by_order[oid]
                if old.get("Condicion_Tecnica") and "Rechazo VWAP" in old["Condicion_Tecnica"]:
                    pt["Condicion_Tecnica"] = old["Condicion_Tecnica"]
                if old.get("SL_Ticks"):
                    pt["SL_Ticks"] = old["SL_Ticks"]
                if old.get("TP_Ticks"):
                    pt["TP_Ticks"] = old["TP_Ticks"]
                if old.get("Motivo_Rechazo"):
                    pt["Motivo_Rechazo"] = old["Motivo_Rechazo"]
                if old.get("Codigo_Error"):
                    pt["Codigo_Error"] = old["Codigo_Error"]
            final_csv_rows.append(pt)

        for r_row in rejected_rows:
            for col in ['Precio_Entrada_Real', 'Precio_Salida_Real', 'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error']:
                if col not in r_row or not r_row[col]:
                    r_row[col] = "0.00" if "USD" in col else "N/A"
            if "Ultima_Actualizacion" not in r_row:
                r_row["Ultima_Actualizacion"] = sync_str
            final_csv_rows.append(r_row)

        fieldnames = [
            'Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
            'Precio_Entrada_Real', 'Precio_Salida_Real', 'SL_Ticks', 'TP_Ticks',
            'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
            'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
        ]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(CSV_PATH)), exist_ok=True)
            with open(CSV_PATH, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';')
                writer.writeheader()
                for row in final_csv_rows:
                    clean_row = {k: row.get(k, "N/A") for k in fieldnames}
                    writer.writerow(clean_row)
        except Exception as e:
            self.log(f"⚠️ Error escribiendo CSV sincronizado: {e}", "ERROR")

        # 5. Computar estadísticas matemáticas estrictamente de datos REALES
        total_closed = 0
        winning = 0
        losing = 0
        total_gross_pnl = 0.0
        total_fees = 0.0
        total_net_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        by_sym = {sym: {"total": 0, "executed": 0, "longs": 0, "shorts": 0, "net_pnl": 0.0} for sym in SUPPORTED_SYMBOLS}

        for pt in paired_trades:
            if "CERRADA" in pt["Estado"]:
                total_closed += 1
                g_pnl = float(pt["PnL_Bruto_USD"])
                n_pnl = float(pt["PnL_Neto_USD"])
                comm = float(pt["Comisiones_USD"])
                
                total_gross_pnl += g_pnl
                total_fees += comm
                total_net_pnl += n_pnl
                
                if g_pnl > 0:
                    winning += 1
                    gross_profit += g_pnl
                elif g_pnl < 0:
                    losing += 1
                    gross_loss += abs(g_pnl)
                    
                sym = pt["Simbolo"]
                if sym in by_sym:
                    by_sym[sym]["total"] += 1
                    by_sym[sym]["executed"] += 1
                    by_sym[sym]["net_pnl"] += n_pnl
                    if pt["Direccion"] == "LONG":
                        by_sym[sym]["longs"] += 1
                    else:
                        by_sym[sym]["shorts"] += 1

        win_rate = round((winning / total_closed * 100.0), 1) if total_closed > 0 else 0.0
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)

        with self.lock:
            self.stats = {
                "total_trades": len(final_csv_rows),
                "executed_trades": total_closed,
                "rejected_trades": len(rejected_rows),
                "longs": sum(s["longs"] for s in by_sym.values()),
                "shorts": sum(s["shorts"] for s in by_sym.values()),
                "real_balance": real_balance,
                "gross_pnl_usd": round(total_gross_pnl, 2),
                "total_fees_usd": round(total_fees, 2),
                "net_pnl_usd": round(total_net_pnl, 2),
                "win_rate": win_rate,
                "profit_factor": pf,
                "winning_trades": winning,
                "losing_trades": losing,
                "last_sync": sync_str,
                "next_sync": next_str,
                "by_symbol": by_sym
            }

        pnl_symbol_str = "+" if total_net_pnl >= 0 else ""
        self.log(
            f"🔄 Sincronización TopstepX exitosa: Balance: ${real_balance:,.2f} | PnL Neto Real: {pnl_symbol_str}${total_net_pnl:,.2f} USD | Operaciones: {total_closed} (Win Rate: {win_rate}%)",
            "SYSTEM"
        )
        return self.stats

    def load_operations_history(self) -> List[Dict[str, Any]]:
        """Lee el CSV de operaciones institucional."""
        trades = []
        if not os.path.isfile(CSV_PATH):
            return trades
            
        try:
            with open(CSV_PATH, mode='r', encoding='utf-8') as file:
                reader = csv.DictReader(file, delimiter=';')
                for row in reader:
                    if "Simbolo" not in row or not row["Simbolo"]:
                        row["Simbolo"] = "ES"
                    trades.append(row)
        except Exception as e:
            print(f"Error reading CSV: {e}")
            
        return trades

    # ==========================================
    # MULTI-SYMBOL BACKGROUND ENGINE LOOP
    # ==========================================
    def run_loop(self):
        self.log("⏳ Motor Multi-Símbolo Activo. Telemetría y scanner iniciados...", "SYSTEM")
        if self.last_sync_time == 0:
            self.last_sync_time = time.time()
            self.next_sync_time = time.time() + self.sync_interval_seconds
        
        while not self.stop_event.is_set():
            try:
                # 0. Sincronización horaria automática con TopstepX (cada 1 hora = 3600 segundos)
                if time.time() - self.last_sync_time >= self.sync_interval_seconds:
                    self.log("⏰ [SINCRONIZACIÓN HORARIA] Actualizando balance y operaciones reales con TopstepX...", "SYSTEM")
                    try:
                        self.sync_real_data_from_topstep()
                    except Exception as e:
                        self.log(f"⚠️ Error en sincronización horaria de TopstepX: {e}", "ERROR")

                # 1. Monitorear los símbolos con control inteligente de tasa (Rate Limit)
                symbols_to_query = list(SUPPORTED_SYMBOLS.keys())
                now_poll = time.time()
                
                for sym in symbols_to_query:
                    if self.stop_event.is_set():
                        break
                        
                    state = self.symbol_states[sym]
                    contract_id = state.get("contract_id")
                    if not contract_id:
                        continue
                        
                    is_enabled = state.get("enabled", False)
                    is_active_view = (sym == self.active_symbol)
                    
                    # Control inteligente de frecuencia:
                    # - Símbolos activos o en pantalla: sondeo prioritario cada 2.0 segundos (alta frecuencia segundo a segundo).
                    # - Símbolos inactivos (apagados): sondeo de baja frecuencia cada 60 segundos.
                    min_interval = 2.0 if (is_enabled or is_active_view) else 60.0
                    if (now_poll - state.get("last_poll_time", 0.0)) < min_interval:
                        continue
                    state["last_poll_time"] = now_poll
                    
                    # Descargamos historial de 350 velas para asegurar la sesión completa CME (reseteo 18:00 ET)
                    limit_bars = 350
                    
                    df = self.client.retrieve_bars(contract_id, unit=2, unit_number=5, limit=limit_bars, days_back=2)
                    if not df.empty:
                        df = self.calcular_vwap_sesion(df)
                        
                        with self.lock:
                            state["latest_df"] = df
                            vela_actual = df.iloc[-1]
                            dist_vwap = vela_actual['Close'] - vela_actual['VWAP']
                            
                            cd_left = max(0, int(state["cooldown_until"] - time.time()))
                            state["telemetry"] = {
                                "symbol": sym,
                                "contract": state["contract_name"],
                                "timestamp": vela_actual['Timestamp'].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "close": float(vela_actual['Close']),
                                "open": float(vela_actual['Open']),
                                "high": float(vela_actual['High']),
                                "low": float(vela_actual['Low']),
                                "volume": int(vela_actual['Volume']),
                                "vwap": float(vela_actual['VWAP']),
                                "upper_band": float(vela_actual['Upper_Band']),
                                "lower_band": float(vela_actual['Lower_Band']),
                                "distance_vwap": float(dist_vwap),
                                "enabled": is_enabled,
                                "cooldown_remaining": cd_left
                            }
                        
                        # Emitir telemetría para todos los símbolos habilitados o para la vista activa
                        if is_enabled or is_active_view:
                            self.log(f"📊 {sym}: {vela_actual['Close']:,.2f} | 🔴 Sup: {vela_actual['Upper_Band']:,.2f} | 🔵 VWAP: {vela_actual['VWAP']:,.2f} | 🟢 Inf: {vela_actual['Lower_Band']:,.2f} | Dist: {dist_vwap:+.2f} pts", "TELEMETRY", sym)
                        
                        # 2. Evaluación de Trading Autónomo en Tiempo Real (mientras la vela está en proceso)
                        if is_enabled:
                            vela_actual = df.iloc[-1]
                            vela_ts = vela_actual['Timestamp']
                            time_str = vela_ts.strftime('%H:%M')
                            
                            # Si detectamos una nueva vela 5m iniciada, emitimos notificación
                            if state.get("current_active_candle_ts") != vela_ts:
                                state["current_active_candle_ts"] = vela_ts
                                self.log(f"🕯️ [{sym}] Vela 5m activa ({time_str} UTC). Escaneando rechazo por mecha en VWAP segundo a segundo...", "SETUP", sym)

                            # 1. Filtro institucional de sesión CME (primeras 2 horas tras la apertura: 18:00-20:00 ET y receso diario)
                            is_operable, motivo_horario = self.es_horario_operable()
                            if not is_operable:
                                if state.get("last_inoperable_reason") != motivo_horario:
                                    state["last_inoperable_reason"] = motivo_horario
                                    self.log(f"⏸️ [{sym}] {motivo_horario}. Entradas suspendidas.", "SETUP", sym)
                                continue
                            else:
                                state["last_inoperable_reason"] = None

                            # 2. Filtro de sobreexposición: Máximo 1 operación abierta por símbolo a la vez
                            if self.tiene_posicion_abierta(sym):
                                continue

                            # 3. Control de ejecución: Máximo 1 operación por vela
                            if state.get("last_executed_candle_ts") == vela_ts:
                                continue

                            # 4. Cooldown activo
                            if time.time() < state["cooldown_until"]:
                                continue

                            # 5. Evaluación continua segundo a segundo: confirmación de mecha en VWAP
                            setup = self.verificar_setup_vwap(df, sym)
                            if setup:
                                state["last_executed_candle_ts"] = vela_ts
                                self.log(f"⚡ [{sym}] ¡Mecha de rechazo en VWAP confirmada en tiempo real ({time_str} UTC)! Abriendo {setup['direccion']}...", "ORDER", sym)
                                self.ejecutar_orden(setup, df, sym)
                    
                    # Pausa de cortesía entre consultas individuales para no saturar la API
                    time.sleep(0.35)
                    
            except requests.exceptions.HTTPError as he:
                if he.response is not None and he.response.status_code == 429:
                    self.log("⏸️ TopstepX Rate Limit temporal alcanzado (HTTP 429). Pausando 6 segundos...", "SYSTEM")
                    time.sleep(6)
                else:
                    self.log(f"⚠️ Excepción en el ciclo multi-símbolo: {he}", "ERROR", "ALL")
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    self.log("⏸️ TopstepX Rate Limit temporal alcanzado (HTTP 429). Pausando 6 segundos...", "SYSTEM")
                    time.sleep(6)
                else:
                    self.log(f"⚠️ Excepción en el ciclo multi-símbolo: {e}", "ERROR", "ALL")
                
            # Intervalo dinámico entre rondas completas de sondeo en vivo (1 segundo)
            if not self.stop_event.is_set():
                time.sleep(1)

        self.log("🛑 Motor multi-símbolo detenido correctamente.", "SYSTEM", "ALL")

    # ==========================================
    # LIFECYCLE MANAGEMENT
    # ==========================================
    def start(self):
        if self.running:
            self.log("⚠️ El motor ya está activo.", "SYSTEM")
            return
            
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        self.log("⚡ Motor multi-símbolo iniciado en segundo plano.", "SYSTEM")

    def stop(self):
        if not self.running:
            return
            
        self.log("🛑 Deteniendo motor multi-símbolo...", "SYSTEM")
        self.running = False
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def emergency_flatten(self, symbol_key: Optional[str] = None) -> Dict[str, Any]:
        """Cierra inmediatamente posiciones en el símbolo o en todos."""
        sym = (symbol_key or self.active_symbol).upper()
        contract_id = self.symbol_states.get(sym, {}).get("contract_id")
        self.log(f"⚠️ [KILL SWITCH] Disparando cierre de emergencia para {sym}...", "ALERT", sym)
        try:
            res = self.client.close_contract_position(self.account_id, contract_id)
            self.log(f"🛡️ Cierre ejecutado: {res}", "ALERT", sym)
            return res
        except Exception as e:
            self.log(f"❌ Error en cierre: {e}", "ERROR", sym)
            return {"success": False, "error": str(e)}

    # ==========================================
    # DATA SERIALIZATION FOR API & UI
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
        
        # Matrix overview of all symbols
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
            "next_sync": self.stats.get("next_sync", "N/A")
        }
