"""
Alias de compatibilidad ASGI para Render y otros servidores de producción.
Permite ejecutar tanto `uvicorn server:app` como `uvicorn app:app`.
"""
import os
import uvicorn
from app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
