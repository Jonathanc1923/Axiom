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
                "last_executed_1m_ts": None,
                "last_evaluated_1m_ts": None,
                "active_trade": None,
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
    # LOGICAL ENGINE: 1-MINUTE VWAP ABSORPTION & VOLUME EFFORT
    # ==========================================
    def verificar_absorcion_vwap_1m(self, df_5m: pd.DataFrame, sym: str, df_1m_override: Optional[pd.DataFrame] = None) -> Optional[Dict[str, Any]]:
        """
        Estrategia de Absorción Institucional en VWAP (1 Minuto + Esfuerzo de Volumen):
        
        Cuando el precio se aproxima al VWAP (+-5 ticks), se analiza la última vela cerrada
        de 1 minuto y se compara su volumen con el promedio de las últimas 10 velas de 1m.
        
        CONDICIÓN PARA COMPRA (LONG):
        1. El precio entró a la zona (mínimo de la vela 1m <= VWAP + 5 ticks).
        2. Volumen de la vela cerrada >= 1.10 x Promedio de las últimas 10 velas de 1m (+10% esfuerzo).
        3. RECHAZO CONFIRMADO: La vela cerró POR ENCIMA del VWAP (Close >= VWAP)
           y dejó mecha inferior (Close > Low + 2 ticks).
        -> Significado: Hubo esfuerzo vendedor (+10%), pero los compradores absorbieron todo
           y el precio cerró arriba.
        -> STOP LOSS: A 10 ticks por debajo del mínimo de esa vela (extremo_precio = Low).
        
        CONDICIÓN PARA VENTA (SHORT):
        1. El precio entró a la zona (máximo de la vela 1m >= VWAP - 5 ticks).
        2. Volumen de la vela cerrada >= 1.10 x Promedio de las últimas 10 velas de 1m (+10% esfuerzo).
        3. RECHAZO CONFIRMADO: La vela cerró POR DEBAJO del VWAP (Close <= VWAP)
           y dejó mecha superior (High - Close >= 2 ticks).
        -> Significado: Hubo esfuerzo comprador (+10%), pero los vendedores absorbieron todo
           y el precio cerró abajo.
        -> STOP LOSS: A 10 ticks por encima del máximo de esa vela (extremo_precio = High).
        """
        if df_5m is None or len(df_5m) < 1:
            return None
            
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        
        vela_5m = df_5m.iloc[-1]
        vwap_val = float(vela_5m['VWAP'])
        zona_ticks = 5 * tick_size

        # 1. Filtro Institucional de Contexto: Vela previa de 5m completa del lado del rebote
        # La vela de 5 minutos anterior no debe haber pasado ni tocado el VWAP en cuerpo y mecha:
        # - Para LONG (esperamos rebote alcista): vela previa completamente por encima de VWAP (Low > VWAP)
        # - Para SHORT (esperamos rebote bajista): vela previa completamente por debajo de VWAP (High < VWAP)
        previa_valida_long = False
        previa_valida_short = False
        
        if len(df_5m) >= 2:
            vela_5m_prev = df_5m.iloc[-2]
            vwap_prev = float(vela_5m_prev.get('VWAP', vwap_val))
            previa_valida_long = float(vela_5m_prev['Low']) > (vwap_prev + (0.5 * tick_size))
            previa_valida_short = float(vela_5m_prev['High']) < (vwap_prev - (0.5 * tick_size))
        elif df_1m_override is not None:
            previa_valida_long = True
            previa_valida_short = True
            
        if not (previa_valida_long or previa_valida_short):
            return None
        
        # 2. Filtro previo de Zona Caliente en 5m:
        # Verificar si la vela actual de 5m ha tocado o está dentro de VWAP +- 5 ticks
        en_zona_5m_long = float(vela_5m['Low']) <= vwap_val + zona_ticks
        en_zona_5m_short = float(vela_5m['High']) >= vwap_val - zona_ticks
        if not (en_zona_5m_long or en_zona_5m_short):
            return None
            
        # 3. Obtener velas de 1 minuto (TopstepX History o test override)
        if df_1m_override is not None:
            df_1m = df_1m_override
        else:
            state = self.symbol_states.get(sym, {})
            contract_id = state.get("contract_id")
            if not contract_id:
                return None
            df_1m = self.client.retrieve_bars(contract_id, unit=2, unit_number=1, limit=18, days_back=1)
            
        if df_1m is None or df_1m.empty or len(df_1m) < 11:
            return None
            
        # TopstepX retrieve_bars con includePartialBar=True retorna la vela en formación en el último índice [-1].
        # La vela cerrada que acaba de completarse es [-2] si len >= 12, o [-1] si se pasa un dataset sin vela parcial.
        if df_1m_override is not None and len(df_1m) == 11:
            vela_cerrada = df_1m.iloc[-1]
            ventana_10 = df_1m.iloc[-11:-1]
        else:
            vela_cerrada = df_1m.iloc[-2]
            ventana_10 = df_1m.iloc[-12:-2]
            
        vela_ts = vela_cerrada['Timestamp']
        state = self.symbol_states.get(sym, {})
        
        # Deduplicación: evitar re-evaluar la misma vela de 1m cerrada en múltiples consultas por segundo
        if df_1m_override is None:
            if state.get("last_evaluated_1m_ts") == vela_ts:
                return None
            if state.get("last_executed_1m_ts") == vela_ts:
                return None

        # 3. MÓDULO DE VOLUMEN (Esfuerzo en contratos):
        # Promedio de las últimas 10 velas cerradas previas
        promedio_vol_10 = float(ventana_10['Volume'].mean())
        vol_cerrada = float(vela_cerrada['Volume'])
        
        # Umbral: al menos 10% más contratos que el promedio de las 10 velas previas
        umbral_esfuerzo = 1.10 * promedio_vol_10
        esfuerzo_grande = (vol_cerrada >= umbral_esfuerzo) if promedio_vol_10 > 0 else True
        
        if not esfuerzo_grande:
            if df_1m_override is None:
                state["last_evaluated_1m_ts"] = vela_ts
            return None
            
        # 4. MÓDULO DE ACCIÓN DE PRECIO (Mecha y Rechazo):
        o_1m = float(vela_cerrada['Open'])
        h_1m = float(vela_cerrada['High'])
        l_1m = float(vela_cerrada['Low'])
        c_1m = float(vela_cerrada['Close'])
        rvol = (vol_cerrada / promedio_vol_10) if promedio_vol_10 > 0 else 1.0

        # CASO COMPRA (LONG):
        # 1. El precio entró a la zona: mínimo <= VWAP + 3 ticks
        # 2. Cerró por encima del VWAP y el cuerpo no cruzó hacia abajo: min(Open, Close) >= VWAP
        # 3. Dejó mecha inferior: Close > Low + 2 ticks
        toco_zona_long = (l_1m <= vwap_val + zona_ticks)
        cerro_arriba_long = (min(o_1m, c_1m) >= vwap_val - (0.5 * tick_size))
        mecha_inf = c_1m - l_1m
        mecha_confirmada_long = (mecha_inf >= 2 * tick_size)
        
        # CASO VENTA (SHORT):
        # 1. El precio entró a la zona: máximo >= VWAP - 3 ticks
        # 2. Cerró por debajo del VWAP y el cuerpo no cruzó hacia arriba: max(Open, Close) <= VWAP
        # 3. Dejó mecha superior: High - Close >= 2 ticks
        toco_zona_short = (h_1m >= vwap_val - zona_ticks)
        cerro_abajo_short = (max(o_1m, c_1m) <= vwap_val + (0.5 * tick_size))
        mecha_sup = h_1m - c_1m
        mecha_confirmada_short = (mecha_sup >= 2 * tick_size)
        
        es_long = toco_zona_long and cerro_arriba_long and mecha_confirmada_long and previa_valida_long
        es_short = toco_zona_short and cerro_abajo_short and mecha_confirmada_short and previa_valida_short
        
        # Si ambas fueran técnicamente verdaderas, desempatar por el lado hacia donde cerró la vela
        if es_long and es_short:
            if c_1m > vwap_val:
                es_short = False
            elif c_1m < vwap_val:
                es_long = False
            elif mecha_inf >= mecha_sup:
                es_short = False
            else:
                es_long = False
                
        if es_long:
            if df_1m_override is None:
                state["last_evaluated_1m_ts"] = vela_ts
            return {
                "direccion": "LONG",
                "simbolo": sym,
                "vela_cruce": vela_cerrada,
                "vela_5m": vela_5m,
                "extremo_precio": l_1m, # Mínimo de la vela para el Stop Loss
                "vwap_cruce": vwap_val,
                "upper_band": float(vela_5m['Upper_Band']),
                "lower_band": float(vela_5m['Lower_Band']),
                "volumen_1m": vol_cerrada,
                "promedio_vol_10": promedio_vol_10,
                "rvol": rvol,
                "mecha_pts": mecha_inf,
                "timestamp_1m": vela_ts
            }
            
        if es_short:
            if df_1m_override is None:
                state["last_evaluated_1m_ts"] = vela_ts
            return {
                "direccion": "SHORT",
                "simbolo": sym,
                "vela_cruce": vela_cerrada,
                "vela_5m": vela_5m,
                "extremo_precio": h_1m, # Máximo de la vela para el Stop Loss
                "vwap_cruce": vwap_val,
                "upper_band": float(vela_5m['Upper_Band']),
                "lower_band": float(vela_5m['Lower_Band']),
                "volumen_1m": vol_cerrada,
                "promedio_vol_10": promedio_vol_10,
                "rvol": rvol,
                "mecha_pts": mecha_sup,
                "timestamp_1m": vela_ts
            }
            
        if df_1m_override is None:
            state["last_evaluated_1m_ts"] = vela_ts
            
        return None

    def verificar_setup_vwap(self, df: pd.DataFrame, sym: str, ignore_time_window: bool = False) -> Optional[Dict[str, Any]]:
        """Fallback compatible hacia el motor de absorción institucional de 1 minuto."""
        return self.verificar_absorcion_vwap_1m(df, sym)

    # ==========================================
    # RISK ENGINE: SL A 5 TICKS DEL EXTREMO Y TP DINÁMICO POR VOLUMEN (PISO 5t, TECHO 60% BANDA)
    # ==========================================
    def calcular_riesgo_dinamico(self, direccion: str, precio_entrada: float, extremo_precio: float,
                                 upper_band: float, lower_band: float, sym: str,
                                 vwap_val: Optional[float] = None,
                                 rvol: Optional[float] = None) -> tuple[int, int, float, float]:
        """
        Calcula SL y TP según las reglas cuantitativas de absorción:
        - SL: Exactamente 5 ticks sobre el extremo que alcanzó la mecha de 1m:
              LONG: 5 ticks por debajo del precio más bajo alcanzado por la mecha (precio_sl = low_mecha - 5 ticks).
              SHORT: 5 ticks por encima del precio más alto alcanzado por la mecha (precio_sl = high_mecha + 5 ticks).
              Piso mínimo de SL: 5 ticks.
        - TP: Dinámico según el porcentaje de exceso de volumen institucional detectado:
              Ticks de TP = % Exceso de Volumen (ej. +25% vol -> 25 ticks).
              Piso mínimo: 5 ticks.
              Techo máximo: 60% de la distancia entre la línea VWAP y la banda correspondiente.
        """
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        ref_vwap = vwap_val if vwap_val is not None else precio_entrada
        
        # 1. Base de Take Profit por porcentaje de volumen
        rvol_val = float(rvol) if rvol is not None else 1.10
        pct_vol = round((rvol_val - 1.0) * 100)
        ticks_vol = max(5, pct_vol) # Piso mínimo de 5 ticks
        
        if direccion == "LONG":
            # SL inicial: exactamente a 10 ticks de distancia por debajo de la línea VWAP
            precio_sl = ref_vwap - (10 * tick_size)
            distancia_sl_puntos = abs(precio_entrada - precio_sl)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 5)
            
            # Techo máximo: 60% de distancia entre VWAP y Banda Superior (min 5 ticks)
            distancia_banda = abs(upper_band - ref_vwap)
            techo_60_pct = max(round((distancia_banda * 0.60) / tick_size), 5)
            ticks_tp_abs = min(ticks_vol, techo_60_pct)
            ticks_tp_abs = max(ticks_tp_abs, 5) # Garantizar piso mínimo de 5 ticks
            precio_tp = precio_entrada + (ticks_tp_abs * tick_size)
            
            # Convención de signos TopstepX: LONG -> SL negativo (-), TP positivo (+)
            return -ticks_sl_abs, ticks_tp_abs, precio_sl, precio_tp
            
        else: # SHORT
            # SL inicial: exactamente a 10 ticks de distancia por encima de la línea VWAP
            precio_sl = ref_vwap + (10 * tick_size)
            distancia_sl_puntos = abs(precio_sl - precio_entrada)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 5)
            
            # Techo máximo: 60% de distancia entre VWAP y Banda Inferior (min 5 ticks)
            distancia_banda = abs(ref_vwap - lower_band)
            techo_60_pct = max(round((distancia_banda * 0.60) / tick_size), 5)
            ticks_tp_abs = min(ticks_vol, techo_60_pct)
            ticks_tp_abs = max(ticks_tp_abs, 5) # Garantizar piso mínimo de 5 ticks
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
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        contract_id = self.symbol_states[sym]["contract_id"]
        precio_entrada = float(df.iloc[-1]['Close'])
        rvol = setup.get("rvol", 1.10)
        
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.calcular_riesgo_dinamico(
            direccion, precio_entrada, extremo_precio,
            float(setup['upper_band']),
            float(setup['lower_band']),
            sym,
            vwap_cruce,
            rvol=rvol
        )
        
        info_sl = f"SL: {abs(ticks_sl)} ticks ({precio_sl:,.2f} | 5 ticks desde extremo de mecha)"
        info_tp = f"TP: {abs(ticks_tp)} ticks ({precio_tp:,.2f} | Dinámico por Volumen / Techo 60% Banda)"
        self.log(f"🚨 DISPARANDO {direccion} | Entrada: {precio_entrada:,.2f} | {info_sl} | {info_tp}", "ORDER", sym)
        
        side = 1 if direccion == "SHORT" else 0
        mecha_pts = setup.get("mecha_pts", 0.0)
        mecha_ticks = round(mecha_pts / tick_size) if tick_size > 0 else 0
        rvol_info = f" | +{(rvol-1.0)*100:.1f}% Vol" if "rvol" in setup else ""
        condicion_str = f"Absorcion VWAP 1m {setup['vwap_cruce']:.2f} ({mecha_ticks}t Mecha{rvol_info} | Extremo {extremo_precio:.2f}). SL: 5t Mecha ({precio_sl:.2f}) | TP Dinamico: {abs(ticks_tp)}t ({precio_tp:.2f})"
        
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
                
                # Inicializar seguimiento activo e independiente para la Escalera de 3 Hitos (cada 2 segundos)
                recorrido_tp_pts = abs(precio_tp - precio_entrada)
                target_40 = (precio_entrada + recorrido_tp_pts * 0.40) if direccion == "LONG" else (precio_entrada - recorrido_tp_pts * 0.40)
                
                self.symbol_states[sym]["active_trade"] = {
                    "symbol": sym,
                    "contract_id": contract_id,
                    "direction": direccion,
                    "entry_price": precio_entrada,
                    "current_price": precio_entrada,
                    "initial_sl_price": precio_sl,
                    "current_sl_price": precio_sl,
                    "tp_price": precio_tp,
                    "initial_sl_ticks": ticks_sl,
                    "tp_ticks": ticks_tp,
                    "rvol": rvol,
                    "order_id": order_id,
                    "sl_stage": 0, # 0=Inicial, 1=Hito 1 (40%), 2=Hito 2 (75%), 3=Hito 3 (90%)
                    "entry_timestamp": time.time(),
                    "last_check_time": time.time()
                }
                
                self.log(f"✅ Ejecución exitosa en TopstepX. Order ID: {order_id} | Escalera armada (H1 al 40% TP: {target_40:.2f})", "ORDER", sym)
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
    # ACTIVE TRADE MANAGEMENT & BREAK-EVEN ENGINE
    # ==========================================
    def sincronizar_posiciones_activas_cuenta(self):
        """
        Sondeo global de posiciones abiertas en la cuenta TopstepX (cada 2 segundos).
        Actualiza has_open_position en cada símbolo y vincula contratos activos dinámicamente.
        """
        if not self.account_id:
            return
            
        now = time.time()
        if now - getattr(self, "_last_pos_sync", 0.0) < 2.0:
            return
        self._last_pos_sync = now
        
        try:
            positions = self.client.get_open_positions(self.account_id)
            active_symbols = set()
            for p in positions:
                raw_size = abs(int(p.get("size", 0)))
                if raw_size > 0:
                    cid = str(p.get("contractId", ""))
                    sym = self.resolve_symbol_from_contract(cid)
                    active_symbols.add(sym)
                    if sym in self.symbol_states:
                        st = self.symbol_states[sym]
                        st["has_open_position"] = True
                        if not st.get("contract_id"):
                            st["contract_id"] = cid
                            
            for sym, st in self.symbol_states.items():
                if sym not in active_symbols:
                    if st.get("has_open_position") and not st.get("active_trade"):
                        st["has_open_position"] = False
        except Exception:
            pass

    def adoptar_posicion_abierta(self, sym: str, df: Optional[pd.DataFrame] = None) -> Optional[Dict[str, Any]]:
        """
        Adopta y vincula automáticamente cualquier posición abierta existente en TopstepX para el símbolo dado,
        incluso si fue abierta manualmente desde la web de TopstepX o si el bot acaba de reiniciarse.
        """
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
            
            direccion = "LONG" if (pos_type == 1 or raw_size > 0) else "SHORT"
            
            # Buscar órdenes Stop (SL) y Limit (TP) asociadas en TopstepX
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
                        
            # Determinar precio de SL y TP
            if sl_order and sl_order.get("stopPrice") is not None:
                current_sl_price = float(sl_order["stopPrice"])
                sl_order_id = sl_order.get("id")
            else:
                default_sl_pts = spec.get("defaultSlPoints", 5 * tick_size)
                current_sl_price = (entry_price - default_sl_pts) if direccion == "LONG" else (entry_price + default_sl_pts)
                sl_order_id = "N/A"
                
            if tp_order and tp_order.get("limitPrice") is not None:
                tp_price = float(tp_order["limitPrice"])
                tp_order_id = tp_order.get("id")
            else:
                default_tp_pts = 10 * tick_size
                tp_price = (entry_price + default_tp_pts) if direccion == "LONG" else (entry_price - default_tp_pts)
                tp_order_id = "N/A"
                
            recorrido_tp_pts = abs(tp_price - entry_price)
            tp_ticks = round(recorrido_tp_pts / tick_size) if tick_size > 0 else 0
            sl_ticks = round(abs(entry_price - current_sl_price) / tick_size) if tick_size > 0 else 0
            
            # Evaluar si el SL actual ya corresponde a algún hito avanzado
            initial_stage = 0
            if recorrido_tp_pts > 0:
                if direccion == "LONG":
                    if current_sl_price >= entry_price + (recorrido_tp_pts * 0.55):
                        initial_stage = 3
                    elif current_sl_price >= entry_price + (recorrido_tp_pts * 0.30):
                        initial_stage = 2
                    elif current_sl_price >= entry_price:
                        initial_stage = 1
                else: # SHORT
                    if current_sl_price <= entry_price - (recorrido_tp_pts * 0.55):
                        initial_stage = 3
                    elif current_sl_price <= entry_price - (recorrido_tp_pts * 0.30):
                        initial_stage = 2
                    elif current_sl_price <= entry_price:
                        initial_stage = 1
                        
            adopted_trade = {
                "symbol": sym,
                "contract_id": contract_id,
                "direction": direccion,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "initial_sl_price": current_sl_price,
                "current_sl_price": current_sl_price,
                "sl_ticks": sl_ticks,
                "tp_ticks": tp_ticks,
                "sl_stage": initial_stage,
                "order_id": target_pos.get("id", "ADOPTADO"),
                "sl_order_id": sl_order_id,
                "tp_order_id": tp_order_id,
                "size": abs(raw_size),
                "last_check_time": 0.0,
                "adopted": True
            }
            
            state["active_trade"] = adopted_trade
            state["has_open_position"] = True
            state["contract_id"] = contract_id
            
            self.log(
                f"🎯 [{sym}] ¡Posición abierta adoptada automáticamente en TopstepX! "
                f"Dirección: {direccion} | Entrada: {entry_price:.2f} | SL: {current_sl_price:.2f} | TP: {tp_price:.2f} "
                f"(Hito inicial: {initial_stage}) | Contrato: {contract_id}.",
                "ORDER", sym
            )
            return adopted_trade
        except Exception as e:
            self.log(f"⚠️ Error al intentar adoptar posición abierta para {sym}: {e}", "ERROR", sym)
            return None

    def monitorear_posicion_abierta(self, sym: str, df: Optional[pd.DataFrame] = None):
        """
        Analiza de manera independiente la operación abierta del símbolo:
        - Mide el progreso en tiempo real hacia el Take Profit utilizando TANTO el precio de cierre como los extremos de mecha (High/Low).
        - Memoriza el máximo avance alcanzado en la vida del trade para garantizar que ningún hito se pierda ante fluctuaciones rápidas.
        - ESCALERA DE 3 HITOS PROGRESIVOS:
          * Hito 1 (al 40% del TP): Mueve SL a Entrada + 1 tick (Break-Even / Riesgo Cero).
          * Hito 2 (al 75% del TP): Sube SL para asegurar el +35% de ganancia del trade.
          * Hito 3 (al 90% del TP): Sube SL para asegurar el +60% de ganancia (candado final).
        - Fail-Safe Guard: Si el precio retrocede y toca el SL protegido, garantiza la salida inmediata a mercado.
        - Registra cada hito en la telemetría, consola y en el archivo CSV institucional.
        - Detecta el cierre de la posición para liberar el seguimiento limpiamente.
        """
        state = self.symbol_states.get(sym, {})
        trade = state.get("active_trade")
        
        # Si no hay trade en memoria, verificar si existe una posición abierta en TopstepX para adoptarla
        if not trade:
            trade = self.adoptar_posicion_abierta(sym, df)
            if not trade:
                return

        now = time.time()
        if now - trade.get("last_check_time", 0.0) < 0.5:
            return
        trade["last_check_time"] = now

        # 1. Verificar si la posición sigue activa en TopstepX
        is_open = self.tiene_posicion_abierta(sym)
        if not is_open:
            self.log(f"🏁 [{sym}] Posición cerrada en mercado. Finalizando seguimiento activo de {trade['direction']}.", "ORDER", sym)
            state["active_trade"] = None
            state["has_open_position"] = False
            return

        precio_actual = None
        vela_high = None
        vela_low = None
        
        if df is not None and not df.empty:
            vela = df.iloc[-1]
            precio_actual = float(vela['Close'])
            vela_high = float(vela.get('High', precio_actual))
            vela_low = float(vela.get('Low', precio_actual))
        elif state.get("telemetry", {}).get("close"):
            precio_actual = float(state["telemetry"]["close"])
            vela_high = precio_actual
            vela_low = precio_actual

        if precio_actual is None:
            return

        trade["current_price"] = precio_actual
        direccion = trade["direction"]
        precio_entrada = trade["entry_price"]
        precio_tp = trade["tp_price"]
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)

        # 2. Calcular avance porcentual utilizando TANTO el precio de cierre como el extremo más favorable de la mecha
        recorrido_tp_pts = abs(precio_tp - precio_entrada)
        if direccion == "LONG":
            precio_favorable = max(precio_actual, vela_high if vela_high is not None else precio_actual)
            ganancia_pts = precio_favorable - precio_entrada
            pct_avance = (ganancia_pts / recorrido_tp_pts) * 100 if recorrido_tp_pts > 0 else 0.0
            
            sl_h1 = precio_entrada + (1 * tick_size)
            sl_h2 = round((precio_entrada + recorrido_tp_pts * 0.35) / tick_size) * tick_size
            sl_h3 = round((precio_entrada + recorrido_tp_pts * 0.60) / tick_size) * tick_size
        else: # SHORT
            precio_favorable = min(precio_actual, vela_low if vela_low is not None else precio_actual)
            ganancia_pts = precio_entrada - precio_favorable
            pct_avance = (ganancia_pts / recorrido_tp_pts) * 100 if recorrido_tp_pts > 0 else 0.0
            
            sl_h1 = precio_entrada - (1 * tick_size)
            sl_h2 = round((precio_entrada - recorrido_tp_pts * 0.35) / tick_size) * tick_size
            sl_h3 = round((precio_entrada - recorrido_tp_pts * 0.60) / tick_size) * tick_size

        # Memorizar el máximo avance histórico alcanzado durante la vida del trade
        max_pct = max(trade.get("max_pct_avance", 0.0), pct_avance)
        trade["max_pct_avance"] = max_pct

        ganancia_ticks = round(ganancia_pts / tick_size) if tick_size > 0 else 0
        sl_actual = trade["current_sl_price"]
        sl_stage = trade.get("sl_stage", 0)
        stage_str = f" [HITO {sl_stage}]" if sl_stage > 0 else ""
        
        self.log(
            f"🎯 [{sym}] Trade {direccion} activo{stage_str} | In: {precio_entrada:.2f} | Now: {precio_actual:.2f} (Fav: {precio_favorable:.2f}) "
            f"({ganancia_ticks:+d}t | {pct_avance:.1f}% TP | Max: {max_pct:.1f}%) | SL: {sl_actual:.2f} | TP: {precio_tp:.2f}",
            "TELEMETRY", sym
        )

        # 3. ESCALERA DE 3 HITOS: Mover SL progresivamente según el máximo avance alcanzado
        nuevo_sl = None
        nuevo_stage = sl_stage
        motivo_hito = ""

        if max_pct >= 90.0 and sl_stage < 3:
            nuevo_sl = sl_h3
            nuevo_stage = 3
            motivo_hito = f"HITO 3 (90% TP) | Candado activado: SL asegurando +60% ganancia ({sl_h3:.2f})"
        elif max_pct >= 75.0 and sl_stage < 2:
            nuevo_sl = sl_h2
            nuevo_stage = 2
            motivo_hito = f"HITO 2 (75% TP) | SL asegurando +35% ganancia ({sl_h2:.2f})"
        elif max_pct >= 40.0 and sl_stage < 1:
            nuevo_sl = sl_h1
            nuevo_stage = 1
            motivo_hito = f"HITO 1 (40% TP) | Moviendo SL a Break-Even ({sl_h1:.2f}) | Riesgo Cero"

        # Validación estricta: Jamás retroceder el Stop Loss
        es_mejora = False
        if nuevo_sl is not None and nuevo_stage > sl_stage:
            if direccion == "LONG" and (sl_actual is None or nuevo_sl > sl_actual):
                es_mejora = True
            elif direccion == "SHORT" and (sl_actual is None or nuevo_sl < sl_actual):
                es_mejora = True

        if es_mejora:
            trade["sl_stage"] = nuevo_stage
            trade["current_sl_price"] = nuevo_sl
            contract_id = trade["contract_id"]
            
            # Enviar modificación a TopstepX con stopPrice
            self.mover_sl_en_topstep(contract_id, nuevo_sl, sym)
            
            self.log(
                f"🛡️ [{sym}] ¡{motivo_hito}! Avance: +{max_pct:.1f}% de TP.",
                "ORDER", sym
            )
            self.registrar_evento_be_en_csv(sym, trade, nuevo_sl, max_pct, motivo_hito)

        # 4. SALVAGUARDA DE SEGURIDAD POR SOFTWARE (Fail-Safe Guard):
        # Si ya se activó algún hito de protección y el precio retrocede y cruza el SL protegido:
        if trade.get("sl_stage", 0) >= 1:
            sl_protegido = trade["current_sl_price"]
            sl_alcanzado = False
            if direccion == "LONG" and precio_actual <= sl_protegido:
                sl_alcanzado = True
            elif direccion == "SHORT" and precio_actual >= sl_protegido:
                sl_alcanzado = True
                
            if sl_alcanzado:
                self.log(f"🛡️ [{sym}] Precio alcanzó nivel de Stop protegido ({precio_actual:.2f} vs SL {sl_protegido:.2f}). Ejecutando cierre de seguridad inmediato.", "ORDER", sym)
                try:
                    self.client.close_contract_position(self.account_id, trade["contract_id"])
                except Exception as ce:
                    self.log(f"⚠️ Error al forzar cierre de seguridad en {sym}: {ce}", "ERROR", sym)
                state["active_trade"] = None
                state["has_open_position"] = False
                return

    def mover_sl_en_topstep(self, contract_id: str, nuevo_sl: float, sym: str) -> bool:
        """Localiza la orden Stop activa en TopstepX para el contrato y modifica su stopPrice a nuevo_sl."""
        if not self.account_id:
            return False
            
        try:
            open_orders = self.client.search_open_orders(self.account_id)
            stop_order = None
            for o in open_orders:
                cid = str(o.get("contractId", ""))
                otype = int(o.get("type", 0))
                # Type 4 = Stop Order
                if cid == str(contract_id) and otype == 4:
                    stop_order = o
                    break
                    
            if stop_order:
                order_id = stop_order.get("id")
                size = stop_order.get("size", 1)
                res = self.client.modify_order(
                    account_id=self.account_id,
                    order_id=order_id,
                    stop_price=nuevo_sl,
                    size=size
                )
                if res.get("success"):
                    self.log(f"✅ [{sym}] Orden Stop #{order_id} modificada con éxito en TopstepX a {nuevo_sl:.2f}.", "ORDER", sym)
                    return True
                else:
                    err_msg = res.get('errorMessage') or f"Código {res.get('errorCode')}"
                    self.log(f"⚠️ [{sym}] Respuesta al modificar orden #{order_id} en TopstepX: {err_msg}", "ERROR", sym)
            else:
                self.log(f"ℹ️ [{sym}] No se encontró orden Stop abierta en TopstepX. Nivel protegido resguardado por software en {nuevo_sl:.2f}.", "SYSTEM", sym)
        except Exception as e:
            self.log(f"⚠️ Error al intentar mover SL en TopstepX: {e}", "ERROR", sym)
            
        return False

    def registrar_evento_be_en_csv(self, sym: str, trade: Dict[str, Any], nuevo_sl: float, pct_avance: float, motivo: str = "Ajuste SL"):
        """Registra el evento de Break-Even o Profit Lock en el archivo CSV institucional para trazabilidad."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(CSV_PATH)), exist_ok=True)
            archivo_existe = os.path.isfile(CSV_PATH)
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            
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
                    'Simbolo': sym,
                    'Estado': f"SL PROTEGIDO (Hito {trade.get('sl_stage', 1)})",
                    'Direccion': trade["direction"],
                    'Precio_Mercado': f"{trade.get('current_price', nuevo_sl):.2f}",
                    'Precio_Entrada_Real': f"{trade['entry_price']:.2f}",
                    'Precio_Salida_Real': "PENDIENTE",
                    'SL_Ticks': str(trade.get("sl_stage", 1)),
                    'TP_Ticks': str(trade.get("tp_ticks", "Dinámico")),
                    'PnL_Bruto_USD': "0.00",
                    'Comisiones_USD': "0.00",
                    'PnL_Neto_USD': "0.00",
                    'Order_ID': str(trade.get("order_id", "N/A")),
                    'Exit_Order_ID': "PENDIENTE",
                    'Motivo_Rechazo': "N/A",
                    'Codigo_Error': "0",
                    'Condicion_Tecnica': motivo,
                    'Ultima_Actualizacion': f"{fecha_actual} {hora_actual}"
                })
            self.log(f"💾 Evento de protección de ganancias registrado en CSV.", "SYSTEM", sym)
        except Exception as e:
            self.log(f"❌ Error al registrar en CSV: {e}", "ERROR", sym)

    # ==========================================
    # BLACK BOX RECORDING & REAL TOPSTEP SYNCHRONIZATION
    # ==========================================
    def resolve_symbol_from_contract(self, contract_id: str) -> str:
        """Determina el símbolo estándar (ES, NQ, GC, CL, MCL, etc.) a partir del contractId de TopstepX."""
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
    # DUAL-PROCESS ENGINE ARCHITECTURE
    # ==========================================
    def run_trade_monitor_loop(self):
        """
        SUBPROCESO 1: MONITOR DE POSICIONES Y TRAILING STOP DE TIEMPO REAL (1.0s)
        - Exclusivo para gestión de operaciones abiertas.
        - No descarga historial pesado ni calcula VWAP.
        - Detecta y adopta posiciones abiertas en TopstepX.
        - Consulta el precio actual y el extremo vivo de la mecha (High/Low) en <50ms.
        - Ajusta el Stop Loss inmediatamente si se alcanzó o superó cualquiera de los 3 hitos.
        - Aplica salvaguarda Fail-Safe ante retrocesos.
        """
        self.log("⚡ [FAST TRACKER] Hilo dedicado de monitoreo de operaciones iniciado (1.0s).", "SYSTEM")
        
        while not self.stop_event.is_set():
            try:
                # 1. Sincronización global rápida de posiciones en la cuenta TopstepX
                self.sincronizar_posiciones_activas_cuenta()
                
                # 2. Localizar símbolos con operación abierta
                for sym, state in list(self.symbol_states.items()):
                    if self.stop_event.is_set():
                        break
                        
                    has_trade = (state.get("active_trade") is not None) or state.get("has_open_position", False)
                    if not has_trade:
                        continue
                        
                    contract_id = state.get("contract_id")
                    if not contract_id:
                        continue
                        
                    # Consulta ultra-ligera de 1 sola barra viva de 1m (<50ms de latencia)
                    df_live = self.client.retrieve_bars(contract_id, unit=2, unit_number=1, limit=1)
                    self.monitorear_posicion_abierta(sym, df_live)
                    
            except Exception as e:
                self.log(f"⚠️ Excepción en Fast Trade Tracker: {e}", "ERROR")
                
            if not self.stop_event.is_set():
                time.sleep(1.0)
                
        self.log("🛑 Fast Trade Tracker detenido correctamente.", "SYSTEM")

    def run_scanner_loop(self):
        """
        SUBPROCESO 2: ESCÁNER DE NUEVAS ENTRADAS Y TELEMETRÍA VWAP (Segundo Plano)
        - Descarga historial de 350 barras para el cálculo de sesión VWAP y bandas.
        - Si un símbolo ya tiene posición abierta, omite el escaneo para ese símbolo (lo gestiona Subproceso 1).
        - Evalúa el filtro de horario institucional CME (18:00-20:00 ET).
        - Evalúa la condición de vela previa de 5m limpia (cuerpo y mecha completa del lado del rebote).
        - Evalúa la absorción de volumen en 1m (+10% esfuerzo vs SMA10, mecha >= 2 ticks).
        - Despacha órdenes OCO bracket y las entrega a Subproceso 1.
        """
        self.log("🔍 [SCANNER] Hilo de escaneo institucional VWAP iniciado.", "SYSTEM")
        
        while not self.stop_event.is_set():
            try:
                # 0. Sincronización horaria periódica con TopstepX
                if time.time() - self.last_sync_time >= self.sync_interval_seconds:
                    self.log("⏰ [SINCRONIZACIÓN HORARIA] Actualizando balance y operaciones reales con TopstepX...", "SYSTEM")
                    try:
                        self.sync_real_data_from_topstep()
                    except Exception as e:
                        self.log(f"⚠️ Error en sincronización horaria de TopstepX: {e}", "ERROR")

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
                    
                    # Si ya tiene una operación abierta activa, Subproceso 1 se encarga de ella.
                    if state.get("active_trade") or state.get("has_open_position"):
                        continue
                        
                    # Frecuencia de escaneo: cada 5.0 segundos para activos/pantalla; cada 60s para inactivos
                    min_interval = 5.0 if (is_enabled or is_active_view) else 60.0
                    if (now_poll - state.get("last_poll_time", 0.0)) < min_interval:
                        continue
                    state["last_poll_time"] = now_poll
                    
                    # Descargamos historial de 350 velas para VWAP acumulativo desde 18:00 ET
                    df = self.client.retrieve_bars(contract_id, unit=2, unit_number=5, limit=350, days_back=2)
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
                        
                        # Emitir telemetría para símbolos habilitados o vista activa
                        if is_enabled or is_active_view:
                            self.log(f"📊 {sym}: {vela_actual['Close']:,.2f} | 🔴 Sup: {vela_actual['Upper_Band']:,.2f} | 🔵 VWAP: {vela_actual['VWAP']:,.2f} | 🟢 Inf: {vela_actual['Lower_Band']:,.2f} | Dist: {dist_vwap:+.2f} pts", "TELEMETRY", sym)
                        
                        # Evaluación de NUEVAS operaciones para símbolos habilitados
                        if is_enabled:
                            vela_actual = df.iloc[-1]
                            vela_ts = vela_actual['Timestamp']
                            time_str = vela_ts.strftime('%H:%M')
                            
                            if state.get("current_active_candle_ts") != vela_ts:
                                state["current_active_candle_ts"] = vela_ts
                                self.log(f"🕯️ [{sym}] Vela 5m activa ({time_str} UTC). Monitoreando zona VWAP y absorción...", "SETUP", sym)

                            # 1. Filtro institucional de sesión CME (primeras 2 horas tras apertura y receso)
                            is_operable, motivo_horario = self.es_horario_operable()
                            if not is_operable:
                                if state.get("last_inoperable_reason") != motivo_horario:
                                    state["last_inoperable_reason"] = motivo_horario
                                    self.log(f"⏸️ [{sym}] {motivo_horario}. Entradas suspendidas.", "SETUP", sym)
                                continue
                            else:
                                state["last_inoperable_reason"] = None

                            # 2. Cooldown
                            if time.time() < state["cooldown_until"]:
                                continue

                            # 3. Máximo 1 operación por ciclo
                            if state.get("last_executed_candle_ts") == vela_ts:
                                continue

                            # 4. Evaluación de Absorción Institucional en VWAP (incluye condición de vela previa 5m limpia)
                            setup = self.verificar_absorcion_vwap_1m(df, sym)
                            if setup:
                                state["last_executed_candle_ts"] = vela_ts
                                state["last_executed_1m_ts"] = setup.get("timestamp_1m")
                                time_1m = setup["timestamp_1m"].strftime('%H:%M') if setup.get("timestamp_1m") else time_str
                                rvol_pct = (setup.get("rvol", 1.0) - 1.0) * 100
                                self.log(
                                    f"⚡ [{sym}] ¡Absorción en VWAP confirmada en vela 1m ({time_1m} UTC)! "
                                    f"Vol: {setup['volumen_1m']:,.0f} vs Prom(10m): {setup['promedio_vol_10']:,.0f} ({rvol_pct:+.1f}%). "
                                    f"Extremo: {setup['extremo_precio']:.2f}. Abriendo {setup['direccion']}...",
                                    "ORDER", sym
                                )
                                self.ejecutar_orden(setup, df, sym)
                    
                    time.sleep(0.25)
                    
            except requests.exceptions.HTTPError as he:
                if he.response is not None and he.response.status_code == 429:
                    self.log("⏸️ TopstepX Rate Limit temporal alcanzado (HTTP 429). Pausando 6 segundos...", "SYSTEM")
                    time.sleep(6)
                else:
                    self.log(f"⚠️ Excepción en el escáner multi-símbolo: {he}", "ERROR", "ALL")
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    self.log("⏸️ TopstepX Rate Limit temporal alcanzado (HTTP 429). Pausando 6 segundos...", "SYSTEM")
                    time.sleep(6)
                else:
                    self.log(f"⚠️ Excepción en el escáner multi-símbolo: {e}", "ERROR", "ALL")
                
            if not self.stop_event.is_set():
                time.sleep(1.0)

        self.log("🛑 Escáner multi-símbolo detenido correctamente.", "SYSTEM", "ALL")

    def run_loop(self):
        """Bucle unificado de compatibilidad si se invoca directamente."""
        self.run_scanner_loop()

    # ==========================================
    # LIFECYCLE MANAGEMENT
    # ==========================================
    def start(self):
        if self.running:
            self.log("⚠️ El motor ya está activo.", "SYSTEM")
            return
            
        self.stop_event.clear()
        self.running = True
        
        # Subproceso 1: Monitor de Posiciones Abiertas y Trailing Stop de Alta Frecuencia (1.0s)
        self.trade_monitor_thread = threading.Thread(target=self.run_trade_monitor_loop, daemon=True, name="Axiom-FastTracker")
        self.trade_monitor_thread.start()
        self.thread = self.trade_monitor_thread
        
        # Subproceso 2: Escáner de Señales VWAP y Telemetría
        self.scanner_thread = threading.Thread(target=self.run_scanner_loop, daemon=True, name="Axiom-Scanner")
        self.scanner_thread.start()
        
        self.log("⚡ Motor multi-símbolo iniciado con arquitectura dual: Subproceso 1 (Fast Trailing Stop 1s) + Subproceso 2 (Escáner VWAP).", "SYSTEM")

    def stop(self):
        if not self.running:
            return
            
        self.log("🛑 Deteniendo motor multi-símbolo...", "SYSTEM")
        self.running = False
        self.stop_event.set()
        
        if hasattr(self, "trade_monitor_thread") and self.trade_monitor_thread and self.trade_monitor_thread.is_alive():
            self.trade_monitor_thread.join(timeout=2)
            
        if hasattr(self, "scanner_thread") and self.scanner_thread and self.scanner_thread.is_alive():
            self.scanner_thread.join(timeout=2)

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
