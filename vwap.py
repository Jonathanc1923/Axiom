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
    
    # Buscar Cuenta (Filtro PRAC estricto)
    res_acc = requests.post(f"{API_BASE_URL}/api/Account/search", json={"onlyActiveAccounts": True}, headers=get_headers())
    cuentas = res_acc.json().get("accounts", [])
    if not cuentas: raise Exception("No se encontraron cuentas activas.")
    
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
# 5. MOTOR LÓGICO DE RECHAZO (INDEPENDIENTE DEL COLOR)
# ==========================================
def verificar_setup_vwap(df):
    """
    Detecta rechazo en VWAP sin importar si la vela es verde o roja.
    Identifica la última vela cerrada que cruzó o tocó el VWAP:
    - LONG: vela que testeó el VWAP (Low <= VWAP) y cerró por encima (Close > VWAP).
    - SHORT: vela que testeó el VWAP (High >= VWAP) y cerró por debajo (Close < VWAP).
    """
    if len(df) < 4: return None
        
    candidatos = [df.iloc[-2], df.iloc[-3]]
    
    # 1. SETUP LONG (Compras)
    for vela in candidatos:
        vwap_val = vela['VWAP']
        toco_o_cruzo_vwap = vela['Low'] <= vwap_val
        cerro_arriba_vwap = vela['Close'] > vwap_val
        
        if toco_o_cruzo_vwap and cerro_arriba_vwap:
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

    # 2. SETUP SHORT (Ventas)
    for vela in candidatos:
        vwap_val = vela['VWAP']
        toco_o_cruzo_vwap = vela['High'] >= vwap_val
        cerro_abajo_vwap = vela['Close'] < vwap_val
        
        if toco_o_cruzo_vwap and cerro_abajo_vwap:
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
# 6. MOTOR DE EJECUCIÓN (CON SL A 5 TICKS DEL EXTREMO)
# ==========================================
def calcular_riesgo_dinamico(direccion, precio_entrada, extremo_precio, upper_band, lower_band):
    TICK_SIZE = 0.25
    
    # REGLA EXACTA:
    # Compras: SL 10 ticks por debajo del precio más bajo de la última vela que cruzó/tocó el VWAP
    # Ventas: SL 10 ticks por encima del precio más alto de la última vela que cruzó/tocó el VWAP
    if direccion == "LONG":
        precio_sl = extremo_precio - (10 * TICK_SIZE)
        distancia_sl = abs(precio_entrada - precio_sl)
        ticks_sl_abs = max(round(distancia_sl / TICK_SIZE), 10)
        
        distancia_tp = max(abs(upper_band - precio_entrada) * 0.75, (ticks_sl_abs * 1.5) * TICK_SIZE)
        ticks_tp_abs = max(round(distancia_tp / TICK_SIZE), 10)
        precio_tp = precio_entrada + (ticks_tp_abs * TICK_SIZE)
        
        return -ticks_sl_abs, ticks_tp_abs, precio_sl, precio_tp
    else:
        precio_sl = extremo_precio + (10 * TICK_SIZE)
        distancia_sl = abs(precio_sl - precio_entrada)
        ticks_sl_abs = max(round(distancia_sl / TICK_SIZE), 10)
        
        distancia_tp = max(abs(precio_entrada - lower_band) * 0.75, (ticks_sl_abs * 1.5) * TICK_SIZE)
        ticks_tp_abs = max(round(distancia_tp / TICK_SIZE), 10)
        precio_tp = precio_entrada - (ticks_tp_abs * TICK_SIZE)
        
        return ticks_sl_abs, -ticks_tp_abs, precio_sl, precio_tp

def ejecutar_orden(setup, df):
    direccion = setup["direccion"]
    extremo_precio = setup["extremo_precio"]
    vela_cruce = setup["vela_cruce"]
    precio_entrada = float(df.iloc[-1]['Close'])
    
    ticks_sl, ticks_tp, precio_sl, precio_tp = calcular_riesgo_dinamico(
        direccion, precio_entrada, extremo_precio,
        float(setup['upper_band']),
        float(setup['lower_band'])
    )
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚨 DISPARANDO {direccion} | Entrada: {precio_entrada:,.2f} | SL: {ticks_sl} ticks ({precio_sl:,.2f}, 10 ticks de extremo {extremo_precio:,.2f}) | TP: {ticks_tp} ticks ({precio_tp:,.2f})", flush=True)
    
    payload = {
        "accountId": ACCOUNT_ID,
        "contractId": CONTRACT_ID,
        "type": 2, # Orden a Mercado
        "side": 1 if direccion == "SHORT" else 0, 
        "size": 1, # Lote fijo
        "stopLossBracket": {"ticks": ticks_sl, "type": 4},
        "takeProfitBracket": {"ticks": ticks_tp, "type": 1}
    }
    
    condicion_str = f"Rechazo VWAP {setup['vwap_cruce']:.2f}. Extremo vela: {extremo_precio:.2f}. SL 10 ticks: {precio_sl:.2f}"
    
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
    ultima_vela_timestamp = None
    
    while True:
        try:
            df = obtener_velas()
            if not df.empty:
                df = calcular_vwap_sesion(df)
                vela_actual = df.iloc[-1]
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 ES: {vela_actual['Close']:,.2f} | 🔴 Sup: {vela_actual['Upper_Band']:,.2f} | 🔵 VWAP: {vela_actual['VWAP']:,.2f} | 🟢 Inf: {vela_actual['Lower_Band']:,.2f}", flush=True)
                
                vela_cerrada_timestamp = df.iloc[-2]['Timestamp']
                if ultima_vela_timestamp is None:
                    ultima_vela_timestamp = vela_cerrada_timestamp
                elif vela_cerrada_timestamp != ultima_vela_timestamp:
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⏱️ Nueva vela confirmada. Evaluando patrón...", flush=True)
                    ultima_vela_timestamp = vela_cerrada_timestamp
                    
                    senal = verificar_setup_vwap(df)
                    if senal:
                        ejecutar_orden(senal, df)
                        print("⏸️ Pausa estratégica de 5 minutos activada...\n", flush=True)
                        time.sleep(300)
                    else:
                        print("-> Estructura rechazada. Manteniendo disciplina.\n", flush=True)
            
            time.sleep(15) 
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Excepción en el ciclo principal: {e}", flush=True)
            time.sleep(15)

if __name__ == "__main__":
    main()