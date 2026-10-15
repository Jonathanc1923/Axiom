import requests
import json
import sys

API_BASE_URL = "https://api.topstepx.com"
USERNAME = "jonathanencina2020@gmail.com"
API_KEY = "O/Jvyz5VggKPKRLF69YDGJQjXIcxsKuxZbJg9rgwek0="

print("🚀 Iniciando Test de Orden Rápida en Python...\n")

# ==========================================
# 1. AUTENTICACIÓN
# ==========================================
print("[1/4] Autenticando...")
res_auth = requests.post(
    f"{API_BASE_URL}/api/Auth/loginKey",
    json={"userName": USERNAME, "apiKey": API_KEY},
    headers={"Content-Type": "application/json"}
)

if not res_auth.ok or not res_auth.json().get("success"):
    print(f"❌ Error de Autenticación: {res_auth.text}")
    sys.exit()

token = res_auth.json()["token"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
print("✅ Autenticación exitosa.")

# ==========================================
# 2. BUSCAR CUENTA PRAC
# ==========================================
print("\n[2/4] Buscando cuenta de Práctica...")
res_acc = requests.post(f"{API_BASE_URL}/api/Account/search", json={"onlyActiveAccounts": True}, headers=headers)
cuentas = res_acc.json().get("accounts", [])

account_id = None
for c in cuentas:
    if "PRAC" in c["name"].upper() or "PRACTICE" in c["name"].upper():
        account_id = c["id"]
        print(f"✅ Cuenta conectada: {c['name']} (ID: {account_id})")
        break

if not account_id:
    print("❌ No se encontró tu cuenta de PRAC.")
    sys.exit()

# ==========================================
# 3. BUSCAR CONTRATO ES
# ==========================================
print("\n[3/4] Buscando contrato ES activo...")
res_cont = requests.post(f"{API_BASE_URL}/api/Contract/search", json={"live": False, "searchText": "ES"}, headers=headers)
contratos = res_cont.json().get("contracts", [])

contract_id = None
for c in contratos:
    if "F.US.EP" in c["symbolId"] and c["activeContract"]:
        contract_id = c["id"]
        print(f"✅ Contrato detectado: {c['name']} (ID: {contract_id})")
        break

if not contract_id:
    print("❌ No se encontró contrato ES.")
    sys.exit()

# ==========================================
# 4. DISPARAR ORDEN OCO (CON FIX DE SIGNOS)
# ==========================================
print("\n[4/4] Disparando orden a mercado (COMPRA | 1 Lote | SL -5 Ticks | TP 5 Ticks)...")
payload_orden = {
    "accountId": account_id,
    "contractId": contract_id,
    "type": 2, # 2 = Market
    "side": 0, # 0 = Buy (LONG)
    "size": 1,
    "stopLossBracket": {"ticks": -5, "type": 4}, # FIX: El SL para una compra debe ser negativo
    "takeProfitBracket": {"ticks": 5, "type": 1} # El TP para una compra es positivo
}

res_orden = requests.post(f"{API_BASE_URL}/api/Order/place", json=payload_orden, headers=headers)

print("\n================ RESPUESTA CRUDA DEL SERVIDOR ================")
try:
    respuesta_json = res_orden.json()
    print(json.dumps(respuesta_json, indent=4, ensure_ascii=False))
except Exception:
    print(res_orden.text)
print("==============================================================")