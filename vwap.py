import pandas as pd
import numpy as np
import requests
import time
import os
import csv
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. CREDENCIALES Y CONFIGURACIÓN BASE
# ==========================================
API_BASE_URL = "https://api.topstepx.com"
USERNAME = "jonathanencina2020@gmail.com" 
API_KEY = "O/Jvyz5VggKPKRLF69YDGJQjXIcxsKuxZbJg9rgwek0="

# Configuración del CSV de Registro
CSV_DIR = r"A:\topstetp2"
CSV_PATH = os.path.join(CSV_DIR, "registro_operaciones.csv")

# Variables de estado global
TOKEN = ""
ACCOUNT_ID = None
CONTRACT_ID = ""

# ==========================================
# 1.5. MOTOR DE REGISTRO CSV (CAJA NEGRA)
# ==========================================
def registrar_en_csv(direccion, precio, ticks_sl, ticks_tp, condicion, exito, mensaje_servidor, error_code, order_id):
    """Guarda todos los detalles de la operación o intento fallido en un archivo CSV."""
    try:
        os.makedirs(CSV_DIR, exist_ok=True)
        archivo_existe = os.path.isfile(CSV_PATH)
        
        fecha_actual = datetime.now().strftime('%Y-%m-%d')
        hora_actual = datetime.now().strftime('%H:%M:%S')
        estado = "EJECUTADA (OCO)" if exito else "RECHAZADA"
        
        with open(CSV_PATH, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file, delimiter=';')
            if not archivo_existe:
                writer.writerow(['Fecha', 'Hora', 'Estado', 'Direccion', 'Precio_Mercado', 'SL_Ticks', 'TP_Ticks', 'Condicion_Tecnica', 'Respuesta_Servidor', 'Error_Code', 'Order_ID'])
            
            writer.writerow([fecha_actual, hora_actual, estado, direccion, precio, ticks_sl, ticks_tp, condicion, mensaje_servidor, error_code, order_id])
            
        print(f"[{hora_actual}] 💾 Registro guardado en CSV ({estado}).", flush=True)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error al intentar escribir en el CSV: {e}", flush=True)

# ==========================================
# 2. MOTOR DE AUTENTICACIÓN Y ENTORNO
# ==========================================
def obtener_token():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Autenticando con la API de TopstepX...", flush=True)
    url = f"{API_BASE_URL}/api/Auth/loginKey"
    res = requests.post(url, json={"userName": USERNAME, "apiKey": API_KEY}, headers={"Content-Type": "application/json"})
    data = res.json()
    if data.get("success"):
        print("✅ Autenticación exitosa.", flush=True)
        return data["token"]
    else:
        raise Exception(f"Fallo de Autenticación: {data.get('errorMessage')}")

def get_headers():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def inicializar_entorno():
    global ACCOUNT_ID, CONTRACT_ID
    
    # Buscar Cuenta (Filtro PRAC con cuenta específica)
    TARGET_ACCOUNT = "PRAC-V2-460183-51765674"
    res_acc = requests.post(f"{API_BASE_URL}/api/Account/search", json={"onlyActiveAccounts": True}, headers=get_headers())
    cuentas = res_acc.json().get("accounts", [])
    if not cuentas: raise Exception("No se encontraron cuentas activas.")
    
    cuenta_seleccionada = next((c for c in cuentas if TARGET_ACCOUNT in c["name"].upper() and c.get("canTrade", False)), None)
    if not cuenta_seleccionada:
        cuenta_seleccionada = next((c for c in cuentas if ("PRAC" in c["name"].upper() or "PRACTICE" in c["name"].upper()) and c.get("canTrade", False)), None)
    if not cuenta_seleccionada:
        cuenta_seleccionada = next((c for c in cuentas if "PRAC" in c["name"].upper() or "PRACTICE" in c["name"].upper()), None)
    if not cuenta_seleccionada: raise Exception("Seguridad: No se detectó tu cuenta PRACTICE.")
        
    ACCOUNT_ID = cuenta_seleccionada["id"]
    print(f"✅ Cuenta de Práctica asegurada: {cuenta_seleccionada['name']} | ID: {ACCOUNT_ID}", flush=True)
    
    # Buscar Contrato ES
    res_cont = requests.post(f"{API_BASE_URL}/api/Contract/search", json={"live": False, "searchText": "ES"}, headers=get_headers())
    contratos = res_cont.json().get("contracts", [])
    contrato_seleccionado = next((c for c in contratos if "F.US.EP" in c["symbolId"] and c["activeContract"]), None)
    
    if not contrato_seleccionado: raise Exception("No se encontró contrato ES activo.")
    CONTRACT_ID = contrato_seleccionado["id"]
    print(f"✅ Contrato ES detectado: {contrato_seleccionado['name']} | ID: {CONTRACT_ID}", flush=True)

