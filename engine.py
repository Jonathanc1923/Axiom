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
    INITIAL_SL_TICKS, POLL_INTERVAL_SECONDS,
    HITO_1_TRIGGER_PCT, HITO_2_TRIGGER_PCT, HITO_2_SL_PCT, HITO_2_TP_EXPAND_PCT,
    HITO_3_TRIGGER_PCT, HITO_3_SL_PCT, HITO_3_TP_EXPAND_PCT,
    HITO_4_TRIGGER_PCT, HITO_4_SL_PCT, HITO_4_TP_EXPAND_PCT, RUNNER_STEP_PCT
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
                "enabled": (sym in ["ES", "NQ"]), # ES y NQ habilitados por defecto
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
        
        # Sincronización horaria con TopstepX
        self.last_sync_time: float = 0
        self.sync_interval_seconds: int = 3600

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

    @staticmethod
    def format_milestone(stage: int) -> str:
        """Formatea el hito alcanzado de forma descriptiva e infinita según config.py."""
        if stage >= 5:
            k = stage - 4
            trig_k = HITO_4_TRIGGER_PCT + (k * RUNNER_STEP_PCT)
            sl_k = (HITO_4_SL_PCT + (k * (RUNNER_STEP_PCT / 100.0))) * 100.0
            tp_k = (HITO_4_TP_EXPAND_PCT + (k * (RUNNER_STEP_PCT / 100.0))) * 100.0
            return f"Hito {stage} ({trig_k:.0f}% SL{sl_k:.0f}/TP{tp_k:.0f})"
        elif stage == 4:
            return f"Hito 4 ({HITO_4_TRIGGER_PCT:.0f}% SL{HITO_4_SL_PCT*100:.0f}/TP{HITO_4_TP_EXPAND_PCT*100:.0f})"
        elif stage == 3:
            return f"Hito 3 ({HITO_3_TRIGGER_PCT:.0f}% SL{HITO_3_SL_PCT*100:.0f}/TP{HITO_3_TP_EXPAND_PCT*100:.0f})"
        elif stage == 2:
            return f"Hito 2 ({HITO_2_TRIGGER_PCT:.0f}% SL{HITO_2_SL_PCT*100:.0f}/TP{HITO_2_TP_EXPAND_PCT*100:.0f})"
        elif stage == 1:
            return f"Hito 1 ({HITO_1_TRIGGER_PCT:.0f}% BE+1)"
        return "Inicial"

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
        print(f"[{timestamp_str}] [{level}] {prefix_sym}{message}")
        
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
        """
        if df.empty:
            return df
            
        df = df.copy()
        df['NY_Time'] = df['Timestamp'].dt.tz_convert('America/New_York')
        df['Session'] = (df['NY_Time'] + pd.Timedelta(hours=6)).dt.date
        
        df['HLC3'] = (df['High'] + df['Low'] + df['Close']) / 3.0
        df['PxV'] = df['HLC3'] * df['Volume']
        df['Px2xV'] = (df['HLC3'] ** 2) * df['Volume']
        
        df['Cum_Vol'] = df.groupby('Session')['Volume'].cumsum()
        df['Cum_PxV'] = df.groupby('Session')['PxV'].cumsum()
        df['Cum_Px2xV'] = df.groupby('Session')['Px2xV'].cumsum()
        
        safe_cum_vol = df['Cum_Vol'].replace(0, np.nan)
        df['VWAP'] = np.where(df['Cum_Vol'] > 0, df['Cum_PxV'] / safe_cum_vol, df['HLC3'])
        
        varianza = (df['Cum_Px2xV'] / safe_cum_vol) - (df['VWAP'] ** 2)
        df['Variance'] = np.maximum(varianza.fillna(0), 0)
        df['Std_Dev'] = np.sqrt(df['Variance']).fillna(0)
        df['Upper_Band'] = df['VWAP'] + df['Std_Dev']
        df['Lower_Band'] = df['VWAP'] - df['Std_Dev']
        
        return df

    # ==========================================
    # STRATEGY ENGINE: PURE VWAP TOUCH & CONTEXT
    # ==========================================
    def verificar_setup_vwap(self, df_5m: pd.DataFrame, sym: str, current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Estrategia de Reversión VWAP Institucional Pura:
        
        REGLA DE CONTEXTO (Vela previa de 5m completa en nuestro lado):
        - Para LONG (esperamos rebote alcista):
          La vela previa de 5 minutos debe haber estado 100% por encima del VWAP:
          prev_candle.Low > prev_candle.VWAP
        - Para SHORT (esperamos rebote bajista):
          La vela previa de 5 minutos debe haber estado 100% por debajo del VWAP:
          prev_candle.High < prev_candle.VWAP

        GATILLO INMEDIATO (Al tocar o traspasar VWAP dentro de rango de tolerancia):
        - Para LONG: precio_actual <= VWAP_actual (máximo 12 ticks de penetración anti-chasing)
        - Para SHORT: precio_actual >= VWAP_actual (máximo 12 ticks de penetración anti-chasing)
        """
        if df_5m is None or len(df_5m) < 2:
            return None

        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)

        vela_5m_actual = df_5m.iloc[-1]
        vela_5m_prev = df_5m.iloc[-2]

        vwap_now = float(vela_5m_actual['VWAP'])
        upper_band_now = float(vela_5m_actual['Upper_Band'])
        lower_band_now = float(vela_5m_actual['Lower_Band'])

        vwap_prev = float(vela_5m_prev.get('VWAP', vwap_now))
        prev_low = float(vela_5m_prev['Low'])
        prev_high = float(vela_5m_prev['High'])

        precio_mercado = float(current_price) if current_price is not None else float(vela_5m_actual['Close'])

        # 1. Filtro de Contexto de Vela Previa
        contexto_long = prev_low > (vwap_prev + (0.5 * tick_size))
        contexto_short = prev_high < (vwap_prev - (0.5 * tick_size))

        # 2. Evaluación de Gatillo al tocar o traspasar VWAP (Sin restricción anti-chasing)
        # Para LONG: Venimos desde arriba, y el precio toca o pasa el VWAP hacia abajo
        if contexto_long and precio_mercado <= vwap_now:
            d_base = abs(upper_band_now - precio_mercado)
            if d_base < (10 * tick_size):
                d_base = 15 * tick_size
            return {
                "simbolo": sym,
                "direccion": "LONG",
                "precio_entrada": precio_mercado,
                "vwap_cruce": vwap_now,
                "upper_band": upper_band_now,
                "lower_band": lower_band_now,
                "target_band": upper_band_now,
                "d_base": d_base,
                "timestamp_vela": vela_5m_actual['Timestamp']
            }

        # Para SHORT: Venimos desde abajo, y el precio toca o pasa el VWAP hacia arriba
        if contexto_short and precio_mercado >= vwap_now:
            d_base = abs(precio_mercado - lower_band_now)
            if d_base < (10 * tick_size):
                d_base = 15 * tick_size
            return {
                "simbolo": sym,
                "direccion": "SHORT",
                "precio_entrada": precio_mercado,
                "vwap_cruce": vwap_now,
                "upper_band": upper_band_now,
                "lower_band": lower_band_now,
                "target_band": lower_band_now,
                "d_base": d_base,
                "timestamp_vela": vela_5m_actual['Timestamp']
            }

        return None

    # ==========================================
    # RISK FILTERS & SCHEDULE
    # ==========================================
    def es_horario_operable(self) -> tuple[bool, str]:
        """
        Evalúa si el mercado CME está activo.
        - Cierre de fin de semana: Viernes 17:00 ET a Domingo 18:00 ET.
        - Mantenimiento diario CME: 17:00 a 18:00 ET (Lunes a Jueves).
        - 24/7 durante toda la semana operativa regular (incluyendo apertura 18:00 ET).
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
        
        # Fin de semana
        if weekday == 5:
            return False, f"Mercado CME cerrado por fin de semana (Sábado - {time_str})"
        if weekday == 6 and hour < 18:
            return False, f"Mercado CME cerrado por fin de semana (Domingo pre-apertura - {time_str})"
        if weekday == 4 and hour >= 17:
            return False, f"Mercado CME cerrado por fin de semana (Viernes post-cierre - {time_str})"
            
        # Mantenimiento diario CME (17:00 a 18:00 ET)
        if hour == 17:
            return False, f"Receso diario de mantenimiento CME (17:00-18:00 ET - {time_str})"
            
        return True, f"Sesión regular activa ({time_str})"

    def tiene_posicion_abierta(self, sym: str, cached_positions: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Verifica si el símbolo ya tiene una posición abierta activa, reutilizando posiciones en bloque si se proporcionan."""
        state = self.symbol_states.get(sym, {})
        contract_id = state.get("contract_id")
        
        try:
            positions = cached_positions
            if positions is None and self.account_id:
                positions = self.client.get_open_positions(self.account_id)
                
            if positions is not None:
                for pos in positions:
                    pos_cid = str(pos.get("contractId", ""))
                    pos_size = abs(int(pos.get("size", 0)))
                    pos_sym = self.resolve_symbol_from_contract(pos_cid)
                    if pos_size > 0 and (pos_cid == contract_id or pos_sym == sym):
                        state["has_open_position"] = True
                        return True
                # Si consultó exitosamente a Topstep y no hay posiciones abiertas:
                # Solo marcamos False si no hay un active_trade recién emitido (< 5 segundos)
                trade = state.get("active_trade")
                if trade and (time.time() - trade.get("entry_timestamp", 0.0) < 5.0):
                    return True
                state["has_open_position"] = False
                return False
        except Exception:
            return state.get("has_open_position", False)
            
        return state.get("has_open_position", False)

    # ==========================================
    # ORDER EXECUTION ENGINE
    # ==========================================
    def ejecutar_orden(self, setup: Dict[str, Any], sym: str, ignore_schedule: bool = False):
        """
        Ejecuta la orden a mercado en TopstepX con brackets OCO iniciales:
        - SL Inicial: Fijo en 25 ticks (INITIAL_SL_TICKS).
        - TP Inicial: Distancia base D_base hacia la banda lateral que le sigue.
        - Congela los parámetros base en memoria para la gestión por hitos cada 2 segundos.
        """
        if setup.get("simbolo") != sym:
            self.log(f"❌ Discrepancia de símbolo setup={setup.get('simbolo')} vs sym={sym}", "ERROR", sym)
            return

        if not ignore_schedule:
            is_operable, motivo_horario = self.es_horario_operable()
            if not is_operable:
                self.log(f"⏸️ [{sym}] Orden suspendida: {motivo_horario}.", "SETUP", sym)
                return

        if self.tiene_posicion_abierta(sym):
            self.log(f"🛡️ [{sym}] Ya existe una posición abierta en {sym}.", "SETUP", sym)
            return
            
        direccion = setup["direccion"]
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        contract_id = self.symbol_states[sym]["contract_id"]
        precio_entrada = round_to_tick(float(setup["precio_entrada"]), tick_size)
        d_base = float(setup["d_base"])
        
        # 1. SL Inicial: Universal en 25 ticks
        ticks_sl = INITIAL_SL_TICKS
        ticks_tp = max(round(d_base / tick_size), 10)

        if direccion == "LONG":
            precio_sl = round_to_tick(precio_entrada - (ticks_sl * tick_size), tick_size)
            precio_tp = round_to_tick(precio_entrada + (ticks_tp * tick_size), tick_size)
            side = 0
            # Convención oficial TopstepX: LONG -> SL negativo (-), TP positivo (+)
            sl_ticks_order = -ticks_sl
            tp_ticks_order = ticks_tp
        else: # SHORT
            precio_sl = round_to_tick(precio_entrada + (ticks_sl * tick_size), tick_size)
            precio_tp = round_to_tick(precio_entrada - (ticks_tp * tick_size), tick_size)
            side = 1
            # Convención oficial TopstepX: SHORT -> SL positivo (+), TP negativo (-)
            sl_ticks_order = ticks_sl
            tp_ticks_order = -ticks_tp

        condicion_str = (
            f"VWAP Touch Reversion. Entrada: {precio_entrada:.2f} | "
            f"SL inicial: 25t ({precio_sl:.2f}) | TP inicial Banda: {ticks_tp}t ({precio_tp:.2f}) | D_base: {d_base:.2f} pts"
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
                
                # Inicializar seguimiento activo e independiente con D_base congelado
                self.symbol_states[sym]["active_trade"] = {
                    "symbol": sym,
                    "contract_id": contract_id,
                    "direction": direccion,
                    "entry_price": precio_entrada,
                    "current_price": precio_entrada,
                    "highest_price": precio_entrada,
                    "lowest_price": precio_entrada,
                    "initial_sl_price": precio_sl,
                    "current_sl_price": precio_sl,
                    "initial_tp_price": precio_tp,
                    "current_tp_price": precio_tp,
                    "base_tp_pts": d_base,
                    "initial_sl_ticks": ticks_sl,
                    "tp_ticks": ticks_tp,
                    "order_id": order_id,
                    "sl_stage": 0, # 0=Inicio, 1=Hito 1 (40%), 2=Hito 2 (70%), 3=Hito 3 (120%)
                    "max_pct_avance": 0.0,
                    "entry_timestamp": time.time(),
                    "last_check_time": time.time()
                }
                
                self.log(f"✅ Orden ejecutada con éxito en TopstepX. Order ID: {order_id} | Trailing 2s inicializado.", "ORDER", sym)
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
                self.symbol_states[sym]["cooldown_until"] = time.time() + 180
            else:
                error_msg = res.get('errorMessage') or res.get('message') or res.get('error') or 'Rechazo de API'
                error_code = str(res.get('errorCode') or res.get('code') or 'REJECTED')
                self.symbol_states[sym]["has_open_position"] = False
                self.log(f"❌ Orden RECHAZADA por TopstepX en {sym}: {error_msg} (Código: {error_code})", "ERROR", sym)
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
            state["cooldown_until"] = time.time() + 180
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
        if direccion == "LONG":
            highest_since_entry = max(trade.get("highest_price", precio_entrada), precio_actual)
            trade["highest_price"] = highest_since_entry
            precio_favorable = highest_since_entry
            ganancia_pts = precio_favorable - precio_entrada
            pct_avance = (ganancia_pts / d_base) * 100.0 if d_base > 0 else 0.0
        else: # SHORT
            lowest_since_entry = min(trade.get("lowest_price", precio_entrada), precio_actual)
            trade["lowest_price"] = lowest_since_entry
            precio_favorable = lowest_since_entry
            ganancia_pts = precio_entrada - precio_favorable
            pct_avance = (ganancia_pts / d_base) * 100.0 if d_base > 0 else 0.0

        max_pct = max(trade.get("max_pct_avance", 0.0), pct_avance)
        trade["max_pct_avance"] = max_pct

        ganancia_ticks = round(ganancia_pts / tick_size) if tick_size > 0 else 0
        stage_str = f" [{self.format_milestone(sl_stage)}]" if sl_stage > 0 else " [Inicial]"

        self.log(
            f"🎯 [{sym}] {direccion}{stage_str} | In: {precio_entrada:.2f} | Actual: {precio_actual:.2f} "
            f"({ganancia_ticks:+d}t | {pct_avance:.1f}% D_base | Max: {max_pct:.1f}%) | SL: {sl_actual:.2f} | TP: {precio_tp:.2f}",
            "TELEMETRY", sym
        )

        contract_id = trade["contract_id"]
        order_id = str(trade.get("order_id", "N/A"))

        # EVALUACIÓN DE ESCALERA DE HITOS INFINITA (D_base runners)
        target_stage = sl_stage
        
        if max_pct >= HITO_1_TRIGGER_PCT and target_stage < 1:
            target_stage = 1
        if max_pct >= HITO_2_TRIGGER_PCT and target_stage < 2:
            target_stage = 2
        if max_pct >= HITO_3_TRIGGER_PCT and target_stage < 3:
            target_stage = 3
        if max_pct >= HITO_4_TRIGGER_PCT:
            runner_tier = 4 + int((max_pct - HITO_4_TRIGGER_PCT) // RUNNER_STEP_PCT)
            if runner_tier > target_stage:
                target_stage = runner_tier

        nuevo_sl = None
        nuevo_tp = None
        motivo_hito = ""

        if target_stage > sl_stage:
            if target_stage == 1:
                nuevo_sl = round_to_tick(precio_entrada + 1 * tick_size, tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - 1 * tick_size, tick_size)
                nuevo_tp = None
                motivo_hito = f"HITO 1 (40% D_base) | SL a Break-Even + 1 tick ({nuevo_sl:.2f})"
            elif target_stage == 2:
                nuevo_sl = round_to_tick(precio_entrada + (d_base * HITO_2_SL_PCT), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * HITO_2_SL_PCT), tick_size)
                nuevo_tp = round_to_tick(precio_entrada + (d_base * HITO_2_TP_EXPAND_PCT), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * HITO_2_TP_EXPAND_PCT), tick_size)
                motivo_hito = f"HITO 2 (70% D_base) | SL al 45% ({nuevo_sl:.2f}) | TP Ampliado al 140% ({nuevo_tp:.2f})"
            elif target_stage == 3:
                nuevo_sl = round_to_tick(precio_entrada + (d_base * HITO_3_SL_PCT), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * HITO_3_SL_PCT), tick_size)
                nuevo_tp = round_to_tick(precio_entrada + (d_base * HITO_3_TP_EXPAND_PCT), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * HITO_3_TP_EXPAND_PCT), tick_size)
                motivo_hito = f"HITO 3 (120% D_base) | SL al 108% ({nuevo_sl:.2f}) | TP Ampliado al 170% ({nuevo_tp:.2f})"
            elif target_stage == 4:
                nuevo_sl = round_to_tick(precio_entrada + (d_base * HITO_4_SL_PCT), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * HITO_4_SL_PCT), tick_size)
                nuevo_tp = round_to_tick(precio_entrada + (d_base * HITO_4_TP_EXPAND_PCT), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * HITO_4_TP_EXPAND_PCT), tick_size)
                motivo_hito = f"HITO 4 (135% D_base) | SL al 128% ({nuevo_sl:.2f}) | TP Ampliado al 200% ({nuevo_tp:.2f})"
            else: # target_stage >= 5 (Infinito)
                k = target_stage - 4
                trig_k = HITO_4_TRIGGER_PCT + (k * RUNNER_STEP_PCT)
                sl_k_pct = HITO_4_SL_PCT + (k * (RUNNER_STEP_PCT / 100.0))
                tp_k_pct = HITO_4_TP_EXPAND_PCT + (k * (RUNNER_STEP_PCT / 100.0))
                
                nuevo_sl = round_to_tick(precio_entrada + (d_base * sl_k_pct), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * sl_k_pct), tick_size)
                nuevo_tp = round_to_tick(precio_entrada + (d_base * tp_k_pct), tick_size) if direccion == "LONG" else round_to_tick(precio_entrada - (d_base * tp_k_pct), tick_size)
                motivo_hito = f"HITO {target_stage} ({trig_k:.0f}% D_base) | SL al {sl_k_pct*100:.0f}% ({nuevo_sl:.2f}) | TP Ampliado al {tp_k_pct*100:.0f}% ({nuevo_tp:.2f})"

            trade["sl_stage"] = target_stage
            if nuevo_sl is not None:
                trade["current_sl_price"] = nuevo_sl
            if nuevo_tp is not None:
                trade["current_tp_price"] = nuevo_tp

            # Ajustar órdenes de forma atómica en TopstepX con una sola consulta
            self.ajustar_bracket_en_topstep(contract_id, nuevo_sl, nuevo_tp, sym)

            self.log(f"🏆 [{sym}] ¡{motivo_hito}! Recorrido máximo: {max_pct:.1f}%", "ORDER", sym)
            self.registrar_evento_be_en_csv(sym, trade, nuevo_sl or sl_actual, max_pct, motivo_hito)

        # SALVAGUARDA FAIL-SAFE POR SOFTWARE
        # Si ya se activó al menos el Hito 1 y el precio retrocede traspasando el SL protegido:
        if trade.get("sl_stage", 0) >= 1:
            sl_protegido = trade["current_sl_price"]
            sl_alcanzado = False
            if direccion == "LONG" and precio_actual < sl_protegido:
                sl_alcanzado = True
            elif direccion == "SHORT" and precio_actual > sl_protegido:
                sl_alcanzado = True
                
            if sl_alcanzado:
                self.log(f"🛡️ [{sym}] Precio perforó Stop protegido ({precio_actual:.2f} vs SL {sl_protegido:.2f}). Liquidando posición de inmediato...", "ORDER", sym)
                try:
                    self.client.close_contract_position(self.account_id, trade["contract_id"])
                    self.cancelar_ordenes_abiertas_contrato(trade["contract_id"], sym)
                except Exception as ce:
                    self.log(f"⚠️ Error al liquidar posición protegida en {sym}: {ce}", "ERROR", sym)
                self.registrar_cierre_posicion(sym, trade, precio_salida=precio_actual, motivo_cierre=f"Stop Protegido Perforado ({sl_protegido:.2f})")
                state["active_trade"] = None
                state["has_open_position"] = False
                state["cooldown_until"] = time.time() + 180
                return

    def ajustar_bracket_en_topstep(self, contract_id: str, nuevo_sl: Optional[float], nuevo_tp: Optional[float], sym: str) -> bool:
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
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)

        try:
            open_orders = self.client.search_open_orders(self.account_id)
            for o in open_orders:
                cid = str(o.get("contractId", ""))
                otype = int(o.get("type", 0))
                order_id = o.get("id")
                size = o.get("size", 1)
                
                if cid == str(contract_id):
                    # Type 4 = Stop Order (SL)
                    if otype == 4 and nuevo_sl is not None:
                        curr_stop = o.get("stopPrice")
                        if curr_stop is not None and abs(float(curr_stop) - float(nuevo_sl)) < (0.01 * tick_size):
                            continue # Ya está en el nivel deseado

                        target_sl = round_to_tick(nuevo_sl, tick_size)
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
                current_sl_price = (entry_price - 25 * tick_size) if direccion == "LONG" else (entry_price + 25 * tick_size)
                
            if tp_order and tp_order.get("limitPrice") is not None:
                tp_price = float(tp_order["limitPrice"])
            else:
                tp_price = (entry_price + 50 * tick_size) if direccion == "LONG" else (entry_price - 50 * tick_size)
                
            recorrido_tp_pts = abs(tp_price - entry_price)
            tp_ticks = round(recorrido_tp_pts / tick_size) if tick_size > 0 else 0
            sl_ticks = round(abs(entry_price - current_sl_price) / tick_size) if tick_size > 0 else 0
            
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
                "last_check_time": time.time()
            }
            
            state["active_trade"] = adopted_trade
            state["has_open_position"] = True
            state["contract_id"] = contract_id
            
            self.log(f"🎯 [{sym}] Posición abierta adoptada en TopstepX: {direccion} @ {entry_price:.2f}.", "ORDER", sym)
            
            # Registrar posición adoptada en CSV para visibilidad inmediata en panel
            now_dt = datetime.now()
            self._guardar_o_actualizar_en_csv({
                'Fecha': now_dt.strftime('%Y-%m-%d'),
                'Hora': now_dt.strftime('%H:%M:%S'),
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
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.csv_path)), exist_ok=True)
            fieldnames = [
                'Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                'Precio_Entrada_Real', 'Precio_Salida_Real', 'SL_Ticks', 'TP_Ticks',
                'Hito_Alcanzado', 'Max_Pct_TP',
                'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
            ]
            
            rows = []
            if os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                with open(self.csv_path, mode='r', encoding='utf-8') as file:
                    reader = csv.DictReader(file, delimiter=';')
                    for r in reader:
                        rows.append(r)
            
            updated = False
            target_oid = str(match_order_id).strip() if match_order_id and str(match_order_id) not in ["N/A", ""] else None
            
            if target_oid:
                for i, r in enumerate(rows):
                    r_oid = str(r.get("Order_ID", "")).strip()
                    if r_oid == target_oid:
                        for k, v in row_data.items():
                            if v is not None and v != "":
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
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            
            precio_entrada = float(trade.get("entry_price", 0.0))
            direccion = trade.get("direction", "LONG")
            order_id = str(trade.get("order_id", "N/A"))
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
            
            self.log(
                f"🏁 [{sym}] Operación CERRADA ({direccion}) | In: {precio_entrada:.2f} | Out: {p_salida:.2f} | "
                f"PnL: {sign_str}${net_pnl:.2f} USD | Hito final: {hito_desc} (Max: {max_pct:.1f}%) | Motivo: {motivo_cierre}",
                "ORDER", sym
            )
            
            row_data = {
                'Fecha': fecha_actual,
                'Hora': hora_actual,
                'Simbolo': sym,
                'Estado': estado_cierre,
                'Direccion': direccion,
                'Precio_Mercado': f"{p_salida:.2f}",
                'Precio_Entrada_Real': f"{precio_entrada:.2f}",
                'Precio_Salida_Real': f"{p_salida:.2f}",
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

    def registrar_en_csv(self, simbolo, direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id):
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
                'Hora': hora_actual,
                'Simbolo': simbolo,
                'Estado': estado,
                'Direccion': direccion,
                'Precio_Mercado': f"{precio:.2f}",
                'Precio_Entrada_Real': f"{precio:.2f}" if exito else "N/A",
                'Precio_Salida_Real': "PENDIENTE" if exito else "N/A",
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
                            net = float(row.get("PnL_Neto_USD", 0.0))
                            gross = float(row.get("PnL_Bruto_USD", 0.0))
                            comms = float(row.get("Comisiones_USD", 0.0))
                            gross_pnl += gross
                            fees += comms
                            net_pnl += net
                            if net >= 0:
                                winning += 1
                            else:
                                losing += 1
                        except (ValueError, TypeError):
                            pass
                            
            closed_total = winning + losing
            win_rate = round((winning / closed_total * 100.0), 1) if closed_total > 0 else 0.0
            profit_factor = round(abs(gross_pnl / fees), 2) if fees > 0 else 0.0
            
            with self.lock:
                self.stats["total_trades"] = total
                self.stats["executed_trades"] = executed
                self.stats["rejected_trades"] = rejected
                self.stats["longs"] = longs
                self.stats["shorts"] = shorts
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
            
            if os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                fieldnames = [
                    'Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                    'Precio_Entrada_Real', 'Precio_Salida_Real', 'SL_Ticks', 'TP_Ticks',
                    'Hito_Alcanzado', 'Max_Pct_TP',
                    'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                    'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
                ]
                
                rows = []
                with open(self.csv_path, mode='r', encoding='utf-8') as file:
                    reader = csv.DictReader(file, delimiter=';')
                    for r in reader:
                        rows.append(r)
                        
                cambios = False
                for r in rows:
                    estado_actual = (r.get("Estado") or "").upper()
                    if "EN CURSO" in estado_actual or "SL PROTEGIDO" in estado_actual or "ABIERTA" in estado_actual:
                        sym = (r.get("Simbolo") or "ES").upper()
                        if sym not in open_symbols:
                            p_salida = None
                            gross_pnl = None
                            comms = None
                            exit_oid = "SYNC_CLOSE"
                            
                            for fill in reversed(raw_trades):
                                f_sym = self.resolve_symbol_from_contract(fill.get("contractId", ""))
                                if f_sym == sym and fill.get("profitAndLoss") is not None:
                                    p_salida = float(fill.get("price", 0.0))
                                    gross_pnl = float(fill.get("profitAndLoss", 0.0))
                                    f_fees = float(fill.get("fees") or 0.0)
                                    f_comms = float(fill.get("commissions") or 0.0)
                                    comms = round(f_fees + f_comms, 2)
                                    exit_oid = str(fill.get("orderId", "SYNC_CLOSE"))
                                    break
                                    
                            p_entrada = float(r.get("Precio_Entrada_Real") or r.get("Precio_Mercado") or 0.0)
                            if p_salida is None or p_salida == 0.0:
                                p_salida = p_entrada
                                
                            if gross_pnl is None:
                                spec = SUPPORTED_SYMBOLS.get(sym, {"pointValue": 50.0})
                                pt_val = spec.get("pointValue", 50.0)
                                diff_pts = (p_salida - p_entrada) if (r.get("Direccion") or "LONG").upper() == "LONG" else (p_entrada - p_salida)
                                gross_pnl = round(diff_pts * pt_val, 2)
                                
                            if comms is None:
                                comms = 1.22 if sym.startswith("M") else 3.78
                                
                            net_pnl = round(gross_pnl - comms, 2)
                            sign = "+" if net_pnl >= 0 else ""
                            
                            r["Estado"] = f"CERRADA ({sign}${net_pnl:.2f} USD)"
                            r["Precio_Salida_Real"] = f"{p_salida:.2f}"
                            r["PnL_Bruto_USD"] = f"{gross_pnl:.2f}"
                            r["Comisiones_USD"] = f"{comms:.2f}"
                            r["PnL_Neto_USD"] = f"{net_pnl:.2f}"
                            r["Exit_Order_ID"] = exit_oid
                            r["Ultima_Actualizacion"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            cambios = True
                            
                if cambios:
                    with open(self.csv_path, mode='w', newline='', encoding='utf-8') as file:
                        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=';')
                        writer.writeheader()
                        for r in rows:
                            writer.writerow({fn: r.get(fn, "N/A") for fn in fieldnames})
                            
            now_dt = datetime.now()
            self.last_sync_time = time.time()
            self.stats["last_sync"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            self.stats["next_sync"] = (now_dt + timedelta(seconds=self.sync_interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            
            self._recalcular_stats_desde_csv()
            self.log(f"🔄 [SYNC] Sincronización completada. Posiciones abiertas en TopstepX: {len(open_positions)} | Balance: ${self.stats['real_balance']:,.2f}", "SYSTEM")
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
                    records.append(row)
        except Exception as e:
            self.log(f"⚠️ Error al leer operaciones de CSV: {e}", "ERROR")
        return records

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

                # Filtrar solo símbolos operables o en vista activa
                symbols_to_process = [
                    sym for sym, state in self.symbol_states.items()
                    if state.get("contract_id") and (state.get("enabled", False) or sym == self.active_symbol)
                ]

                # 2. OBTENCIÓN EN BLOQUE / PARALELO DE PRECIOS VIVOS Y VELAS PARA TODOS LOS SÍMBOLOS ACTIVOS
                now_ts = time.time()
                def fetch_symbol_market_data(sym_key):
                    st = self.symbol_states[sym_key]
                    c_id = st.get("contract_id")
                    need_5m = (now_ts - st.get("last_5m_fetch_time", 0.0)) >= 15.0 or st.get("latest_df").empty
                    
                    df_5m_res = st.get("latest_df")
                    if need_5m:
                        df_fetched = self.client.retrieve_bars(c_id, unit=2, unit_number=5, limit=250, days_back=2)
                        if not df_fetched.empty:
                            df_5m_res = self.calcular_vwap_sesion(df_fetched)
                            with self.lock:
                                st["latest_df"] = df_5m_res
                                st["last_5m_fetch_time"] = now_ts

                    df_live_res = self.client.retrieve_bars(c_id, unit=2, unit_number=1, limit=1)
                    return sym_key, df_5m_res, df_live_res

                market_data = {}
                if symbols_to_process:
                    max_w = min(len(symbols_to_process), 4)
                    with ThreadPoolExecutor(max_workers=max_w) as executor:
                        for s_key, df_5m_res, df_live_res in executor.map(fetch_symbol_market_data, symbols_to_process):
                            market_data[s_key] = (df_5m_res, df_live_res)

                for sym in symbols_to_process:
                    if self.stop_event.is_set():
                        break

                    state = self.symbol_states[sym]
                    contract_id = state.get("contract_id")
                    if not contract_id or sym not in market_data:
                        continue

                    is_enabled = state.get("enabled", False)
                    df_5m, df_live = market_data[sym]

                    if df_live is not None and not df_live.empty:
                        live_bar = df_live.iloc[-1]
                        current_price = float(live_bar['Close'])
                        live_high = float(live_bar['High'])
                        live_low = float(live_bar['Low'])
                    elif df_5m is not None and not df_5m.empty:
                        live_bar = df_5m.iloc[-1]
                        current_price = float(live_bar['Close'])
                        live_high = float(live_bar['High'])
                        live_low = float(live_bar['Low'])
                    else:
                        continue

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

                    # RAMA B: BÚSQUEDA DE NUEVAS ENTRADAS (Si no hay posición y está habilitado)
                    elif is_enabled and df_5m is not None and not df_5m.empty:
                        # Respetar Cooldown y evitar reentradas repetidas en la misma vela
                        vela_5m_actual = df_5m.iloc[-1]
                        vela_ts = vela_5m_actual['Timestamp']
                        
                        if time.time() >= state["cooldown_until"] and state.get("last_executed_candle_ts") != vela_ts:
                            setup = self.verificar_setup_vwap(df_5m, sym, current_price=current_price)
                            if setup:
                                state["last_executed_candle_ts"] = vela_ts
                                self.log(
                                    f"⚡ [{sym}] ¡Gatillo VWAP activado! Precio: {current_price:.2f} tocó VWAP ({setup['vwap_cruce']:.2f}). "
                                    f"Abriendo {setup['direccion']}...",
                                    "ORDER", sym
                                )
                                self.ejecutar_orden(setup, sym)

                # Reporte periódico en terminal del estado de operaciones abiertas (cada 30s)
                if (now_ts - getattr(self, "_last_portfolio_log_time", 0.0)) >= 30.0:
                    self._last_portfolio_log_time = now_ts
                    open_summary = []
                    for s_k, s_st in self.symbol_states.items():
                        act_tr = s_st.get("active_trade")
                        if act_tr:
                            stg_desc = self.format_milestone(act_tr.get("sl_stage", 0))
                            mx_p = act_tr.get("max_pct_avance", 0.0)
                            open_summary.append(f"{s_k} {act_tr['direction']} @ {act_tr['entry_price']:.2f} [{stg_desc} (Max: {mx_p:.1f}%)]")
                    if open_summary:
                        self.log(f"📊 [CARTERA ABIERTA] {len(open_summary)} posición(es) en curso: {' | '.join(open_summary)}", "SYSTEM")

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

    def emergency_flatten(self, symbol_key: Optional[str] = None) -> Dict[str, Any]:
        """Cierra inmediatamente posiciones en el símbolo o activo."""
        sym = (symbol_key or self.active_symbol).upper()
        contract_id = self.symbol_states.get(sym, {}).get("contract_id")
        self.log(f"⚠️ [KILL SWITCH] Disparando cierre de emergencia para {sym}...", "ALERT", sym)
        try:
            res = self.client.close_contract_position(self.account_id, contract_id)
            self.log(f"🛡️ Cierre ejecutado en TopstepX: {res}", "ALERT", sym)
            self.cancelar_ordenes_abiertas_contrato(contract_id, sym)
            state = self.symbol_states.get(sym, {})
            state["has_open_position"] = False
            state["active_trade"] = None
            return res
        except Exception as e:
            self.log(f"❌ Error en cierre de emergencia: {e}", "ERROR", sym)
            return {"success": False, "error": str(e)}

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
