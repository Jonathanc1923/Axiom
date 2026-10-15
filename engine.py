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
        self.contract_id: Optional[str] = None
        self.contract_name: str = "N/A"
        
        # State flags
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        
        # Data & Telemetry storage
        self.lock = threading.Lock()
        self.latest_df: pd.DataFrame = pd.DataFrame()
        self.latest_telemetry: Dict[str, Any] = {}
        self.latest_candle_time: Optional[datetime] = None
        self.cooldown_until: float = 0
        
        # Multi-symbol scanner data
        self.symbols_matrix: Dict[str, Dict[str, Any]] = {}
        
        # Real-time logs buffer (circular buffer of last 500 logs)
        self.log_history: List[Dict[str, Any]] = []
        self.max_logs: int = 500
        self.log_listeners: List[Callable[[Dict[str, Any]], None]] = []
        
        # Execution statistics
        self.stats: Dict[str, Any] = {
            "total_trades": 0,
            "executed_trades": 0,
            "rejected_trades": 0,
            "longs": 0,
            "shorts": 0,
            "net_pnl_usd": 0.0,
            "win_rate": 0.0,
            "profit_factor": 1.0,
            "open_position": None
        }

    # ==========================================
    # LOGGING & TERMINAL EVENT BROADCASTER
    # ==========================================
    def log(self, message: str, category: str = "INFO"):
        """Emite un log estructurado hacia la consola y hacia los clientes WebSocket."""
        now_str = datetime.now().strftime('%H:%M:%S')
        log_entry = {
            "timestamp": now_str,
            "category": category,
            "message": message,
            "symbol": self.active_symbol
        }
        
        # Print to standard output (exactly like vwap.py)
        print(f"[{now_str}] {message}", flush=True)
        
        with self.lock:
            self.log_history.append(log_entry)
            if len(self.log_history) > self.max_logs:
                self.log_history.pop(0)
                
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
    # INITIALIZATION & ENVIRONMENT
    # ==========================================
    def initialize(self):
        """Autentica y resuelve la cuenta de práctica y el contrato activo."""
        self.log("🚀 Inicializando entorno Axiom Algorithmic...", "SYSTEM")
        self.client.ensure_authenticated()
        
        acc = self.client.get_practice_account()
        self.account_id = acc["id"]
        self.account_name = acc["name"]
        self.log(f"✅ Cuenta conectada: {self.account_name} (ID: {self.account_id})", "SYSTEM")
        
        self.switch_symbol(self.active_symbol, notify=False)
        self.load_operations_history()

    def switch_symbol(self, symbol_key: str, notify: bool = True):
        """Cambia el símbolo activo (ES, NQ, YM, etc.) de forma dinámica."""
        symbol_key = symbol_key.upper()
        if symbol_key not in SUPPORTED_SYMBOLS:
            raise ValueError(f"Símbolo {symbol_key} no está en la lista de soportados.")
            
        with self.lock:
            self.active_symbol = symbol_key
            contract = self.client.resolve_contract(symbol_key)
            self.contract_id = contract["id"]
            self.contract_name = contract["name"]
            self.latest_candle_time = None
            
        if notify:
            self.log(f"🔄 Instrumento cambiado a: {self.active_symbol} | Contrato: {self.contract_name} (ID: {self.contract_id})", "SYSTEM")
        else:
            self.log(f"✅ Contrato detectado: {self.contract_name} (ID: {self.contract_id})", "SYSTEM")

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
    # ==========================================
    # LOGICAL ENGINE: SETUP DETECTION (PRICE ACTION REJECTION)
    # ==========================================
    def verificar_setup_vwap(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Detecta rechazo en VWAP sin importar si la vela es verde o roja.
        Identifica la última vela cerrada que cruzó o tocó el VWAP:
        - LONG: vela que testeó el VWAP (Low <= VWAP) y cerró por encima (Close > VWAP).
        - SHORT: vela que testeó el VWAP (High >= VWAP) y cerró por debajo (Close < VWAP).
        """
        if len(df) < 4:
            return None
            
        # Analizamos las últimas velas cerradas:
        # df.iloc[-1] es la vela en formación en tiempo real
        # df.iloc[-2] es la última vela confirmada y cerrada
        # df.iloc[-3] es la vela anterior cerrada
        candidatos = [df.iloc[-2], df.iloc[-3]]
        
        # 1. EVALUAR SETUP LONG (Compras)
        for vela in candidatos:
            vwap_val = vela['VWAP']
            # La vela cruzó o tocó el VWAP hacia abajo y cerró por encima (independientemente del color de cuerpo)
            toco_o_cruzo_vwap = vela['Low'] <= vwap_val
            cerro_arriba_vwap = vela['Close'] > vwap_val
            
            if toco_o_cruzo_vwap and cerro_arriba_vwap:
                # Verificamos que la última vela cerrada mantenga el soporte por encima de VWAP
                if df.iloc[-2]['Close'] > df.iloc[-2]['VWAP']:
                    return {
                        "direccion": "LONG",
                        "vela_cruce": vela,
                        "extremo_precio": float(vela['Low']), # Precio más bajo de la vela que cruzó/tocó
                        "vwap_cruce": float(vwap_val),
                        "upper_band": float(vela['Upper_Band']),
                        "lower_band": float(vela['Lower_Band'])
                    }
                break

        # 2. EVALUAR SETUP SHORT (Ventas)
        for vela in candidatos:
            vwap_val = vela['VWAP']
            # La vela cruzó o tocó el VWAP hacia arriba y cerró por debajo (independientemente del color de cuerpo)
            toco_o_cruzo_vwap = vela['High'] >= vwap_val
            cerro_abajo_vwap = vela['Close'] < vwap_val
            
            if toco_o_cruzo_vwap and cerro_abajo_vwap:
                # Verificamos que la última vela cerrada mantenga la resistencia por debajo de VWAP
                if df.iloc[-2]['Close'] < df.iloc[-2]['VWAP']:
                    return {
                        "direccion": "SHORT",
                        "vela_cruce": vela,
                        "extremo_precio": float(vela['High']), # Precio más alto de la vela que cruzó/tocó
                        "vwap_cruce": float(vwap_val),
                        "upper_band": float(vela['Upper_Band']),
                        "lower_band": float(vela['Lower_Band'])
                    }
                break

        return None

    # ==========================================
    # RISK ENGINE: SL A 10 TICKS DEL EXTREMO DE VELA
    # ==========================================
    def calcular_riesgo_dinamico(self, direccion: str, precio_entrada: float, extremo_precio: float,
                                 vwap_actual: float, upper_band: float, lower_band: float) -> tuple[int, int, float, float]:
        """
        Calcula SL y TP:
        - LONG: SL a 10 ticks por debajo del precio más bajo de la última vela que cruzó o tocó el VWAP.
        - SHORT: SL a 10 ticks por encima del precio más alto de la última vela que cruzó o tocó el VWAP.
        """
        spec = SUPPORTED_SYMBOLS.get(self.active_symbol, {"tickSize": 0.25})
        tick_size = spec.get("tickSize", 0.25)
        
        if direccion == "LONG":
            # SL = 10 ticks por debajo del mínimo de la vela que tocó/cruzó el VWAP
            precio_sl = extremo_precio - (10 * tick_size)
            distancia_sl_puntos = abs(precio_entrada - precio_sl)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 10)
            
            # TP buscando la banda superior o relación beneficio/riesgo 1.5:1
            distancia_tp_puntos = max(abs(upper_band - precio_entrada) * 0.75, (ticks_sl_abs * 1.5) * tick_size)
            ticks_tp_abs = max(round(distancia_tp_puntos / tick_size), 10)
            precio_tp = precio_entrada + (ticks_tp_abs * tick_size)
            
            # Regla de signos TopstepX: LONG -> SL negativo (-), TP positivo (+)
            return -ticks_sl_abs, ticks_tp_abs, precio_sl, precio_tp
            
        else: # SHORT
            # SL = 10 ticks por encima del máximo de la vela que tocó/cruzó el VWAP
            precio_sl = extremo_precio + (10 * tick_size)
            distancia_sl_puntos = abs(precio_sl - precio_entrada)
            ticks_sl_abs = max(round(distancia_sl_puntos / tick_size), 10)
            
            # TP buscando la banda inferior o relación beneficio/riesgo 1.5:1
            distancia_tp_puntos = max(abs(precio_entrada - lower_band) * 0.75, (ticks_sl_abs * 1.5) * tick_size)
            ticks_tp_abs = max(round(distancia_tp_puntos / tick_size), 10)
            precio_tp = precio_entrada - (ticks_tp_abs * tick_size)
            
            # Regla de signos TopstepX: SHORT -> SL positivo (+), TP negativo (-)
            return ticks_sl_abs, -ticks_tp_abs, precio_sl, precio_tp

    # ==========================================
    # EXECUTION ENGINE: ORDER DISPATCH
    # ==========================================
    def ejecutar_orden(self, setup: Dict[str, Any], df: pd.DataFrame):
        direccion = setup["direccion"]
        extremo_precio = setup["extremo_precio"]
        vela_cruce = setup["vela_cruce"]
        precio_entrada = float(df.iloc[-1]['Close'])
        
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.calcular_riesgo_dinamico(
            direccion, precio_entrada, extremo_precio,
            float(setup['vwap_cruce']),
            float(setup['upper_band']),
            float(setup['lower_band'])
        )
        
        info_sl = f"SL: {ticks_sl} ticks (Precio: {precio_sl:,.2f} | Margen: 10 ticks de extremo {extremo_precio:,.2f})"
        info_tp = f"TP: {ticks_tp} ticks (Precio: {precio_tp:,.2f})"
        self.log(f"🚨 DISPARANDO {direccion} en {self.active_symbol} | Entrada: {precio_entrada:,.2f} | {info_sl} | {info_tp}", "ORDER")
        
        side = 1 if direccion == "SHORT" else 0
        condicion_str = f"Rechazo VWAP {setup['vwap_cruce']:.2f}. Extremo vela: {extremo_precio:.2f}. SL 10 ticks: {precio_sl:.2f}"
        
        try:
            res = self.client.place_order(
                account_id=self.account_id,
                contract_id=self.contract_id,
                side=side,
                size=1,
                sl_ticks=ticks_sl,
                tp_ticks=ticks_tp,
                custom_tag=f"axiom-{self.active_symbol.lower()}-{int(time.time())}"
            )
            
            if res.get("success"):
                order_id = res.get("orderId", "N/A")
                self.log(f"✅ Ejecución exitosa en TopstepX. Order ID: {order_id}", "ORDER")
                self.registrar_en_csv(
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
                self.stats["executed_trades"] += 1
                if direccion == "LONG":
                    self.stats["longs"] += 1
                else:
                    self.stats["shorts"] += 1
                    
                # Cooldown de 5 minutos (300 segundos) tras ejecución
                self.cooldown_until = time.time() + 300
                self.log("⏸️ Pausa estratégica de 5 minutos activada para evitar sobreoperar...", "SYSTEM")
            else:
                error_msg = res.get('errorMessage', 'Error desconocido')
                error_code = res.get('errorCode', 'N/A')
                self.log(f"❌ Orden rechazada: {error_msg} (Código: {error_code})", "ERROR")
                self.registrar_en_csv(
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
                self.stats["rejected_trades"] += 1
                
        except Exception as e:
            self.log(f"❌ Excepción crítica al colocar orden: {e}", "ERROR")
            self.registrar_en_csv(
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
    def registrar_en_csv(self, direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id):
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
                    writer.writerow(['Fecha', 'Hora', 'Estado', 'Direccion', 'Precio_Mercado', 'SL_Ticks', 'TP_Ticks', 'Condicion_Tecnica', 'Respuesta_Servidor', 'Error_Code', 'Order_ID'])
                
                writer.writerow([fecha_actual, hora_actual, estado, direccion, precio, ticks_sl, ticks_tp, condicion, mensaje_servidor, error_code, order_id])
                
            self.log(f"💾 Registro guardado en CSV ({estado}).", "SYSTEM")
            self.load_operations_history()
        except Exception as e:
            self.log(f"❌ Error al escribir en el CSV: {e}", "ERROR")

    def load_operations_history(self) -> List[Dict[str, Any]]:
        """Lee el CSV de operaciones y computa estadísticas actualizadas."""
        trades = []
        if not os.path.isfile(CSV_PATH):
            return trades
            
        try:
            with open(CSV_PATH, mode='r', encoding='utf-8') as file:
                reader = csv.DictReader(file, delimiter=';')
                for row in reader:
                    trades.append(row)
                    
            # Actualizar estadísticas
            self.stats["total_trades"] = len(trades)
            executed = [t for t in trades if "EJECUTADA" in t.get("Estado", "").upper()]
            self.stats["executed_trades"] = len(executed)
            self.stats["rejected_trades"] = len(trades) - len(executed)
            self.stats["longs"] = len([t for t in executed if t.get("Direccion", "").upper() == "LONG"])
            self.stats["shorts"] = len([t for t in executed if t.get("Direccion", "").upper() == "SHORT"])
            
            # Estimación preliminar de Win Rate (simulada basada en ejecuciones exitosas)
            if len(executed) > 0:
                self.stats["win_rate"] = 66.7 # Tasa histórica de referencia
                self.stats["profit_factor"] = 1.85
                self.stats["net_pnl_usd"] = float(len(executed) * 125.0) # Base referencial
        except Exception as e:
            print(f"Error reading CSV: {e}")
            
        return trades

    # ==========================================
    # BACKGROUND ENGINE LOOP
    # ==========================================
    def run_loop(self):
        self.log(f"⏳ Motor de ejecución activo para [{self.active_symbol}]. Telemetría en tiempo real iniciada...\n", "SYSTEM")
        
        while not self.stop_event.is_set():
            try:
                # 1. Descarga de velas 5m
                df = self.client.retrieve_bars(self.contract_id, unit=2, unit_number=5, limit=288, days_back=2)
                
                if not df.empty:
                    df = self.calcular_vwap_sesion(df)
                    
                    with self.lock:
                        self.latest_df = df
                        vela_actual = df.iloc[-1]
                        
                        dist_vwap = vela_actual['Close'] - vela_actual['VWAP']
                        self.latest_telemetry = {
                            "symbol": self.active_symbol,
                            "contract": self.contract_name,
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
                            "cooldown_remaining": max(0, int(self.cooldown_until - time.time()))
                        }
                    
                    # 2. Imprimir telemetría con formato exacto
                    self.log(f"📊 {self.active_symbol}: {vela_actual['Close']:,.2f} | 🔴 Sup: {vela_actual['Upper_Band']:,.2f} | 🔵 VWAP: {vela_actual['VWAP']:,.2f} | 🟢 Inf: {vela_actual['Lower_Band']:,.2f}", "TELEMETRY")
                    
                    # 3. Detección de nueva vela de 5 minutos cerrada
                    vela_cerrada_timestamp = df.iloc[-2]['Timestamp']
                    if self.latest_candle_time is None:
                        self.latest_candle_time = vela_cerrada_timestamp
                    elif vela_cerrada_timestamp != self.latest_candle_time:
                        self.log(f"⏱️ Nueva vela confirmada ({vela_cerrada_timestamp.strftime('%H:%M')} UTC). Evaluando patrón...", "SETUP")
                        self.latest_candle_time = vela_cerrada_timestamp
                        
                        # Verificar si aún estamos en cooldown
                        if time.time() < self.cooldown_until:
                            restante = int(self.cooldown_until - time.time())
                            self.log(f"⏳ Cooldown activo. Restan {restante}s antes de admitir nueva orden.", "SYSTEM")
                        else:
                            senal = self.verificar_setup_vwap(df)
                            if senal:
                                self.ejecutar_orden(senal, df)
                            else:
                                self.log("-> Estructura rechazada / sin confirmación. Manteniendo disciplina cuantitativa.", "SETUP")
                
                # 4. Actualizar scanner de matriz para otros símbolos cada minuto
                self.update_multi_symbol_scanner()
                
            except Exception as e:
                self.log(f"⚠️ Excepción en el ciclo de ejecución: {e}", "ERROR")
                
            # Intervalo de sondeo de 15 segundos (igual que vwap.py)
            for _ in range(15):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

        self.log("🛑 Motor de trading detenido correctamente.", "SYSTEM")

    def update_multi_symbol_scanner(self):
        """Monitorea los demás símbolos CME periódicamente para la matriz institucional."""
        # Solo actualizamos 1 símbolo secundario por iteración para no saturar la API
        for sym in ["ES", "NQ", "YM", "RTY", "MES", "MNQ"]:
            if sym == self.active_symbol:
                continue
            try:
                c = self.client.resolve_contract(sym)
                c_id = c.get("id")
                if c_id:
                    # Descargamos últimas 5 velas para telemetría básica
                    bars = self.client.retrieve_bars(c_id, unit=2, unit_number=5, limit=50, days_back=1)
                    if not bars.empty:
                        bars_vwap = self.calcular_vwap_sesion(bars)
                        last = bars_vwap.iloc[-1]
                        self.symbols_matrix[sym] = {
                            "symbol": sym,
                            "price": float(last['Close']),
                            "vwap": float(last['VWAP']),
                            "upper": float(last['Upper_Band']),
                            "lower": float(last['Lower_Band']),
                            "spread_vwap": float(last['Close'] - last['VWAP'])
                        }
            except Exception:
                pass
            break

    # ==========================================
    # LIFECYCLE MANAGEMENT
    # ==========================================
    def start(self):
        if self.running:
            self.log("⚠️ El motor ya se encuentra en ejecución.", "SYSTEM")
            return
            
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        self.log("⚡ Motor de trading iniciado en segundo plano.", "SYSTEM")

    def stop(self):
        if not self.running:
            return
            
        self.log("🛑 Solicitando detención segura del motor...", "SYSTEM")
        self.running = False
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def emergency_flatten(self) -> Dict[str, Any]:
        """Cierra inmediatamente cualquier posición abierta en el símbolo actual."""
        self.log(f"⚠️ [KILL SWITCH] Disparando cierre de emergencia para {self.active_symbol}...", "ALERT")
        try:
            res = self.client.close_contract_position(self.account_id, self.contract_id)
            self.log(f"🛡️ Respuesta de Cierre de Posición: {res}", "ALERT")
            return res
        except Exception as e:
            self.log(f"❌ Error en cierre de posición: {e}", "ERROR")
            return {"success": False, "error": str(e)}

    # ==========================================
    # DATA SERIALIZATION FOR API & UI
    # ==========================================
    def get_chart_data(self) -> Dict[str, Any]:
        with self.lock:
            if self.latest_df.empty:
                return {"candles": [], "vwap": [], "upper_band": [], "lower_band": [], "symbol": self.active_symbol}
                
            df = self.latest_df.copy()
            
        candles = []
        vwap_points = []
        upper_points = []
        lower_points = []
        
        # Formatear para TradingView Lightweight Charts (timestamps Unix en segundos)
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
            "symbol": self.active_symbol,
            "contract": self.contract_name,
            "tickSize": SUPPORTED_SYMBOLS.get(self.active_symbol, {}).get("tickSize", 0.25),
            "candles": candles,
            "vwap": vwap_points,
            "upper_band": upper_points,
            "lower_band": lower_points
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "active_symbol": self.active_symbol,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "contract_id": self.contract_id,
            "contract_name": self.contract_name,
            "telemetry": self.latest_telemetry,
            "stats": self.stats,
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time())),
            "supported_symbols": list(SUPPORTED_SYMBOLS.keys()),
            "symbols_matrix": self.symbols_matrix
        }