# ==========================================
# 3. MOTOR DE DATOS
# ==========================================
def obtener_velas():
    ahora_utc = datetime.now(timezone.utc)
    payload = {
        "contractId": CONTRACT_ID, "live": False, "unit": 2, "unitNumber": 5, "limit": 276, 
        "includePartialBar": True,
        "startTime": (ahora_utc - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": ahora_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    try:
        res = requests.post(f"{API_BASE_URL}/api/History/retrieveBars", json=payload, headers=get_headers())
        data = res.json()
        if data.get("success") and data.get("bars"):
            df = pd.DataFrame(data["bars"])
            df = df.rename(columns={'t': 'Timestamp', 'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
            
            # Orden cronológico estricto
            df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True)
            return df.sort_values(by='Timestamp').reset_index(drop=True)
        return pd.DataFrame()
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Error de conexión: {e}", flush=True)
        return pd.DataFrame()

# ==========================================
# 4. MOTOR MATEMÁTICO INSTITUCIONAL (TRADINGVIEW VWAP)
# ==========================================
def calcular_vwap_sesion(df):
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
# 5. MOTOR LÓGICO DE RECHAZO: MECHA EN TIEMPO REAL (2 VELAS PREVIAS CONFIRMADAS)
# ==========================================
def verificar_setup_vwap(df, ignore_time_window=False):
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
        
    TICK_SIZE = 0.25
    vela_actual = df.iloc[-1]
    vela_prev1 = df.iloc[-2]  # Vela previa (t-1)
    vela_prev2 = df.iloc[-3]  # Vela anteprevia (t-2)
    
    # Filtro temporal: La vela debe tener al menos 8 segundos de vida y no haber expirado (> 305s)
    if not ignore_time_window:
        now_utc = datetime.now(timezone.utc)
        vela_ts = vela_actual['Timestamp']
        if getattr(vela_ts, 'tzinfo', None) is None:
            vela_ts = vela_ts.replace(tzinfo=timezone.utc)
        elapsed_sec = (now_utc - vela_ts).total_seconds()
        
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
    t2_arriba = float(vela_prev2['Low']) > p2_vwap
    t1_arriba = float(vela_prev1['Low']) > p1_vwap
    previas_confirman_long = t2_arriba and t1_arriba
    
    # A. La vela abrió por encima del VWAP
    abrio_arriba = (open_val >= vwap_val - (0.5 * TICK_SIZE))
    
    # B. En algún momento transcurrido, tocó o perforó el VWAP
    mecha_toco_vwap_long = (low_val <= vwap_val)
    
    # C. El cuerpo actual NO está cruzando el VWAP (todo el cuerpo está por encima)
    cuerpo_bajo = min(open_val, close_val)
    cuerpo_no_cruzo_long = (cuerpo_bajo >= vwap_val - (0.5 * TICK_SIZE))
    
    # D. Confirmación de MECHA vs CUERPO (Bajas posibilidades de que sea cuerpo):
    mecha_inferior = cuerpo_bajo - low_val
    mecha_sustancial_long = (mecha_inferior >= 2 * TICK_SIZE)
    rebote_confirmado_long = (close_val - low_val >= 2 * TICK_SIZE)
    precio_seguro_long = (close_val >= vwap_val + (1 * TICK_SIZE))
    tamano_cuerpo_long = abs(close_val - open_val)
    patron_rechazo_long = (close_val >= open_val) or (mecha_inferior >= tamano_cuerpo_long * 0.5)
    
    # E. Proximidad de entrada: el precio actual no se ha ido excesivamente lejos del VWAP
    en_zona_entrada_long = (close_val <= vwap_val + max(12 * TICK_SIZE, abs(float(vela_actual['Upper_Band']) - vwap_val) * 0.40))
    
    if (previas_confirman_long and abrio_arriba and mecha_toco_vwap_long and 
        cuerpo_no_cruzo_long and mecha_sustancial_long and rebote_confirmado_long and 
        precio_seguro_long and patron_rechazo_long and en_zona_entrada_long):
        extremo = min(low_val, vwap_val)
        return {
            "direccion": "LONG",
            "vela_cruce": vela_actual,
            "extremo_precio": extremo,
            "vwap_cruce": vwap_val,
            "upper_band": float(vela_actual['Upper_Band']),
            "lower_band": float(vela_actual['Lower_Band']),
            "mecha_pts": mecha_inferior
        }

    # 2. EVALUAR SETUP SHORT (Ventas)
    t2_abajo = float(vela_prev2['High']) < p2_vwap
    t1_abajo = float(vela_prev1['High']) < p1_vwap
    previas_confirman_short = t2_abajo and t1_abajo
    
    # A. La vela abrió por debajo del VWAP
    abrio_abajo = (open_val <= vwap_val + (0.5 * TICK_SIZE))
    
    # B. En algún momento transcurrido, tocó o perforó el VWAP
    mecha_toco_vwap_short = (high_val >= vwap_val)
    
    # C. El cuerpo actual NO está cruzando el VWAP (todo el cuerpo está por debajo)
    cuerpo_alto = max(open_val, close_val)
    cuerpo_no_cruzo_short = (cuerpo_alto <= vwap_val + (0.5 * TICK_SIZE))
    
    # D. Confirmación de MECHA vs CUERPO (Bajas posibilidades de que sea cuerpo):
    mecha_superior = high_val - cuerpo_alto
    mecha_sustancial_short = (mecha_superior >= 2 * TICK_SIZE)
    rechazo_confirmado_short = (high_val - close_val >= 2 * TICK_SIZE)
    precio_seguro_short = (close_val <= vwap_val - (1 * TICK_SIZE))
    tamano_cuerpo_short = abs(close_val - open_val)
    patron_rechazo_short = (close_val <= open_val) or (mecha_superior >= tamano_cuerpo_short * 0.5)
    
    # E. Proximidad de entrada: el precio actual no se ha ido excesivamente lejos del VWAP
    en_zona_entrada_short = (close_val >= vwap_val - max(12 * TICK_SIZE, abs(vwap_val - float(vela_actual['Lower_Band'])) * 0.40))
    
    if (previas_confirman_short and abrio_abajo and mecha_toco_vwap_short and 
        cuerpo_no_cruzo_short and mecha_sustancial_short and rechazo_confirmado_short and 
        precio_seguro_short and patron_rechazo_short and en_zona_entrada_short):
        extremo = max(high_val, vwap_val)
        return {
            "direccion": "SHORT",
            "vela_cruce": vela_actual,
            "extremo_precio": extremo,
            "vwap_cruce": vwap_val,
            "upper_band": float(vela_actual['Upper_Band']),
            "lower_band": float(vela_actual['Lower_Band']),
            "mecha_pts": mecha_superior
        }

    return None

# ==========================================
# 6. MOTOR DE EJECUCIÓN (SL A 10 TICKS DEL VWAP Y TP AL 40% DISTANCIA VWAP-BANDA)
# ==========================================
def calcular_riesgo_dinamico(direccion, precio_entrada, extremo_precio, upper_band, lower_band, vwap_val=None):
    """
    Calcula SL y TP:
    - SL: Exactamente 10 ticks sobre o debajo del VWAP del momento:
          LONG: 10 ticks por debajo del VWAP (precio_sl = vwap - 10 ticks).
          SHORT: 10 ticks por encima del VWAP (precio_sl = vwap + 10 ticks).
    - TP: 40% de la distancia entre la línea VWAP y la siguiente banda
          si es superior a 10 ticks; de lo contrario, TP estático de 10 ticks.
    """
    TICK_SIZE = 0.25
    ref_vwap = vwap_val if vwap_val is not None else precio_entrada
    
    if direccion == "LONG":
        # SL: 10 ticks por debajo del VWAP del momento
        precio_sl = ref_vwap - (10 * TICK_SIZE)
        distancia_sl_puntos = abs(precio_entrada - precio_sl)
        ticks_sl_abs = max(round(distancia_sl_puntos / TICK_SIZE), 10)
        
        # TP: 40% de distancia entre VWAP y Banda Superior (min 10 ticks)
        distancia_banda = abs(upper_band - ref_vwap)
        ticks_40_pct = round((distancia_banda * 0.40) / TICK_SIZE)
        ticks_tp_abs = ticks_40_pct if ticks_40_pct > 10 else 10
        precio_tp = precio_entrada + (ticks_tp_abs * TICK_SIZE)
        
        return -ticks_sl_abs, ticks_tp_abs, precio_sl, precio_tp
    else:
        # SL: 10 ticks por encima del VWAP del momento
        precio_sl = ref_vwap + (10 * TICK_SIZE)
        distancia_sl_puntos = abs(precio_sl - precio_entrada)
        ticks_sl_abs = max(round(distancia_sl_puntos / TICK_SIZE), 10)
        
        # TP: 40% de distancia entre VWAP y Banda Inferior (min 10 ticks)
        distancia_banda = abs(ref_vwap - lower_band)
        ticks_40_pct = round((distancia_banda * 0.40) / TICK_SIZE)
        ticks_tp_abs = ticks_40_pct if ticks_40_pct > 10 else 10
        precio_tp = precio_entrada - (ticks_tp_abs * TICK_SIZE)
        
        return ticks_sl_abs, -ticks_tp_abs, precio_sl, precio_tp

def ejecutar_orden(setup, df):
    direccion = setup["direccion"]
    extremo_precio = setup["extremo_precio"]
    vwap_cruce = setup.get("vwap_cruce")
    vela_cruce = setup["vela_cruce"]
    precio_entrada = float(df.iloc[-1]['Close'])
    
    ticks_sl, ticks_tp, precio_sl, precio_tp = calcular_riesgo_dinamico(
        direccion, precio_entrada, extremo_precio,
        float(setup['upper_band']),
        float(setup['lower_band']),
        vwap_cruce
    )
    
    info_sl = f"SL: {abs(ticks_sl)} ticks ({precio_sl:,.2f} | 10 ticks desde VWAP)"
    info_tp = f"TP: {abs(ticks_tp)} ticks ({precio_tp:,.2f} | 40% distancia VWAP-Banda / Mín 10t)"
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚨 DISPARANDO {direccion} | Entrada: {precio_entrada:,.2f} | {info_sl} | {info_tp}", flush=True)
    
    payload = {
        "accountId": ACCOUNT_ID,
        "contractId": CONTRACT_ID,
        "type": 2, # Orden a Mercado
        "side": 1 if direccion == "SHORT" else 0, 
        "size": 1, # Lote fijo
        "stopLossBracket": {"ticks": ticks_sl, "type": 4},
        "takeProfitBracket": {"ticks": ticks_tp, "type": 1}
    }
    
    condicion_str = f"Mecha VWAP {setup['vwap_cruce']:.2f} ({setup.get('mecha_pts', 0)/TICK_SIZE:.0f}t Mecha Confirmada | 2V Previas). SL: 10t VWAP ({precio_sl:.2f}) | TP 40% Banda: {abs(ticks_tp)}t ({precio_tp:.2f})"
    
    res = requests.post(f"{API_BASE_URL}/api/Order/place", json=payload, headers=get_headers())
    data = res.json()
    
    if data.get("success"):
        print(f"✅ Ejecución exitosa. Order ID: {data['orderId']}", flush=True)
        registrar_en_csv(direccion, precio_entrada, ticks_sl, ticks_tp, condicion_str, True, "Orden colocada con éxito", "N/A", data['orderId'])
    else:
        error_msg = data.get('errorMessage', 'Error desconocido')
        error_code = data.get('errorCode', 'N/A')
        print(f"❌ Orden rechazada: {error_msg} (Code {error_code})", flush=True)
        registrar_en_csv(direccion, precio_entrada, ticks_sl, ticks_tp, condicion_str, False, error_msg, error_code, "N/A")

# ==========================================
# 7. CICLO PRINCIPAL
# ==========================================
def main():
    global TOKEN
    try:
        TOKEN = obtener_token()
        inicializar_entorno()
    except Exception as e:
        print(f"❌ Error de inicialización: {e}", flush=True)
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ Motor de ejecución activo. Iniciando telemetría en tiempo real...\n", flush=True)
    last_executed_ts = None
    
    while True:
        try:
            df = obtener_velas()
            if not df.empty:
                df = calcular_vwap_sesion(df)
                vela_actual = df.iloc[-1]
                vela_ts = vela_actual['Timestamp']
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 ES: {vela_actual['Close']:,.2f} | 🔴 Sup: {vela_actual['Upper_Band']:,.2f} | 🔵 VWAP: {vela_actual['VWAP']:,.2f} | 🟢 Inf: {vela_actual['Lower_Band']:,.2f}", flush=True)
                
                if last_executed_ts != vela_ts:
                    senal = verificar_setup_vwap(df)
                    if senal:
                        last_executed_ts = vela_ts
                        print(f"⚡ ¡Mecha de rechazo en VWAP confirmada en tiempo real! Abriendo {senal['direccion']}...", flush=True)
                        ejecutar_orden(senal, df)
                        print("⏸️ Pausa estratégica de 5 minutos activada...\n", flush=True)
                        time.sleep(300)
            
            time.sleep(2) 
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Excepción en el ciclo principal: {e}", flush=True)
            time.sleep(2)

if __name__ == "__main__":
    main()