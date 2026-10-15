# Axiom Algorithmic — Institutional Quantitative Platform

Plataforma web institucional de trading algorítmico y ejecución cuantitativa para contratos de futuros del CME (E-mini S&P 500, Nasdaq-100, Dow Jones, Russell 2000, Petróleo, Oro, etc.) conectada directamente al gateway de **TopstepX**.

---

## 🏛️ Identidad y Arquitectura de la Firma

**Axiom Algorithmic** implementa un modelo de reversión a la media institucional basado en el **VWAP de Sesión (Volume-Weighted Average Price)** y bandas de desviación estándar (+1σ / -1σ) sincronizadas con el ciclo horario de los mercados de futuros (apertura y reinicio a las 18:00 ET / New York).

### Módulos Principales:
- **Dashboard Web Institucional**: Gráfico de velas interactivas de 5 minutos con la librería de grado bancario **TradingView Lightweight Charts**, superposición de VWAP, bandas superior/inferior y marcadores de órdenes ejecutadas en tiempo real.
- **Motor Autónomo Multiactivo**: Ejecuta la estrategia en segundo plano de manera automática al arrancar (`AUTO_START_ENGINE=true`), y permite cambiar o monitorear dinámicamente símbolos como `ES`, `NQ`, `YM`, `RTY`, `MES`, `MNQ`, `CL` y `GC`.
- **Terminal de Telemetría en Vivo (WebSockets)**: Transmite en vivo a la consola web cada pulso de mercado, evaluaciones de velas cerradas, rechazos en VWAP y respuestas del servidor con la misma fidelidad que en consola.
- **Gestión Dinámica de Riesgo OCO**: Cálculo dinámico de Stop Loss (fuera del VWAP por margen de protección) y Take Profit (75% del recorrido hacia la banda), respetando la convención de signos exigida por TopstepX / ProjectX.
- **Filtro de Seguridad PRACTICE**: Conexión protegida exclusivamente hacia cuentas de simulación/práctica (`PRAC`), evitando ejecuciones accidentales en cuentas reales.
- **Kill Switch de Emergencia**: Botón de *Flatten* para liquidar y cerrar inmediatamente cualquier posición abierta.
- **Caja Negra CSV**: Registro auditado en `registro_operaciones.csv` con exportación directa desde el navegador.

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
