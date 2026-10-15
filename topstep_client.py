import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import pandas as pd
from config import TOPSTEP_BASE_URL, TOPSTEP_USERNAME, TOPSTEP_API_KEY, ACCOUNT_FILTER, SUPPORTED_SYMBOLS

class TopstepClient:
    def __init__(self, base_url: str = TOPSTEP_BASE_URL, username: str = TOPSTEP_USERNAME, api_key: str = TOPSTEP_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.api_key = api_key
        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self.account_cache: Optional[Dict[str, Any]] = None
        self.contract_cache: Dict[str, Dict[str, Any]] = {}

    def get_headers(self) -> Dict[str, str]:
        token = self.ensure_authenticated()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def authenticate(self) -> str:
        url = f"{self.base_url}/api/Auth/loginKey"
        payload = {"userName": self.username, "apiKey": self.api_key}
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        
        if res.status_code != 200:
            raise RuntimeError(f"Fallo HTTP {res.status_code} al autenticar: {res.text}")
        
        data = res.json()
        if not data.get("success"):
            raise RuntimeError(f"Error autenticación TopstepX: {data.get('errorMessage', 'Sin mensaje de error')}")
        
        self.token = data.get("token")
        # Topstep tokens typically last multiple hours; refresh conservatively after 4 hours
        self.token_expiry = time.time() + (4 * 3600)
        return self.token

    def ensure_authenticated(self) -> str:
        if not self.token or time.time() >= self.token_expiry:
            return self.authenticate()
        return self.token

    def get_practice_account(self, filter_prefix: str = ACCOUNT_FILTER) -> Dict[str, Any]:
        """Obtiene la cuenta activa de práctica/simulación según el filtro seguro."""
        url = f"{self.base_url}/api/Account/search"
        res = requests.post(url, json={"onlyActiveAccounts": True}, headers=self.get_headers(), timeout=15)
        res.raise_for_status()
        data = res.json()
        
        accounts = data.get("accounts", [])
        if not accounts:
            raise RuntimeError("No se detectaron cuentas activas en TopstepX.")
        
        matched = None
        for acc in accounts:
            name_upper = acc.get("name", "").upper()
            if filter_prefix in name_upper or "PRACTICE" in name_upper:
                matched = acc
                break
        
        if not matched:
            # Fallback if no specific PRAC named account, but select the first active account if it permits trading
            matched = accounts[0]
            
        self.account_cache = matched
        return matched

    def resolve_contract(self, symbol_key: str) -> Dict[str, Any]:
        """Busca y resuelve el contrato activo para el símbolo dado (ES, NQ, YM, etc.)."""
        symbol_key = symbol_key.upper()
        if symbol_key in self.contract_cache:
            return self.contract_cache[symbol_key]
        
        spec = SUPPORTED_SYMBOLS.get(symbol_key, {"searchKey": symbol_key, "symbolPrefix": symbol_key})
        search_text = spec.get("searchKey", symbol_key)
        prefix = spec.get("symbolPrefix", "")
        
        url = f"{self.base_url}/api/Contract/search"
        payload = {"live": False, "searchText": search_text}
        res = requests.post(url, json=payload, headers=self.get_headers(), timeout=15)
        res.raise_for_status()
        data = res.json()
        
        contracts = data.get("contracts", [])
        if not contracts:
            raise RuntimeError(f"No se encontraron contratos para el símbolo: {symbol_key}")
        
        selected = None
        # Priority 1: Match activeContract and symbol prefix
        for c in contracts:
            sym_id = str(c.get("symbolId", ""))
            if prefix in sym_id and c.get("activeContract", False):
                selected = c
                break
        
        # Priority 2: Match activeContract and name/symbol
        if not selected:
            for c in contracts:
                if c.get("activeContract", False):
                    selected = c
                    break
        
        # Priority 3: First available contract
        if not selected:
            selected = contracts[0]
            
        self.contract_cache[symbol_key] = selected
        return selected

    def retrieve_bars(self, contract_id: str, unit: int = 2, unit_number: int = 5, limit: int = 288, days_back: int = 2) -> pd.DataFrame:
        """Descarga velas históricas (por defecto 5m) para el contrato especificado."""
        now_utc = datetime.now(timezone.utc)
        payload = {
            "contractId": contract_id,
            "live": False,
            "unit": unit, # 2 = minutos
            "unitNumber": unit_number, # 5 min
            "limit": limit,
            "includePartialBar": True,
            "startTime": (now_utc - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        
        url = f"{self.base_url}/api/History/retrieveBars"
        res = requests.post(url, json=payload, headers=self.get_headers(), timeout=20)
        res.raise_for_status()
        data = res.json()
        
        if data.get("success") and data.get("bars"):
            df = pd.DataFrame(data["bars"])
            df = df.rename(columns={
                't': 'Timestamp',
                'o': 'Open',
                'h': 'High',
                'l': 'Low',
                'c': 'Close',
                'v': 'Volume'
            })
            df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True)
            return df.sort_values(by='Timestamp').reset_index(drop=True)
        
        return pd.DataFrame()

    def place_order(self, account_id: int, contract_id: str, side: int, size: int = 1,
                    sl_ticks: Optional[int] = None, tp_ticks: Optional[int] = None,
                    custom_tag: Optional[str] = None) -> Dict[str, Any]:
        """Envía una orden a mercado con brackets de SL y TP (OCO)."""
        payload = {
            "accountId": int(account_id),
            "contractId": contract_id,
            "type": 2, # Market
            "side": int(side), # 0 = Buy/Long, 1 = Sell/Short
            "size": int(size)
        }
        
        if custom_tag:
            payload["customTag"] = custom_tag
            
        if sl_ticks is not None:
            payload["stopLossBracket"] = {"ticks": int(sl_ticks), "type": 4} # Type 4 = Stop
            
        if tp_ticks is not None:
            payload["takeProfitBracket"] = {"ticks": int(tp_ticks), "type": 1} # Type 1 = Limit
            
        url = f"{self.base_url}/api/Order/place"
        res = requests.post(url, json=payload, headers=self.get_headers(), timeout=15)
        res.raise_for_status()
        return res.json()

    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/Order/searchOpen"
        res = requests.post(url, json={"accountId": int(account_id)}, headers=self.get_headers(), timeout=15)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"):
                return data.get("orders", [])
        return []

    def cancel_order(self, account_id: int, order_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/Order/cancel"
        res = requests.post(url, json={"accountId": int(account_id), "orderId": int(order_id)}, headers=self.get_headers(), timeout=15)
        res.raise_for_status()
        return res.json()

    def close_contract_position(self, account_id: int, contract_id: str) -> Dict[str, Any]:
        """Cierre de emergencia / Flatten de la posición en el contrato."""
        url = f"{self.base_url}/api/Position/closeContract"
        res = requests.post(url, json={"accountId": int(account_id), "contractId": contract_id}, headers=self.get_headers(), timeout=15)
        res.raise_for_status()
        return res.json()

    def get_account_balance(self, account_id: Optional[int] = None) -> float:
        """Obtiene el balance real de la cuenta desde TopstepX."""
        url = f"{self.base_url}/api/Account/search"
        try:
            res = requests.post(url, json={"onlyActiveAccounts": True}, headers=self.get_headers(), timeout=15)
            if res.status_code == 200:
                data = res.json()
                accounts = data.get("accounts", [])
                for acc in accounts:
                    if account_id is not None and acc.get("id") == account_id:
                        return float(acc.get("balance", 0.0))
                    if account_id is None and ("PRAC" in acc.get("name", "").upper() or "PRACTICE" in acc.get("name", "").upper()):
                        return float(acc.get("balance", 0.0))
                if accounts:
                    return float(accounts[0].get("balance", 0.0))
        except Exception as e:
            print(f"Error fetching account balance: {e}")
        return 0.0

    def get_open_positions(self, account_id: int) -> List[Dict[str, Any]]:
        """Obtiene las posiciones abiertas activas desde TopstepX."""
        url = f"{self.base_url}/api/Position/searchOpen"
        try:
            res = requests.post(url, json={"accountId": int(account_id)}, headers=self.get_headers(), timeout=15)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    return data.get("positions", [])
        except Exception as e:
            print(f"Error searching open positions: {e}")
        return []

    def search_trades(self, account_id: int, days_back: int = 30) -> List[Dict[str, Any]]:
        """Obtiene todas las operaciones reales ejecutadas (fills) desde TopstepX."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)
        payload = {
            "accountId": int(account_id),
            "startTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTimestamp": end.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        url = f"{self.base_url}/api/Trade/search"
        try:
            res = requests.post(url, json=payload, headers=self.get_headers(), timeout=15)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    trades = data.get("trades", [])
                    trades.sort(key=lambda x: x.get("creationTimestamp", ""))
                    return trades
        except Exception as e:
            print(f"Error searching trades: {e}")
        return []
