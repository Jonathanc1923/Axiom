# Axiom Algorithmic — Institutional Quantitative Platform

Plataforma web institucional de trading algorítmico y ejecución cuantitativa para contratos de futuros del CME (E-mini S&P 500, Nasdaq-100, Dow Jones, Russell 2000, Petróleo, Oro, etc.) conectada directamente al gateway de **TopstepX**.

---

## 🏛️ Identidad y Arquitectura de la Firma

**Axiom Algorithmic (v2.0)** implementa un modelo cuantitativo institucional de reversión a la media de alta frecuencia basado exclusivamente en el **VWAP de Sesión (Volume-Weighted Average Price)** y bandas de desviación estándar (+1σ / -1σ) sincronizadas con el ciclo horario de los mercados de futuros CME (reinicio diario a las 18:00 ET / New York).

### Módulos Principales:
- **Estrategia VWAP Dynamic Reversion**: Disparo instantáneo a mercado al acercarse o tocar la línea de VWAP, con filtro estricto de contexto de la vela previa en 5m (separación mínima de 1 tick respecto al VWAP). No depende de la pendiente/tendencia.
- **Piramidación Protegida (Hasta 2 Operaciones por Símbolo)**: Permite abrir una 2ª posición si la 1ª ya aseguró su Stop Loss en Entrada + 5 ticks a favor.
- **Gestión Dinámica de Riesgo OCO**:
  - **Stop Loss Inicial**: Exactamente a **1/5 (20%) de la distancia entre el VWAP y la banda contraria**.
  - **Take Profit Inicial**: Al **50% de la distancia entre la entrada y la siguiente banda objetivo**.
- **Escalera de Trailing Stop y Sellado de Ganancias (Evaluación cada 2 Segundos)**:
  - **Hito 1 (Avance $\ge 25\%$ del TP inicial)**: Sella el SL en **Entrada + 5 ticks** a favor (riesgo cero absoluto y ganancia neta garantizada). Amplía el TP $+25\%$ adicional.
  - **Hitos Subsiguientes (Cada 25% de avance adicional: 50%, 75%, 100%...)**: El SL avanza $+25\%$ y el TP se amplía $+25\%$ continuamente.
  - **Modo Runner Trailing**: Persigue al precio a 15 ticks detrás del máximo alcanzado tras superar el 100%.
- **Gestión de Cartera y Metas de Sesión**:
  - **Canasta Global Flotante ($750 USD)**: Cierre inmediato de todas las posiciones abiertas en bloque al alcanzar +$750 USD flotantes.
  - **Meta Total de Sesión ($1,500 USD)**: Cierre total y congelamiento de operaciones por el resto del día al alcanzar +$1,500 USD en la sesión completa.
- **Dashboard Web Institucional**: Gráfico de velas de 5 minutos con **TradingView Lightweight Charts**, superposición de VWAP, bandas superior/inferior y marcadores de ejecución en tiempo real.
- **Terminal de Telemetría en Vivo (WebSockets)**: Transmite en vivo cada latido de 2 segundos a la consola web y persiste 24 horas continuas de eventos en disco (`logs_history_24h.jsonl`).
- **Caja Negra CSV**: Registro auditado y trazable de todas las órdenes e hitos en `registro_operaciones.csv` para auditoría y post-análisis cuantitativo.

---

## 📂 Estructura del Proyecto

