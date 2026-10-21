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

class AxiomEngine:
    def __init__(self, client: TopstepClient, default_symbol: str = DEFAULT_SYMBOL):
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
        self.csv_path: str = CSV_PATH
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
        """Formatea el hito alcanzado de forma descriptiva e infinita."""
        if stage >= 5:
            k = stage - 4
            trig_k = 135.0 + (k * 20.0)
            sl_k = 128.0 + (k * 20.0)
            tp_k = 170.0 + (k * 20.0)
            return f"Hito {stage} ({trig_k:.0f}% SL{sl_k:.0f}/TP{tp_k:.0f})"
        elif stage == 4:
            return "Hito 4 (135% SL128/TP170)"
        elif stage == 3:
            return "Hito 3 (120% SL108/TP150)"
        elif stage == 2:
            return "Hito 2 (70% SL45/TP120)"
        elif stage == 1:
            return "Hito 1 (40% BE+1)"
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
        
        df['VWAP'] = np.where(df['Cum_Vol'] > 0, df['Cum_PxV'] / df['Cum_Vol'], df['HLC3'])
        
        varianza = (df['Cum_Px2xV'] / df['Cum_Vol']) - (df['VWAP'] ** 2)
        df['Variance'] = np.maximum(varianza, 0)
        df['Std_Dev'] = np.sqrt(df['Variance'])
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

        GATILLO INMEDIATO (Al tocar o traspasar VWAP):
        - Para LONG: precio_actual <= VWAP_actual
        - Para SHORT: precio_actual >= VWAP_actual

        Sin análisis de volumen ni esperas adicionales.
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

        # 2. Evaluación de Gatillo al tocar o traspasar VWAP
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

    def tiene_posicion_abierta(self, sym: str) -> bool:
        """Verifica si el símbolo ya tiene una posición abierta activa."""
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
        precio_entrada = float(setup["precio_entrada"])
        d_base = float(setup["d_base"])
        
        # 1. SL Inicial: Universal en 25 ticks
        ticks_sl = INITIAL_SL_TICKS
        if direccion == "LONG":
            precio_sl = precio_entrada - (ticks_sl * tick_size)
            precio_tp = precio_entrada + d_base
            ticks_tp = max(round(d_base / tick_size), 10)
            side = 0
            tp_ticks_order = ticks_tp
        else: # SHORT
            precio_sl = precio_entrada + (ticks_sl * tick_size)
            precio_tp = precio_entrada - d_base
            ticks_tp = max(round(d_base / tick_size), 10)
            side = 1
            # Convención de signos TopstepX: SHORT -> TP negativo (-)
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
                sl_ticks=ticks_sl,
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
    def monitorear_posicion_abierta(self, sym: str, current_price: Optional[float] = None, df_5m: Optional[pd.DataFrame] = None):
        """
        Monitorea la posición cada 2 segundos y ejecuta la escalera dinámica de hitos:
        - Hito 1 (Avance >= 40% de D_base): Mueve SL a Entrada + 1 tick (Break-Even protegido).
        - Hito 2 (Avance >= 70% de D_base): Mueve SL al 45% de D_base y sube TP al 120% de D_base.
        - Hito 3 (Avance >= 120% de D_base): Mueve SL al 108% de D_base (90% de 120%) y sube TP al 150% (Límite).
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

        # Verificar si la posición sigue activa en TopstepX
        is_open = self.tiene_posicion_abierta(sym)
        if not is_open:
            self.log(f"🏁 [{sym}] Posición cerrada en mercado. Concluyendo seguimiento de {trade['direction']}.", "ORDER", sym)
            state["active_trade"] = None
            state["has_open_position"] = False
            state["cooldown_until"] = time.time() + 180
            return

        precio_actual = current_price
        vela_high = None
        vela_low = None
        
        if df_5m is not None and not df_5m.empty:
            vela = df_5m.iloc[-1]
            if precio_actual is None:
                precio_actual = float(vela['Close'])
            vela_high = float(vela.get('High', precio_actual))
            vela_low = float(vela.get('Low', precio_actual))
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

        # Calcular avance porcentual sobre la distancia base D_base
        if direccion == "LONG":
            precio_favorable = max(precio_actual, vela_high if vela_high is not None else precio_actual)
            ganancia_pts = precio_favorable - precio_entrada
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
                nuevo_sl = (precio_entrada + 1 * tick_size) if direccion == "LONG" else (precio_entrada - 1 * tick_size)
                nuevo_tp = None
                motivo_hito = f"HITO 1 (40% D_base) | SL a Break-Even + 1 tick ({nuevo_sl:.2f})"
            elif target_stage == 2:
                nuevo_sl = (precio_entrada + round((d_base * HITO_2_SL_PCT) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * HITO_2_SL_PCT) / tick_size) * tick_size)
                nuevo_tp = (precio_entrada + round((d_base * HITO_2_TP_EXPAND_PCT) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * HITO_2_TP_EXPAND_PCT) / tick_size) * tick_size)
                motivo_hito = f"HITO 2 (70% D_base) | SL al 45% ({nuevo_sl:.2f}) | TP Ampliado al 120% ({nuevo_tp:.2f})"
            elif target_stage == 3:
                nuevo_sl = (precio_entrada + round((d_base * HITO_3_SL_PCT) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * HITO_3_SL_PCT) / tick_size) * tick_size)
                nuevo_tp = (precio_entrada + round((d_base * HITO_3_TP_EXPAND_PCT) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * HITO_3_TP_EXPAND_PCT) / tick_size) * tick_size)
                motivo_hito = f"HITO 3 (120% D_base) | SL al 108% ({nuevo_sl:.2f}) | TP Ampliado al 150% ({nuevo_tp:.2f})"
            elif target_stage == 4:
                nuevo_sl = (precio_entrada + round((d_base * HITO_4_SL_PCT) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * HITO_4_SL_PCT) / tick_size) * tick_size)
                nuevo_tp = (precio_entrada + round((d_base * HITO_4_TP_EXPAND_PCT) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * HITO_4_TP_EXPAND_PCT) / tick_size) * tick_size)
                motivo_hito = f"HITO 4 (135% D_base) | SL al 128% ({nuevo_sl:.2f}) | TP Ampliado al 170% ({nuevo_tp:.2f})"
            else: # target_stage >= 5 (Infinito)
                k = target_stage - 4
                trig_k = HITO_4_TRIGGER_PCT + (k * RUNNER_STEP_PCT)
                sl_k_pct = HITO_4_SL_PCT + (k * (RUNNER_STEP_PCT / 100.0))
                tp_k_pct = HITO_4_TP_EXPAND_PCT + (k * (RUNNER_STEP_PCT / 100.0))
                
                nuevo_sl = (precio_entrada + round((d_base * sl_k_pct) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * sl_k_pct) / tick_size) * tick_size)
                nuevo_tp = (precio_entrada + round((d_base * tp_k_pct) / tick_size) * tick_size) if direccion == "LONG" else (precio_entrada - round((d_base * tp_k_pct) / tick_size) * tick_size)
                motivo_hito = f"HITO {target_stage} ({trig_k:.0f}% D_base) | SL al {sl_k_pct*100:.0f}% ({nuevo_sl:.2f}) | TP Ampliado al {tp_k_pct*100:.0f}% ({nuevo_tp:.2f})"

            trade["sl_stage"] = target_stage
            if nuevo_sl is not None:
                trade["current_sl_price"] = nuevo_sl
                self.mover_sl_en_topstep(contract_id, nuevo_sl, sym)
                
            if nuevo_tp is not None:
                trade["current_tp_price"] = nuevo_tp
                self.mover_tp_en_topstep(contract_id, nuevo_tp, sym)

            self.log(f"🏆 [{sym}] ¡{motivo_hito}! Recorrido máximo: {max_pct:.1f}%", "ORDER", sym)
            self.registrar_evento_be_en_csv(sym, trade, nuevo_sl or sl_actual, max_pct, motivo_hito)

        # SALVAGUARDA FAIL-SAFE POR SOFTWARE
        # Si ya se activó al menos el Hito 1 y el precio retrocede traspasando el SL protegido:
        if trade.get("sl_stage", 0) >= 1:
            sl_protegido = trade["current_sl_price"]
            sl_alcanzado = False
            if direccion == "LONG" and precio_actual <= sl_protegido:
                sl_alcanzado = True
            elif direccion == "SHORT" and precio_actual >= sl_protegido:
                sl_alcanzado = True
                
            if sl_alcanzado:
                self.log(f"🛡️ [{sym}] Precio tocó Stop protegido ({precio_actual:.2f} vs SL {sl_protegido:.2f}). Liquidando posición de inmediato...", "ORDER", sym)
                try:
                    self.client.close_contract_position(self.account_id, trade["contract_id"])
                except Exception as ce:
                    self.log(f"⚠️ Error al liquidar posición protegida en {sym}: {ce}", "ERROR", sym)
                state["active_trade"] = None
                state["has_open_position"] = False

    def mover_sl_en_topstep(self, contract_id: str, nuevo_sl: float, sym: str) -> bool:
        """Modifica la orden Stop abierta en TopstepX con el nuevo nivel de stopPrice."""
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
                    self.log(f"✅ [{sym}] Orden Stop #{order_id} ajustada en TopstepX a {nuevo_sl:.2f}.", "ORDER", sym)
                    return True
                else:
                    err_msg = res.get('errorMessage') or f"Código {res.get('errorCode')}"
                    self.log(f"⚠️ [{sym}] Respuesta al ajustar Stop #{order_id}: {err_msg}", "ERROR", sym)
            else:
                self.log(f"ℹ️ [{sym}] Orden Stop no encontrada en TopstepX. Salvaguarda activa en {nuevo_sl:.2f}.", "SYSTEM", sym)
        except Exception as e:
            self.log(f"⚠️ Error al mover SL en TopstepX: {e}", "ERROR", sym)
            
        return False

    def mover_tp_en_topstep(self, contract_id: str, nuevo_tp: float, sym: str) -> bool:
        """Modifica la orden Limit (Take Profit) abierta en TopstepX con el nuevo limitPrice."""
        if not self.account_id:
            return False
            
        try:
            open_orders = self.client.search_open_orders(self.account_id)
            tp_order = None
            for o in open_orders:
                cid = str(o.get("contractId", ""))
                otype = int(o.get("type", 0))
                # Type 1 = Limit Order (TP)
                if cid == str(contract_id) and otype == 1:
                    tp_order = o
                    break
                    
            if tp_order:
                order_id = tp_order.get("id")
                size = tp_order.get("size", 1)
                res = self.client.modify_order(
                    account_id=self.account_id,
                    order_id=order_id,
                    limit_price=nuevo_tp,
                    size=size
                )
                if res.get("success"):
                    self.log(f"✅ [{sym}] Orden Limit TP #{order_id} ampliada en TopstepX a {nuevo_tp:.2f}.", "ORDER", sym)
                    return True
                else:
                    err_msg = res.get('errorMessage') or f"Código {res.get('errorCode')}"
                    self.log(f"⚠️ [{sym}] Respuesta al ampliar TP #{order_id}: {err_msg}", "ERROR", sym)
            else:
                self.log(f"ℹ️ [{sym}] Orden Limit TP no encontrada en TopstepX.", "SYSTEM", sym)
        except Exception as e:
            self.log(f"⚠️ Error al mover TP en TopstepX: {e}", "ERROR", sym)
            
        return False

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
            
            direccion = "LONG" if (pos_type == 1 or raw_size > 0) else "SHORT"
            
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
            return adopted_trade
        except Exception as e:
            self.log(f"⚠️ Error al adoptar posición en {sym}: {e}", "ERROR", sym)
            return None

    # ==========================================
    # CSV AUDITING & TOPSTEP RECONCILIATION
    # ==========================================
    def registrar_evento_be_en_csv(self, sym: str, trade: Dict[str, Any], nuevo_sl: float, pct_avance: float, motivo: str):
        """Registra el avance de hitos en el archivo CSV institucional."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.csv_path)), exist_ok=True)
            archivo_existe = os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            
            fieldnames = [
                'Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                'Precio_Entrada_Real', 'Precio_Salida_Real', 'SL_Ticks', 'TP_Ticks',
                'Hito_Alcanzado', 'Max_Pct_TP',
                'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
            ]
            
            stage = trade.get("sl_stage", 1)
            hito_desc = self.format_milestone(stage)
            max_pct_str = f"{pct_avance:.1f}%"
            
            with open(self.csv_path, mode='a', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=';')
                if not archivo_existe:
                    writer.writeheader()
                writer.writerow({
                    'Fecha': fecha_actual,
                    'Hora': hora_actual,
                    'Simbolo': sym,
                    'Estado': f"SL PROTEGIDO ({hito_desc})",
                    'Direccion': trade["direction"],
                    'Precio_Mercado': f"{trade.get('current_price', nuevo_sl):.2f}",
                    'Precio_Entrada_Real': f"{trade['entry_price']:.2f}",
                    'Precio_Salida_Real': "PENDIENTE",
                    'SL_Ticks': str(trade.get("initial_sl_ticks", 25)),
                    'TP_Ticks': str(trade.get("tp_ticks", "Dinámico")),
                    'Hito_Alcanzado': hito_desc,
                    'Max_Pct_TP': max_pct_str,
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
        except Exception as e:
            self.log(f"❌ Error al escribir hito en CSV: {e}", "ERROR", sym)

    def registrar_en_csv(self, simbolo, direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id):
        """Guarda la orden emitida en el archivo CSV institucional."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.csv_path)), exist_ok=True)
            archivo_existe = os.path.isfile(self.csv_path) and os.path.getsize(self.csv_path) > 0
            
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
            
            fieldnames = [
                'Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado',
                'Precio_Entrada_Real', 'Precio_Salida_Real', 'SL_Ticks', 'TP_Ticks',
                'Hito_Alcanzado', 'Max_Pct_TP',
                'PnL_Bruto_USD', 'Comisiones_USD', 'PnL_Neto_USD', 'Order_ID',
                'Exit_Order_ID', 'Motivo_Rechazo', 'Codigo_Error', 'Condicion_Tecnica', 'Ultima_Actualizacion'
            ]
            
            with open(self.csv_path, mode='a', newline='', encoding='utf-8') as file:
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
                    'Hito_Alcanzado': hito_alcanzado,
                    'Max_Pct_TP': max_pct_tp,
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

    def sync_real_data_from_topstep(self) -> Dict[str, Any]:
        """Sincroniza balance real y operaciones cerradas desde TopstepX."""
        if not self.account_id:
            return self.stats
            
        try:
            balance = self.client.get_account_balance(self.account_id)
            self.stats["real_balance"] = balance
            
            raw_trades = self.client.search_trades(self.account_id, days_back=7)
            now_dt = datetime.now()
            self.last_sync_time = time.time()
            self.stats["last_sync"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            self.stats["next_sync"] = (now_dt + timedelta(seconds=self.sync_interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            
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
        1. Consulta precios actuales de todos los símbolos activos.
        2. Si hay posición abierta: Monitorea y ejecuta ajustes de SL y TP (Escalera de hitos).
        3. Si NO hay posición: Evalúa setup VWAP (Filtro previo 5m + Toque/Cruce actual) y dispara orden.
        4. Actualiza telemetría para WebSockets y dashboard.
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

                symbols_to_process = list(SUPPORTED_SYMBOLS.keys())

                for sym in symbols_to_process:
                    if self.stop_event.is_set():
                        break

                    state = self.symbol_states[sym]
                    contract_id = state.get("contract_id")
                    if not contract_id:
                        continue

                    is_enabled = state.get("enabled", False)
                    is_active_view = (sym == self.active_symbol)

                    # Solo procesamos símbolos habilitados o el que está en pantalla
                    if not is_enabled and not is_active_view:
                        continue

                    # 1. Actualizar historial de 5m periódicamente (cada 15s) para el cálculo de VWAP y Bandas
                    now_ts = time.time()
                    need_5m_fetch = (now_ts - state.get("last_5m_fetch_time", 0.0)) >= 15.0 or state.get("latest_df").empty
                    if need_5m_fetch:
                        df_5m = self.client.retrieve_bars(contract_id, unit=2, unit_number=5, limit=250, days_back=2)
                        if not df_5m.empty:
                            df_5m = self.calcular_vwap_sesion(df_5m)
                            with self.lock:
                                state["latest_df"] = df_5m
                                state["last_5m_fetch_time"] = now_ts
                    else:
                        df_5m = state.get("latest_df")

                    # 2. Consultar barra actual de 1m para obtener el precio vivo exacto
                    df_live = self.client.retrieve_bars(contract_id, unit=2, unit_number=1, limit=1)
                    if not df_live.empty:
                        live_bar = df_live.iloc[-1]
                        current_price = float(live_bar['Close'])
                        live_high = float(live_bar['High'])
                        live_low = float(live_bar['Low'])
                    elif not df_5m.empty:
                        live_bar = df_5m.iloc[-1]
                        current_price = float(live_bar['Close'])
                        live_high = float(live_bar['High'])
                        live_low = float(live_bar['Low'])
                    else:
                        continue

                    # Actualizar telemetría de símbolo
                    if not df_5m.empty:
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

                    # RAMA A: GESTIÓN DE POSICIÓN ABIERTA
                    if state.get("has_open_position") or state.get("active_trade"):
                        self.monitorear_posicion_abierta(sym, current_price=current_price, df_5m=df_5m)

                    # RAMA B: BÚSQUEDA DE NUEVAS ENTRADAS (Si no hay posición y está habilitado)
                    elif is_enabled and not df_5m.empty:
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
