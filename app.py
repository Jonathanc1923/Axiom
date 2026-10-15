import os
import sys
import io
import json
import asyncio

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

from pathlib import Path
from typing import Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import (
    PORT, TOPSTEP_USERNAME, TOPSTEP_API_KEY, TOPSTEP_BASE_URL,
    DEFAULT_SYMBOL, AUTO_START_ENGINE, CSV_PATH, SUPPORTED_SYMBOLS
)
from topstep_client import TopstepClient
from engine import AxiomEngine

app = FastAPI(
    title="Axiom Algorithmic - Institutional Trading Engine",
    description="Mission-critical algorithmic trading execution framework and live telemetry dashboard.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directories
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "css").mkdir(exist_ok=True)
(STATIC_DIR / "js").mkdir(exist_ok=True)
(STATIC_DIR / "img").mkdir(exist_ok=True)

# Instantiate core components
client = TopstepClient(
    base_url=TOPSTEP_BASE_URL,
    username=TOPSTEP_USERNAME,
    api_key=TOPSTEP_API_KEY
)
engine = AxiomEngine(client=client, default_symbol=DEFAULT_SYMBOL)

# Connected WebSocket clients
connected_sockets: List[WebSocket] = []
main_loop = None

# Bridge engine logs to async WebSocket broadcast
def on_engine_log(log_entry: Dict[str, Any]):
    global main_loop
    if main_loop and connected_sockets:
        asyncio.run_coroutine_threadsafe(broadcast_message({"type": "log", "data": log_entry}), main_loop)

engine.add_log_listener(on_engine_log)

async def broadcast_message(message: Dict[str, Any]):
    dead_sockets = []
    text = json.dumps(message)
    for ws in connected_sockets:
        try:
            await ws.send_text(text)
        except Exception:
            dead_sockets.append(ws)
    for ws in dead_sockets:
        if ws in connected_sockets:
            connected_sockets.remove(ws)

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()
    print("=" * 60)
    print("🚀 AXIOM ALGORITHMIC - INSTITUTIONAL TRADING PLATFORM")
    print(f"📡 TopstepX Target: {TOPSTEP_BASE_URL}")
    print(f"📊 Símbolo Activo: {DEFAULT_SYMBOL}")
    print(f"🌐 Servidor escuchando en puerto: {PORT}")
    print("=" * 60)
    
    try:
        engine.initialize()
    except Exception as e:
        print(f"⚠️ Alerta durante inicialización inicial: {e}")
        
    if AUTO_START_ENGINE:
        print("⚡ AUTO_START_ENGINE=True: Lanzando motor algorítmico automáticamente...")
        engine.start()

@app.on_event("shutdown")
def shutdown_event():
    print("🛑 Deteniendo Axiom Engine en shutdown del servidor...")
    engine.stop()

# ==========================================
# REST API ENDPOINTS
# ==========================================

class SymbolRequest(BaseModel):
    symbol: str

@app.get("/api/status")
def get_system_status():
    status = engine.get_status()
    # Check open orders from gateway
    open_orders = []
    if engine.account_id:
        try:
            open_orders = client.search_open_orders(engine.account_id)
        except Exception:
            pass
    status["open_orders"] = open_orders
    return status

@app.post("/api/engine/start")
def start_engine():
    if engine.running:
        return {"success": True, "message": "El motor ya está activo."}
    engine.start()
    return {"success": True, "message": "Motor algorítmico iniciado exitosamente."}

@app.post("/api/engine/stop")
def stop_engine():
    if not engine.running:
        return {"success": True, "message": "El motor ya está detenido."}
    engine.stop()
    return {"success": True, "message": "Motor algorítmico detenido."}

@app.post("/api/engine/symbol")
def change_symbol(req: SymbolRequest):
    sym = req.symbol.upper()
    if sym not in SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"Símbolo no soportado: {sym}")
    try:
        engine.switch_symbol(sym)
        return {"success": True, "symbol": sym, "message": f"Cambiado a {sym}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/engine/flatten")
def emergency_flatten():
    res = engine.emergency_flatten()
    return res

@app.get("/api/chart/data")
def get_chart_data():
    return engine.get_chart_data()

@app.get("/api/operations")
def get_operations():
    history = engine.load_operations_history()
    trades_topstep = []
    if engine.account_id:
        try:
            trades_topstep = client.search_trades(engine.account_id, minutes_back=1440)
        except Exception:
            pass
    return {
        "csv_records": history,
        "topstep_trades": trades_topstep,
        "stats": engine.stats
    }

@app.get("/api/operations/download")
def download_csv():
    if os.path.isfile(CSV_PATH):
        return FileResponse(CSV_PATH, filename="registro_operaciones.csv", media_type="text/csv")
    raise HTTPException(status_code=404, detail="El archivo de registro aún no ha sido generado.")

@app.get("/api/logs")
def get_logs(limit: int = Query(default=100, le=500)):
    with engine.lock:
        return engine.log_history[-limit:]

@app.get("/api/symbols")
def get_symbols():
    return SUPPORTED_SYMBOLS

# ==========================================
# WEBSOCKET REAL-TIME TELEMETRY
# ==========================================

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_sockets.append(websocket)
    
    # Send initial welcome payload
    try:
        await websocket.send_text(json.dumps({
            "type": "welcome",
            "status": engine.get_status(),
            "chart": engine.get_chart_data()
        }))
    except Exception:
        pass
        
    try:
        while True:
            # Send periodic telemetry heartbeat every 3 seconds
            await asyncio.sleep(3)
            with engine.lock:
                telemetry_data = engine.latest_telemetry
                stats_data = engine.stats
                
            await websocket.send_text(json.dumps({
                "type": "telemetry",
                "telemetry": telemetry_data,
                "stats": stats_data,
                "running": engine.running
            }))
    except WebSocketDisconnect:
        if websocket in connected_sockets:
            connected_sockets.remove(websocket)
    except Exception:
        if websocket in connected_sockets:
            connected_sockets.remove(websocket)

# Mount static assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Axiom Algorithmic - Cargando interfaz...</h1>")