```
a:\topstetp2\
├── app.py                  # Servidor de producción FastAPI con WebSockets y endpoints REST
├── engine.py               # Motor cuantitativo institucional (cálculo de VWAP, señales, ejecución OCO)
├── topstep_client.py       # Cliente para la API REST de TopstepX (Auth, contratos, velas, órdenes)
├── config.py               # Configuración centralizada y lectura de variables de entorno
├── requirements.txt        # Dependencias de producción para Python
├── Procfile                # Comando de arranque para Render Web Services
├── render.yaml             # Blueprint de despliegue automatizado en Render
├── .gitignore              # Protección estricta de credenciales, logs y caché
├── .env.example            # Plantilla de variables de entorno
├── static/
│   ├── index.html          # Panel de control institucional (SPA)
│   ├── css/
│   │   └── axiom.css       # Sistema de diseño oscuro institucional Axiom
│   ├── js/
│   │   └── axiom.js        # Lógica del cliente, gráficos TradingView y WebSockets
│   └── img/
│       ├── logo.svg        # Logotipo institucional vectorizado Axiom Algorithmic
│       └── favicon.svg     # Favicon institucional
├── prueba de envio de orden.py  # Script de diagnóstico de órdenes
└── vwap.py                 # Script clásico original
```

---

## 🚀 Despliegue en Render (Paso a Paso)

La aplicación está lista para producción en **Render** como un **Web Service**.

### Paso 1: Subir el proyecto a GitHub / GitLab
1. Inicializa tu repositorio Git si aún no lo has hecho:
   ```bash
   git init
   git add .
   git commit -m "Axiom Algorithmic - Institutional Web Platform"
   ```
2. Crea un repositorio privado en GitHub o GitLab y sube el código:
   ```bash
   git branch -M main
   git remote add origin https://github.com/tu-usuario/axiom-algorithmic.git
   git push -u origin main
   ```

### Paso 2: Crear el Web Service en Render
1. Inicia sesión en [render.com](https://render.com).
2. Haz clic en **New +** y selecciona **Web Service**.
3. Conecta tu repositorio de GitHub recién creado.
4. Completa la configuración básica:
   - **Name**: `axiom-algorithmic`
   - **Environment**: `Python 3`
   - **Region**: Selecciona la más cercana (ej. `Oregon, USA` o `Frankfurt`).
   - **Branch**: `main`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`

### Paso 3: Configurar las Variables de Entorno en Render
En la sección **Environment** del panel de control de tu servicio en Render, agrega las siguientes variables seguras:

| Clave | Valor Recomendado | Descripción |
|---|---|---|
| `TOPSTEP_USERNAME` | `tu-email@correo.com` | Tu correo o usuario de TopstepX |
| `TOPSTEP_API_KEY` | `tu_api_key_secreta` | Tu API Key generada en TopstepX |
| `TOPSTEP_BASE_URL` | `https://api.topstepx.com` | URL base de la API REST de TopstepX |
| `ACCOUNT_FILTER` | `PRAC` | Filtro para usar solo tu cuenta de práctica |
| `DEFAULT_SYMBOL` | `ES` | Símbolo inicial activo (ES, NQ, YM, etc.) |
| `AUTO_START_ENGINE` | `true` | Inicia automáticamente el bot al encender el servidor |

5. Haz clic en **Create Web Service**. Render instalará las dependencias y lanzará la aplicación en tu URL pública protegida con SSL (`https://axiom-algorithmic.onrender.com`).

---

## 💻 Ejecución Local en Tu Computadora

Si deseas ejecutar la plataforma en local antes de desplegarla:

1. Instala las dependencias (si hiciera falta):
   ```bash
   pip install -r requirements.txt
   ```

2. Ejecuta el servidor:
   ```bash
   uvicorn app:app --host 127.0.0.1 --port 8000 --reload
   ```

3. Abre tu navegador en:
   ```
   http://127.0.0.1:8000
   ```

---

## 📊 Especificaciones de Símbolos CME Soportados

El motor incluye calibración automática de tamaño de tick y márgenes de riesgo para:
- **ES** (E-mini S&P 500)
- **NQ** (E-mini Nasdaq-100)
- **YM** (E-mini Dow Jones)
- **RTY** (E-mini Russell 2000)
- **MES** (Micro E-mini S&P 500)
- **MNQ** (Micro E-mini Nasdaq-100)
- **CL** (Crude Oil)
- **GC** (Gold)

Cualquiera de estos instrumentos puede seleccionarse en vivo desde las pestañas del gráfico para redirigir la telemetría y la ejecución autónoma.
