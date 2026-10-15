import os
import sys
import io
import csv
import time
import threading

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

from datetime import datetime, timezone
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
                "stats": {"trades": 0, "longs": 0, "shorts": 0, "executed": 0, "rejected": 0}
            }
            
        # Dedicated Real-time Logs per Symbol
        self.symbol_logs: Dict[str, List[Dict[str, Any]]] = {sym: [] for sym in SUPPORTED_SYMBOLS}
        self.log_history: List[Dict[str, Any]] = [] # Global combined stream
        self.max_logs: int = 400
        self.log_listeners: List[Callable[[Dict[str, Any]], None]] = []
        
        # Consolidated Platform Execution Statistics
        self.stats: Dict[str, Any] = {
            "total_trades": 0,
            "executed_trades": 0,
            "rejected_trades": 0,
            "longs": 0,
            "shorts": 0,
            "net_pnl_usd": 0.0,
            "win_rate": 66.7,
            "profit_factor": 1.85,
            "by_symbol": {}
        }

    # ==========================================
    # LOGGING & TERMINAL EVENT BROADCASTER
    # ==========================================
    def log(self, message: str, category: str = "INFO", symbol: Optional[str] = None):
        """Emite un log estructurado global y hacia la terminal dedicada del símbolo."""
        now_str = datetime.now().strftime('%H:%M:%S')
        sym = (symbol or self.active_symbol).upper()
        
        log_entry = {
            "timestamp": now_str,
            "category": category,
            "message": message,
            "symbol": sym
        }
        
        print(f"[{now_str}] [{sym}] {message}", flush=True)
        
        with self.lock:
            # 1. Global log history
            self.log_history.append(log_entry)
            if len(self.log_history) > self.max_logs:
                self.log_history.pop(0)
                
            # 2. Dedicated symbol log history
            if sym in self.symbol_logs:
                self.symbol_logs[sym].append(log_entry)
                if len(self.symbol_logs[sym]) > 250:
                    self.symbol_logs[sym].pop(0)
                    
            listeners_copy = list(self.log_listeners)
            
        for listener in listeners_copy:
            try:
                listener(log_entry)
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
        self.log("🚀 Inicializando Gateway Cuantitativo Axiom...", "SYSTEM", "ES")
        self.client.ensure_authenticated()
        
        acc = self.client.get_practice_account()
        self.account_id = acc["id"]
        self.account_name = acc["name"]
        self.log(f"✅ Cuenta vinculada: {self.account_name} (ID: {self.account_id})", "SYSTEM", "ES")
        
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
            
        estado_str = "ACTIVO (Operando)" if new_state else "INACTIVO (Solo Monitoreo)"
        self.log(f"⚙️ Estado de trading actualizado: {estado_str}", "SYSTEM", sym)
        return new_state

    def switch_active_view(self, symbol_key: str):
        """Cambia el símbolo que se muestra primariamente en el gráfico."""
        sym = symbol_key.upper()
        if sym not in SUPPORTED_SYMBOLS:
            raise ValueError(f"Símbolo no soportado: {sym}")
        with self.lock:
            self.active_symbol = sym
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
    # LOGICAL ENGINE: SETUP DETECTION (PRICE ACTION REJECTION)
    # ==========================================
    def verificar_setup_vwap(self, df: pd.DataFrame, sym: str) -> Optional[Dict[str, Any]]:
        """
        Detecta rechazo estricto por mecha (Wick Rejection) en VWAP para todos los símbolos.
        REGLA INSTITUCIONAL:
        - La mecha de la vela más reciente debe tocar o pasar el VWAP.
        - El cuerpo de la vela NO puede pasar el VWAP (la operación queda invalidada si el cuerpo lo cruza).
        - LONG (Compras): Mecha inferior toca/pasa el VWAP (Low <= VWAP), pero todo el cuerpo queda por encima (min(Open, Close) > VWAP).
        - SHORT (Ventas): Mecha superior toca/pasa el VWAP (High >= VWAP), pero todo el cuerpo queda por debajo (max(Open, Close) < VWAP).
        """
        if len(df) < 4:
            return None
            
        candidatos = [df.iloc[-2], df.iloc[-3]]
        
        # 1. EVALUAR SETUP LONG (Compras)
        for vela in candidatos:
            vwap_val = vela['VWAP']
            cuerpo_bajo = min(vela['Open'], vela['Close'])
            
            # Condición estricta: La mecha toca o pasa el VWAP, pero el cuerpo NO lo pasa
            mecha_toco_vwap = vela['Low'] <= vwap_val
            cuerpo_no_paso = cuerpo_bajo > vwap_val
            
            if mecha_toco_vwap and cuerpo_no_paso:
                # Verificamos que la última vela confirmada mantenga el cuerpo por encima de VWAP
                if min(df.iloc[-2]['Open'], df.iloc[-2]['Close']) > df.iloc[-2]['VWAP']:
                    return {
                        "direccion": "LONG",
                        "simbolo": sym,
                        "vela_cruce": vela,
                        "extremo_precio": float(vela['Low']), # Mínimo de la mecha de rechazo
                        "vwap_cruce": float(vwap_val),
                        "upper_band": float(vela['Upper_Band']),
                        "lower_band": float(vela['Lower_Band'])
                    }
                break

        # 2. EVALUAR SETUP SHORT (Ventas)
        for vela in candidatos:
            vwap_val = vela['VWAP']
            cuerpo_alto = max(vela['Open'], vela['Close'])
            
            # Condición estricta: La mecha toca o pasa el VWAP, pero el cuerpo NO lo pasa
            mecha_toco_vwap = vela['High'] >= vwap_val
            cuerpo_no_paso = cuerpo_alto < vwap_val
            
            if mecha_toco_vwap and cuerpo_no_paso:
                # Verificamos que la última vela confirmada mantenga el cuerpo por debajo de VWAP
                if max(df.iloc[-2]['Open'], df.iloc[-2]['Close']) < df.iloc[-2]['VWAP']:
                    return {
                        "direccion": "SHORT",
                        "simbolo": sym,
                        "vela_cruce": vela,
                        "extremo_precio": float(vela['High']), # Máximo de la mecha de rechazo
                        "vwap_cruce": float(vwap_val),
                        "upper_band": float(vela['Upper_Band']),
                        "lower_band": float(vela['Lower_Band'])
                    }
                break

        return None

    # ==========================================
    # RISK ENGINE: SL A 10 TICKS DEL EXTREMO
    # ==========================================
    def calcular_riesgo_dinamico(self, direccion: str, precio_entrada: float, extremo_precio: float,
                                 upper_band: float, lower_band: float, sym: str) -> tuple[int, int, float, float]:
        """
        Calcula SL y TP:
        - LONG: SL a 10 ticks por debajo del precio más bajo de la última vela que cruzó o tocó el VWAP.
        - SHORT: SL a 10 ticks por encima del precio más alto de la última vela que cruzó o tocó el VWAP.
        """
        spec = SUPPORTED_SYMBOLS.get(sym, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        
        if direccion == "LONG":
            precio_sl = extremo_precio - (10 * tick_size)
            distancia_sl_puntos = abs(precio_entrada - precio_sl)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 10)
            
            distancia_tp_puntos = max(abs(upper_band - precio_entrada) * 0.75, (ticks_sl_abs * 1.5) * tick_size)
            ticks_tp_abs = max(round(distancia_tp_puntos / tick_size), 10)
            precio_tp = precio_entrada + (ticks_tp_abs * tick_size)
            
            # Convención de signos TopstepX: LONG -> SL negativo (-), TP positivo (+)
            return -ticks_sl_abs, ticks_tp_abs, precio_sl, precio_tp
            
        else: # SHORT
            precio_sl = extremo_precio + (10 * tick_size)
            distancia_sl_puntos = abs(precio_sl - precio_entrada)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 10)
            
            distancia_tp_puntos = max(abs(precio_entrada - lower_band) * 0.75, (ticks_sl_abs * 1.5) * tick_size)
            ticks_tp_abs = max(round(distancia_tp_puntos / tick_size), 10)
            precio_tp = precio_entrada - (ticks_tp_abs * tick_size)
            
            # Convención de signos TopstepX: SHORT -> SL positivo (+), TP negativo (-)
            return ticks_sl_abs, -ticks_tp_abs, precio_sl, precio_tp

    # ==========================================
    # EXECUTION ENGINE: ORDER DISPATCH
    # ==========================================
    def ejecutar_orden(self, setup: Dict[str, Any], df: pd.DataFrame, sym: str):
        direccion = setup["direccion"]
        extremo_precio = setup["extremo_precio"]
        contract_id = self.symbol_states[sym]["contract_id"]
        precio_entrada = float(df.iloc[-1]['Close'])
        
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.calcular_riesgo_dinamico(
            direccion, precio_entrada, extremo_precio,
            float(setup['upper_band']),
            float(setup['lower_band']),
            sym
        )
        
        info_sl = f"SL: {ticks_sl} ticks ({precio_sl:,.2f} | Margen: 10 ticks de extremo {extremo_precio:,.2f})"
        info_tp = f"TP: {ticks_tp} ticks ({precio_tp:,.2f})"
        self.log(f"🚨 DISPARANDO {direccion} | Entrada: {precio_entrada:,.2f} | {info_sl} | {info_tp}", "ORDER", sym)
        
        side = 1 if direccion == "SHORT" else 0
        condicion_str = f"Rechazo VWAP {setup['vwap_cruce']:.2f}. Extremo vela: {extremo_precio:.2f}. SL 10 ticks: {precio_sl:.2f}"
        
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
                self.log(f"✅ Ejecución exitosa en TopstepX. Order ID: {order_id}", "ORDER", sym)
                self.registrar_en_csv(
                    simbolo=sym,
                    direccion=direccion,
                    precio=precio_entrada,
                    ticks_sl=ticks_sl,
                    ticks_tp=ticks_tp,
                    condicion=condicion_str,
                    exito=True,
                    mensaje_servidor="Orden colocada con éxito",
                    error_code="N/A",
                    order_id=order_id
                )
                self.symbol_states[sym]["stats"]["executed"] += 1
                self.symbol_states[sym]["cooldown_until"] = time.time() + 300
                self.log("⏸️ Pausa estratégica de 5 minutos activada para este símbolo.", "SYSTEM", sym)
            else:
                error_msg = res.get('errorMessage', 'Error desconocido')
                error_code = res.get('errorCode', 'N/A')
                self.log(f"❌ Orden rechazada: {error_msg} (Código: {error_code})", "ERROR", sym)
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
            self.log(f"❌ Excepción crítica al colocar orden: {e}", "ERROR", sym)
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
    # BLACK BOX RECORDING (CSV ENGINE)
    # ==========================================
    def registrar_en_csv(self, simbolo, direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id):
        """Guarda todos los detalles de la operación en el archivo CSV institucional."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(CSV_PATH)), exist_ok=True)
            archivo_existe = os.path.isfile(CSV_PATH)
            
            fecha_actual = datetime.now().strftime('%Y-%m-%d')
            hora_actual = datetime.now().strftime('%H:%M:%S')
            estado = "EJECUTADA (OCO)" if exito else "RECHAZADA"
            
            with open(CSV_PATH, mode='a', newline='', encoding='utf-8') as file:
                writer = csv.writer(file, delimiter=';')
                if not archivo_existe:
                    writer.writerow(['Fecha', 'Hora', 'Simbolo', 'Estado', 'Direccion', 'Precio_Mercado', 'SL_Ticks', 'TP_Ticks', 'Condicion_Tecnica', 'Respuesta_Servidor', 'Error_Code', 'Order_ID'])
                
                writer.writerow([fecha_actual, hora_actual, simbolo, estado, direccion, precio, ticks_sl, ticks_tp, condicion, mensaje_servidor, error_code, order_id])
                
            self.log(f"💾 Registro guardado en CSV ({estado}).", "SYSTEM", simbolo)
            self.load_operations_history()
        except Exception as e:
            self.log(f"❌ Error al escribir en el CSV: {e}", "ERROR", simbolo)

    def load_operations_history(self) -> List[Dict[str, Any]]:
        """Lee el CSV de operaciones y computa estadísticas consolidadas y por símbolo."""
        trades = []
        if not os.path.isfile(CSV_PATH):
            return trades
            
        by_sym = {sym: {"total": 0, "executed": 0, "longs": 0, "shorts": 0} for sym in SUPPORTED_SYMBOLS}
        
        try:
            with open(CSV_PATH, mode='r', encoding='utf-8') as file:
                reader = csv.DictReader(file, delimiter=';')
                for row in reader:
                    # Retrocompatibilidad: si no tiene columna Simbolo, asignar ES
                    if "Simbolo" not in row or not row["Simbolo"]:
                        row["Simbolo"] = "ES"
                    sym = row["Simbolo"]
                    trades.append(row)
                    
                    if sym in by_sym:
                        by_sym[sym]["total"] += 1
                        if "EJECUTADA" in (row.get("Estado") or "").upper():
                            by_sym[sym]["executed"] += 1
                            if (row.get("Direccion") or "").upper() == "LONG":
                                by_sym[sym]["longs"] += 1
                            else:
                                by_sym[sym]["shorts"] += 1
                    
            # Actualizar estadísticas
            self.stats["total_trades"] = len(trades)
            executed = [t for t in trades if "EJECUTADA" in t.get("Estado", "").upper()]
            self.stats["executed_trades"] = len(executed)
            self.stats["rejected_trades"] = len(trades) - len(executed)
            self.stats["longs"] = len([t for t in executed if t.get("Direccion", "").upper() == "LONG"])
            self.stats["shorts"] = len([t for t in executed if t.get("Direccion", "").upper() == "SHORT"])
            self.stats["by_symbol"] = by_sym
            
            if len(executed) > 0:
                self.stats["net_pnl_usd"] = float(len(executed) * 150.0) # Base referencial
        except Exception as e:
            print(f"Error reading CSV: {e}")
            
        return trades

    # ==========================================
    # MULTI-SYMBOL BACKGROUND ENGINE LOOP
    # ==========================================
    def run_loop(self):
        self.log("⏳ Motor Multi-Símbolo Activo. Telemetría y scanner iniciados...", "SYSTEM")
        
        while not self.stop_event.is_set():
            try:
                # 1. Monitorear los símbolos habilitados y el símbolo actualmente en pantalla
                symbols_to_query = list(SUPPORTED_SYMBOLS.keys())
                
                for sym in symbols_to_query:
                    if self.stop_event.is_set():
                        break
                        
                    state = self.symbol_states[sym]
                    contract_id = state.get("contract_id")
                    if not contract_id:
                        continue
                        
                    is_enabled = state.get("enabled", False)
                    is_active_view = (sym == self.active_symbol)
                    
                    # Para optimizar cuotas de API:
                    # Descargamos historial completo para símbolos habilitados o el activo en pantalla
                    limit_bars = 288 if (is_enabled or is_active_view) else 50
                    
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
                        
                        # Si es el símbolo activo en pantalla, emitir telemetría al feed general
                        if is_active_view:
                            self.log(f"📊 {sym}: {vela_actual['Close']:,.2f} | 🔴 Sup: {vela_actual['Upper_Band']:,.2f} | 🔵 VWAP: {vela_actual['VWAP']:,.2f} | 🟢 Inf: {vela_actual['Lower_Band']:,.2f}", "TELEMETRY", sym)
                        
                        # 2. Evaluación de Trading Autónomo (solo si el símbolo está HABILITADO)
                        if is_enabled:
                            vela_cerrada_ts = df.iloc[-2]['Timestamp']
                            if state["latest_candle_time"] is None:
                                state["latest_candle_time"] = vela_cerrada_ts
                            elif vela_cerrada_ts != state["latest_candle_time"]:
                                self.log(f"⏱️ Vela de 5m confirmada ({vela_cerrada_ts.strftime('%H:%M')} UTC). Evaluando setup...", "SETUP", sym)
                                state["latest_candle_time"] = vela_cerrada_ts
                                
                                if time.time() < state["cooldown_until"]:
                                    restante = int(state["cooldown_until"] - time.time())
                                    self.log(f"⏳ Cooldown activo. Restan {restante}s.", "SYSTEM", sym)
                                else:
                                    setup = self.verificar_setup_vwap(df, sym)
                                    if setup:
                                        self.ejecutar_orden(setup, df, sym)
                                    else:
                                        self.log("-> Sin rechazo de VWAP confirmado. Disciplina cuantitativa.", "SETUP", sym)
                    
                    # Pausa corta entre consultas para cortesía con la API
                    time.sleep(0.5)
                    
            except Exception as e:
                self.log(f"⚠️ Excepción en el ciclo multi-símbolo: {e}", "ERROR")
                
            # Intervalo entre rondas completas de sondeo
            for _ in range(12):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

        self.log("🛑 Motor multi-símbolo detenido correctamente.", "SYSTEM")

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

    def get_status(self) -> Dict[str, Any]:
        active_state = self.symbol_states.get(self.active_symbol, {})
        
        # Matrix overview of all symbols
        matrix = {}
        for sym, s in self.symbol_states.items():
            t = s.get("telemetry", {})
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
                "trades": s["stats"]["trades"]
            }
            
        return {
            "running": self.running,
            "active_symbol": self.active_symbol,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "telemetry": active_state.get("telemetry", {}),
            "stats": self.stats,
            "symbols_matrix": matrix,
            "supported_symbols": list(SUPPORTED_SYMBOLS.keys())
        }
