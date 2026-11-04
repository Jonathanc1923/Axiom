/**
 * AXIOM ALGORITHMIC - INSTITUTIONAL MULTI-SYMBOL CLIENT ENGINE
 * Handles TradingView Lightweight Charts, Multi-Symbol Fleet, WebSockets & Terminal Filtering
 */

document.addEventListener('DOMContentLoaded', () => {
    // State
    let chart = null;
    let candleSeries = null;
    let vwapSeries = null;
    let upperBandSeries = null;
    let lowerBandSeries = null;
    
    let activeSymbol = 'ES';
    let isRunning = false;
    let ws = null;
    let autoScrollTerminal = true;
    let selectedTerminalSymbol = 'ALL'; // Filter for dedicated symbol terminal
    let symbolStates = {}; // Cache of all 12 CME symbols
    let rawOperations = []; // Cache of all operations
    
    // UI Elements
    const statusPill = document.getElementById('engine-status-pill');
    const statusText = document.getElementById('engine-status-text');
    const btnStart = document.getElementById('btn-start-engine');
    const btnStop = document.getElementById('btn-stop-engine');
    const btnFlatten = document.getElementById('btn-flatten');
    const btnEmergencyTop = document.getElementById('btn-emergency-top');
    const chartSymbolTabs = document.getElementById('chart-symbol-tabs');
    const btnToggleActiveTrading = document.getElementById('btn-toggle-active-trading');
    const fleetStrip = document.getElementById('fleet-strip');
    
    // Telemetry HUD Elements
    const hudSymbol = document.getElementById('hud-symbol');
    const hudPrice = document.getElementById('hud-price');
    const hudVwap = document.getElementById('hud-vwap');
    const hudUpper = document.getElementById('hud-upper');
    const hudLower = document.getElementById('hud-lower');
    const hudSpread = document.getElementById('hud-spread');
    const radarSymbolBadge = document.getElementById('radar-symbol-badge');
    const specStatus = document.getElementById('spec-status');
    const btnSpecToggle = document.getElementById('btn-spec-toggle');
    
    // Metrics Ribbon (Real Topstep Data)
    const metricPnl = document.getElementById('metric-pnl');
    const metricBalance = document.getElementById('metric-balance');
    const metricWinrate = document.getElementById('metric-winrate');
    const metricTrades = document.getElementById('metric-trades');
    const metricProfitFactor = document.getElementById('metric-profit-factor');
    const metricSyncStatus = document.getElementById('metric-sync-status');
    const metricSyncTime = document.getElementById('metric-sync-time');
    const btnSyncTopstep = document.getElementById('btn-sync-topstep');
    const metricCooldown = document.getElementById('metric-cooldown');
    const metricPosition = document.getElementById('metric-position');
    
    // Clocks
    const clockNy = document.getElementById('clock-ny');
    const clockUtc = document.getElementById('clock-utc');
    const marketStatusPill = document.getElementById('market-status-pill');
    
    // Bottom Deck Tabs, Terminal & Breakdown
    const deckTabs = document.querySelectorAll('.deck-tab');
    const tabPanes = document.querySelectorAll('.tab-pane');
    const terminalLogs = document.getElementById('terminal-logs');
    const terminalSymBar = document.getElementById('terminal-sym-bar');
    const btnClearLogs = document.getElementById('btn-clear-logs');
    const chkAutoScroll = document.getElementById('chk-autoscroll');
    const operationsTableBody = document.getElementById('operations-table-body');
    const selectOpFilter = document.getElementById('select-op-filter');
    const fleetStatsSummary = document.getElementById('fleet-stats-summary');
    const btnDownloadCsv = document.getElementById('btn-download-csv');
    const matrixTableBody = document.getElementById('matrix-table-body');

    // Ranking por Símbolo UI Elements (Nueva Ley)
    let latestRankingData = null;
    const rankTotalNet = document.getElementById('rank-total-net');
    const rankTotalGrossSub = document.getElementById('rank-total-gross-sub');
    const rankTotalWinrate = document.getElementById('rank-total-winrate');
    const rankTotalTradesSub = document.getElementById('rank-total-trades-sub');
    const rankTotalPf = document.getElementById('rank-total-pf');
    const rankLeaderSymbol = document.getElementById('rank-leader-symbol');
    const rankLeaderPnl = document.getElementById('rank-leader-pnl');
    const rankingTableBody = document.getElementById('ranking-table-body');
    const rankingTableFoot = document.getElementById('ranking-table-foot');
    const selectRankingDetailFilter = document.getElementById('select-ranking-detail-filter');
    const rankingOperationsDetailBody = document.getElementById('ranking-operations-detail-body');

    // ==========================================
    // 1. INITIALIZE TRADINGVIEW LIGHTWEIGHT CHART
    // ==========================================
    function initChart() {
        const chartContainer = document.getElementById('tradingview-chart');
        if (!chartContainer) return;
        chartContainer.innerHTML = '';

        chart = LightweightCharts.createChart(chartContainer, {
            layout: {
                background: { color: '#060911' },
                textColor: '#94A3B8',
                fontSize: 11,
                fontFamily: "'JetBrains Mono', 'Inter', monospace",
            },
            grid: {
                vertLines: { color: 'rgba(25, 37, 59, 0.45)' },
                horzLines: { color: 'rgba(25, 37, 59, 0.45)' },
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: {
                    color: '#00F2FE',
                    width: 1,
                    style: LightweightCharts.LineStyle.Dotted,
                    labelBackgroundColor: '#0A101C',
                },
                horzLine: {
                    color: '#00F2FE',
                    width: 1,
                    style: LightweightCharts.LineStyle.Dotted,
                    labelBackgroundColor: '#0A101C',
                },
            },
            timeScale: {
                timeVisible: true,
                secondsVisible: false,
                borderColor: '#19253B',
            },
            rightPriceScale: {
                borderColor: '#19253B',
                autoScale: true,
            },
        });

        // 30m Candlestick series
        candleSeries = chart.addCandlestickSeries({
            upColor: '#10B981',
            downColor: '#EF4444',
            borderUpColor: '#10B981',
            borderDownColor: '#EF4444',
            wickUpColor: '#10B981',
            wickDownColor: '#EF4444',
        });

        // VWAP Line (Cyan)
        vwapSeries = chart.addLineSeries({
            color: '#38BDF8',
            lineWidth: 2,
            title: 'VWAP',
            priceLineVisible: false,
        });

        // Upper Band +1 STD (Coral/Red)
        upperBandSeries = chart.addLineSeries({
            color: '#F87171',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            title: '+1 STD',
            priceLineVisible: false,
        });

        // Lower Band -1 STD (Emerald/Green)
        lowerBandSeries = chart.addLineSeries({
            color: '#34D399',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            title: '-1 STD',
            priceLineVisible: false,
        });

        window.addEventListener('resize', () => {
            if (chart && chartContainer) {
                chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
            }
        });
    }

    // ==========================================
    // 2. FETCH & RENDER CHART DATA FOR SYMBOL
    // ==========================================
    async function fetchChartData(symbolKey = activeSymbol) {
        try {
            const res = await fetch(`/api/chart/data?symbol=${symbolKey}`);
            if (!res.ok) return;
            const data = await res.json();
            
            if (data.candles && data.candles.length > 0) {
                candleSeries.setData(data.candles);
                if (data.vwap) vwapSeries.setData(data.vwap);
                if (data.upper_band) upperBandSeries.setData(data.upper_band);
                if (data.lower_band) lowerBandSeries.setData(data.lower_band);
                
                chart.timeScale().fitContent();
            } else {
                candleSeries.setData([]);
                vwapSeries.setData([]);
                upperBandSeries.setData([]);
                lowerBandSeries.setData([]);
            }
            
            // Update active symbol button states
            updateActiveSymbolControls(data.symbol, data.enabled);
            loadChartMarkers(data.symbol);
        } catch (e) {
            console.error('Error fetching chart data:', e);
        }
    }

    function updateActiveSymbolControls(sym, isEnabled) {
        if (hudSymbol) hudSymbol.innerText = sym;
        if (radarSymbolBadge) radarSymbolBadge.innerText = sym;
        const specSym = document.getElementById('spec-symbol');
        if (specSym) specSym.innerText = sym;
        
        if (btnToggleActiveTrading) {
            btnToggleActiveTrading.className = `fleet-toggle-btn ${isEnabled ? 'active' : 'inactive'}`;
            btnToggleActiveTrading.innerText = `${sym}: ${isEnabled ? 'ACTIVO (Pausar)' : 'PAUSADO (Activar)'}`;
            btnToggleActiveTrading.style.color = isEnabled ? '#10B981' : '#EF4444';
            btnToggleActiveTrading.style.borderColor = isEnabled ? 'rgba(16, 185, 129, 0.4)' : 'rgba(239, 68, 68, 0.4)';
        }
        if (specStatus) {
            specStatus.innerText = isEnabled ? 'ACTIVO (Operando)' : 'PAUSADO (Solo Monitoreo)';
            specStatus.style.color = isEnabled ? '#10B981' : '#EF4444';
        }
        if (btnSpecToggle) {
            btnSpecToggle.className = `fleet-toggle-btn ${isEnabled ? 'active' : 'inactive'}`;
            btnSpecToggle.innerText = isEnabled ? 'PAUSAR' : 'ACTIVAR';
            btnSpecToggle.style.color = isEnabled ? '#EF4444' : '#10B981';
            btnSpecToggle.style.borderColor = isEnabled ? 'rgba(239, 68, 68, 0.4)' : 'rgba(16, 185, 129, 0.4)';
        }
    }

    function updateSymbolTabsStatus(matrix) {
        if (!matrix) return;
        document.querySelectorAll('.symbol-tab').forEach(tab => {
            const sym = tab.getAttribute('data-symbol');
            const item = matrix[sym];
            const isEnabled = item ? item.enabled : true;
            
            tab.classList.toggle('tab-enabled', isEnabled);
            tab.classList.toggle('tab-disabled', !isEnabled);
            
            let dot = tab.querySelector('.tab-dot');
            if (!dot) {
                dot = document.createElement('span');
                dot.className = 'tab-dot';
                tab.insertBefore(dot, tab.firstChild);
            }
            tab.title = `${sym}: ${isEnabled ? 'OPERANDO (Click para ver gráfico, doble click para pausar)' : 'PAUSADO (Click para ver gráfico, doble click para activar)'}`;
        });
    }

    async function loadChartMarkers(forSymbol = activeSymbol) {
        try {
            const res = await fetch('/api/operations');
            if (!res.ok) return;
            const data = await res.json();
            const markers = [];
            
            (data.csv_records || []).forEach(record => {
                const recSym = record.Simbolo || 'ES';
                if (recSym === forSymbol && record.Estado && record.Estado.includes('EJECUTADA')) {
                    const isLong = (record.Direccion || '').toUpperCase() === 'LONG';
                    markers.push({
                        time: candleSeries.data().length > 0 ? candleSeries.data()[candleSeries.data().length - 1].time : undefined,
                        position: isLong ? 'belowBar' : 'aboveBar',
                        color: isLong ? '#10B981' : '#EF4444',
                        shape: isLong ? 'arrowUp' : 'arrowDown',
                        text: `${record.Direccion} @ ${record.Precio_Mercado}`,
                    });
                }
            });
            if (markers.length > 0 && markers[0].time) {
                candleSeries.setMarkers(markers.slice(-15));
            }
        } catch (e) {
            console.error('Error loading markers:', e);
        }
    }

    // ==========================================
    // 3. CLOCKS & MARKET SESSION
    // ==========================================
    function updateClocks() {
        const now = new Date();
        const utcStr = now.toISOString().slice(11, 19) + ' UTC';
        if (clockUtc) clockUtc.innerText = utcStr;
        
        const nyFormatter = new Intl.DateTimeFormat('en-US', {
            timeZone: 'America/New_York',
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
        const nyStr = nyFormatter.format(now) + ' ET';
        if (clockNy) clockNy.innerText = nyStr;
        
        const nyHour = parseInt(nyFormatter.format(now).split(':')[0], 10);
        const day = now.getUTCDay();
        let isSessionOpen = true;
        if (day === 6) isSessionOpen = false;
        if (day === 0 && nyHour < 18) isSessionOpen = false;
        if (nyHour === 17) isSessionOpen = false;
        
        if (marketStatusPill) {
            if (isSessionOpen) {
                marketStatusPill.className = 'status-badge running';
                marketStatusPill.innerHTML = '<span class="pulse-dot"></span> CME LIVE';
            } else {
                marketStatusPill.className = 'status-badge stopped';
                marketStatusPill.innerHTML = '<span class="pulse-dot"></span> CME BREAK';
            }
        }
    }
    setInterval(updateClocks, 1000);
    updateClocks();

    // ==========================================
    // 4. WEBSOCKET REAL-TIME CONNECTION
    // ==========================================
    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/live`;
        
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            appendTerminalLine({
                timestamp: new Date().toLocaleTimeString(),
                category: 'SYSTEM',
                symbol: 'ALL',
                message: '✅ Conexión WebSocket multi-símbolo establecida con Axiom Platform.'
            });
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'welcome') {
                    updateStatusUI(msg.status);
                    if (msg.chart && msg.chart.candles) {
                        candleSeries.setData(msg.chart.candles);
                        if (msg.chart.vwap) vwapSeries.setData(msg.chart.vwap);
                        if (msg.chart.upper_band) upperBandSeries.setData(msg.chart.upper_band);
                        if (msg.chart.lower_band) lowerBandSeries.setData(msg.chart.lower_band);
                        chart.timeScale().fitContent();
                    }
                } else if (msg.type === 'telemetry') {
                    if (msg.telemetry) updateTelemetryUI(msg.telemetry);
                    if (msg.stats) updateMetricsUI(msg.stats, msg.running);
                    if (msg.symbols_matrix) {
                        renderFleetStrip(msg.symbols_matrix);
                        updateMatrixUI(msg.symbols_matrix);
                        updateSymbolTabsStatus(msg.symbols_matrix);
                        const currentItem = msg.symbols_matrix[activeSymbol];
                        if (currentItem) {
                            if (specContract) specContract.innerText = currentItem.contract || 'Detectando...';
                            updateActiveSymbolControls(activeSymbol, currentItem.enabled);
                        }
                    }
                    if (msg.ranking) {
                        renderRankingSection(msg.ranking);
                    }
                } else if (msg.type === 'log') {
                    appendTerminalLine(msg.data);
                    if (msg.data.category === 'ORDER' || msg.data.category === 'SYSTEM') {
                        loadOperations();
                        loadRankingData();
                    }
                }
            } catch (err) {
                console.error('Error handling WebSocket message:', err);
            }
        };

        ws.onclose = () => {
            appendTerminalLine({
                timestamp: new Date().toLocaleTimeString(),
                category: 'ERROR',
                symbol: 'ALL',
                message: '⚠️ Desconexión WebSocket. Reintentando en 3 segundos...'
            });
            setTimeout(connectWebSocket, 3000);
        };
    }

    // ==========================================
    // 5. MULTI-SYMBOL FLEET STRIP & MATRIX RENDERER
    // ==========================================
    function renderFleetStrip(matrix) {
        if (!fleetStrip || !matrix) return;
        symbolStates = matrix;
        
        const existingCards = fleetStrip.querySelectorAll('.fleet-card');
        const symbolKeys = Object.keys(matrix);
        
        if (existingCards.length !== symbolKeys.length) {
            // Inicializar tarjetas en el DOM
            fleetStrip.innerHTML = '';
            symbolKeys.forEach(sym => {
                const item = matrix[sym];
                const isSelected = (sym === activeSymbol);
                const isEnabled = item.enabled;
                const spread = item.spread_vwap || 0;
                const spreadClass = spread >= 0 ? 'bull' : 'bear';
                
                const card = document.createElement('div');
                card.className = `fleet-card ${isSelected ? 'active-view' : ''}`;
                card.setAttribute('data-symbol', sym);
                
                card.innerHTML = `
                    <div class="fleet-card-header">
                        <span class="fleet-symbol">${sym}</span>
                        <span class="badge-status-chip ${isEnabled ? 'active' : 'inactive'}">${isEnabled ? 'ACTIVO' : 'INACTIVO'}</span>
                    </div>
                    <div class="fleet-price">${item.price > 0 ? item.price.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</div>
                    <div class="fleet-sub fleet-vwap">VWAP: ${item.vwap > 0 ? item.vwap.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</div>
                    <div class="fleet-sub fleet-spread ${spreadClass}">Δ: ${spread >= 0 ? '+' : ''}${spread.toFixed(2)} pts</div>
                    <button class="fleet-toggle-btn ${isEnabled ? 'active' : 'inactive'} btn-toggle-card" data-symbol="${sym}">
                        ${isEnabled ? 'OPERANDO (ON)' : 'ACTIVAR (OFF)'}
                    </button>
                `;
                
                // Clicking the card switches chart view
                card.addEventListener('click', (e) => {
                    if (e.target.classList.contains('btn-toggle-card')) return;
                    switchSymbol(sym);
                });
                
                const toggleBtn = card.querySelector('.btn-toggle-card');
                if (toggleBtn) {
                    toggleBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        toggleSymbol(sym);
                    });
                }
                
                fleetStrip.appendChild(card);
            });
        } else {
            // Actualización suave in-place sin parpadeos ni interrupción de clics
            existingCards.forEach(card => {
                const sym = card.getAttribute('data-symbol');
                const item = matrix[sym];
                if (!item) return;
                
                card.classList.toggle('active-view', sym === activeSymbol);
                
                const chip = card.querySelector('.badge-status-chip');
                if (chip) {
                    chip.className = `badge-status-chip ${item.enabled ? 'active' : 'inactive'}`;
                    chip.innerText = item.enabled ? 'ACTIVO' : 'INACTIVO';
                }
                
                const priceEl = card.querySelector('.fleet-price');
                if (priceEl && item.price > 0) {
                    priceEl.innerText = item.price.toLocaleString('en-US', { minimumFractionDigits: 2 });
                }
                
                const vwapEl = card.querySelector('.fleet-vwap');
                if (vwapEl && item.vwap > 0) {
                    vwapEl.innerText = `VWAP: ${item.vwap.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
                }
                
                const spreadEl = card.querySelector('.fleet-spread');
                if (spreadEl) {
                    const spread = item.spread_vwap || 0;
                    spreadEl.className = `fleet-sub fleet-spread ${spread >= 0 ? 'bull' : 'bear'}`;
                    spreadEl.innerText = `Δ: ${spread >= 0 ? '+' : ''}${spread.toFixed(2)} pts`;
                }
                
                const btn = card.querySelector('.btn-toggle-card');
                if (btn) {
                    btn.className = `fleet-toggle-btn ${item.enabled ? 'active' : 'inactive'} btn-toggle-card`;
                    btn.innerText = item.enabled ? 'OPERANDO (ON)' : 'ACTIVAR (OFF)';
                }
            });
        }
    }

    function updateMatrixUI(matrix) {
        if (!matrixTableBody || !matrix) return;
        matrixTableBody.innerHTML = '';
        
        Object.keys(matrix).forEach(sym => {
            const item = matrix[sym];
            const isEnabled = item.enabled;
            const spread = item.spread_vwap || 0;
            const spreadClass = spread >= 0 ? 'bull' : 'bear';
            const tr = document.createElement('tr');
            
            tr.innerHTML = `
                <td><span class="badge-sym">${sym}</span></td>
                <td>${item.contract || item.name || 'N/A'}</td>
                <td>
                    <button class="fleet-toggle-btn ${isEnabled ? 'active' : 'inactive'} btn-matrix-toggle" data-symbol="${sym}">
                        ${isEnabled ? '● ACTIVO' : '○ INACTIVO'}
                    </button>
                </td>
                <td style="font-weight:700;">${item.price > 0 ? item.price.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</td>
                <td style="color:var(--vwap-color);">${item.vwap > 0 ? item.vwap.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</td>
                <td style="color:var(--upper-band-color);">${item.upper > 0 ? item.upper.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</td>
                <td style="color:var(--lower-band-color);">${item.lower > 0 ? item.lower.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</td>
                <td class="${spreadClass}">${spread >= 0 ? '+' : ''}${spread.toFixed(2)}</td>
                <td>${item.cooldown > 0 ? `${item.cooldown}s` : 'LISTO'}</td>
                <td><button class="btn-small btn-switch-matrix" data-symbol="${sym}">Ver Gráfico</button></td>
            `;
            matrixTableBody.appendChild(tr);
        });

        document.querySelectorAll('.btn-switch-matrix').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const s = e.target.getAttribute('data-symbol');
                switchSymbol(s);
            });
        });

        document.querySelectorAll('.btn-matrix-toggle').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const s = e.target.getAttribute('data-symbol');
                toggleSymbol(s);
            });
        });
    }

    // ==========================================
    // 6. UI UPDATERS
    // ==========================================
    function updateStatusUI(status) {
        if (!status) return;
        isRunning = status.running;
        activeSymbol = status.active_symbol;
        
        if (statusPill && statusText) {
            if (isRunning) {
                statusPill.className = 'status-badge running';
                statusText.innerText = 'MOTOR EN EJECUCIÓN';
            } else {
                statusPill.className = 'status-badge stopped';
                statusText.innerText = 'MOTOR DETENIDO';
            }
        }
        
        // Highlight active symbol tab in chart header
        const symbolTabs = document.querySelectorAll('.symbol-tab');
        symbolTabs.forEach(tab => {
            tab.classList.toggle('active', tab.getAttribute('data-symbol') === activeSymbol);
        });
        
        const specSym = document.getElementById('spec-symbol');
        const specContract = document.getElementById('spec-contract');
        const specAccount = document.getElementById('spec-account');
        if (specSym) specSym.innerText = activeSymbol;
        if (specAccount) specAccount.innerText = `${status.account_name} (${status.account_id})`;
        
        if (status.symbols_matrix) {
            renderFleetStrip(status.symbols_matrix);
            updateMatrixUI(status.symbols_matrix);
            updateSymbolTabsStatus(status.symbols_matrix);
            const currentItem = status.symbols_matrix[activeSymbol];
            if (currentItem) {
                if (specContract) specContract.innerText = currentItem.contract || 'Detectando...';
                updateActiveSymbolControls(activeSymbol, currentItem.enabled);
            }
        }
        
        if (status.telemetry) updateTelemetryUI(status.telemetry);
        if (status.stats) updateMetricsUI(status.stats, isRunning);
    }

    function updateTelemetryUI(telemetry) {
        if (!telemetry || !telemetry.close) return;
        
        if (hudPrice) hudPrice.innerText = telemetry.close.toLocaleString('en-US', { minimumFractionDigits: 2 });
        if (hudVwap) hudVwap.innerText = telemetry.vwap.toLocaleString('en-US', { minimumFractionDigits: 2 });
        if (hudUpper) hudUpper.innerText = telemetry.upper_band.toLocaleString('en-US', { minimumFractionDigits: 2 });
        if (hudLower) hudLower.innerText = telemetry.lower_band.toLocaleString('en-US', { minimumFractionDigits: 2 });
        
        const spread = telemetry.distance_vwap;
        if (hudSpread) {
            const spreadFormatted = (spread >= 0 ? '+' : '') + spread.toFixed(2) + ' pts';
            hudSpread.innerText = spreadFormatted;
            hudSpread.className = spread >= 0 ? 'metric-value bull' : 'metric-value bear';
        }
        
        const cdEl = document.getElementById('metric-cooldown');
        if (cdEl) {
            const cd = telemetry.cooldown_remaining || 0;
            if (cd > 0) {
                cdEl.innerText = `${cd}s (PAUSA)`;
                cdEl.className = 'metric-value bear';
            } else {
                cdEl.innerText = '0s (LISTO)';
                cdEl.className = 'metric-value bull';
            }
        }
        
        const checkTrend = document.getElementById('check-trend');
        const checkTouch = document.getElementById('check-touch');
        if (checkTrend) {
            if (Math.abs(spread) < 6.0) {
                checkTrend.innerText = 'PROXIMIDAD ALTA';
                checkTrend.className = 'check-indicator ready';
            } else {
                checkTrend.innerText = 'EN RANGO';
                checkTrend.className = 'check-indicator pending';
            }
        }
        if (checkTouch) {
            checkTouch.innerText = (telemetry.high >= telemetry.vwap && telemetry.low <= telemetry.vwap) ? 'TEST VWAP' : 'EN CANAL';
            checkTouch.className = (telemetry.high >= telemetry.vwap && telemetry.low <= telemetry.vwap) ? 'check-indicator ready' : 'check-indicator pending';
        }
    }

    function updateMetricsUI(stats, running) {
        if (!stats) return;
        
        // PnL Neto Real
        if (metricPnl) {
            const netPnl = stats.net_pnl_usd || 0;
            metricPnl.innerText = (netPnl >= 0 ? '+' : '') + `$${netPnl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
            metricPnl.className = netPnl >= 0 ? 'metric-value bull' : 'metric-value bear';
        }
        
        // Balance Real de la Cuenta
        if (metricBalance) {
            const bal = stats.real_balance || 0;
            metricBalance.innerText = `$${bal.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
        }
        
        // Win Rate Real
        if (metricWinrate) {
            metricWinrate.innerText = `${stats.win_rate || 0}%`;
            metricWinrate.className = (stats.win_rate >= 50) ? 'metric-value bull' : 'metric-value cyan';
        }
        
        // Total Operaciones Reales
        if (metricTrades) {
            const exec = stats.executed_trades || 0;
            const wins = stats.winning_trades || 0;
            const losses = stats.losing_trades || 0;
            metricTrades.innerText = `${exec} (${wins}W / ${losses}L)`;
        }
        
        // Profit Factor Real
        if (metricProfitFactor) {
            const pf = stats.profit_factor || 0.0;
            metricProfitFactor.innerText = pf.toFixed(2);
            metricProfitFactor.className = pf >= 1.0 ? 'metric-value bull' : 'metric-value bear';
        }
        
        // Sincronización Topstep
        if (metricSyncTime && stats.last_sync) {
            const timePart = stats.last_sync.includes(' ') ? stats.last_sync.split(' ')[1] : stats.last_sync;
            metricSyncTime.innerText = `Última: ${timePart}`;
        }
        
        // Update fleet stats breakdown chip summary
        if (fleetStatsSummary && stats.by_symbol) {
            const parts = [];
            Object.keys(stats.by_symbol).forEach(sym => {
                const s = stats.by_symbol[sym];
                if (s.total > 0 || s.executed > 0) {
                    const pnlVal = s.net_pnl || 0;
                    const pnlSign = pnlVal >= 0 ? '+' : '';
                    const pnlColor = pnlVal >= 0 ? '#10B981' : '#EF4444';
                    parts.push(`<span><strong>${sym}:</strong> ${s.executed || s.total} ops (<strong style="color:${pnlColor}">${pnlSign}$${pnlVal.toFixed(2)}</strong>)</span>`);
                }
            });
            fleetStatsSummary.innerHTML = parts.length > 0 ? parts.join(' | ') : '<span>Sincronizando operaciones reales...</span>';
        }
    }

    // ==========================================
    // 7. DEDICATED TERMINAL & LOGS
    // ==========================================
    function appendTerminalLine(log) {
        if (!terminalLogs) return;
        
        // Check symbol filter
        if (selectedTerminalSymbol !== 'ALL') {
            const logSym = (log.symbol || '').toUpperCase();
            if (logSym && logSym !== 'ALL' && logSym !== selectedTerminalSymbol) {
                return;
            }
        }

        const line = document.createElement('div');
        line.className = 'terminal-line';
        
        let catClass = 'term-info';
        if (log.category === 'TELEMETRY') catClass = 'term-telemetry';
        else if (log.category === 'SETUP') catClass = 'term-setup';
        else if (log.category === 'ORDER') catClass = 'term-order';
        else if (log.category === 'ERROR') catClass = 'term-error';
        else if (log.category === 'SYSTEM') catClass = 'term-system';

        const symPrefix = log.symbol ? `[${log.symbol}] ` : '';
        line.innerHTML = `<span class="term-time">[${log.timestamp || ''}]</span> <strong style="color:#38BDF8;">${escapeHtml(symPrefix)}</strong><span class="${catClass}">${escapeHtml(log.message || '')}</span>`;
        terminalLogs.appendChild(line);

        if (chkAutoScroll && chkAutoScroll.checked) {
            terminalLogs.scrollTop = terminalLogs.scrollHeight;
        }
    }

    async function reloadTerminalLogsForSymbol(sym) {
        if (!terminalLogs) return;
        terminalLogs.innerHTML = '';
        try {
            const url = sym === 'ALL' ? '/api/logs?limit=2500' : `/api/logs?symbol=${sym}&limit=2500`;
            const res = await fetch(url);
            if (!res.ok) return;
            const logs = await res.json();
            logs.forEach(l => appendTerminalLine(l));
        } catch (e) {
            console.error('Error reloading terminal logs:', e);
        }
    }

    function escapeHtml(str) {
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ==========================================
    // 8. UNIFIED OPERATIONS BREAKDOWN LOADER
    // ==========================================
    async function loadOperations() {
        try {
            const res = await fetch('/api/operations');
            if (!res.ok) return;
            const data = await res.json();
            rawOperations = data.csv_records || [];
            if (data.stats) updateMetricsUI(data.stats, isRunning);
            renderOperationsTable();
            loadRankingData();
        } catch (e) {
            console.error('Error loading operations:', e);
        }
    }

    function renderOperationsTable() {
        if (!operationsTableBody) return;
        operationsTableBody.innerHTML = '';
        
        const filterVal = selectOpFilter ? selectOpFilter.value : 'ALL';
        const filtered = rawOperations.slice().reverse().filter(r => {
            if (filterVal === 'ALL') return true;
            return (r.Simbolo || 'ES').toUpperCase() === filterVal;
        });

        if (filtered.length === 0) {
            operationsTableBody.innerHTML = `<tr><td colspan="17" style="text-align:center; color:#64748B; padding:20px;">No hay registros de operaciones para el filtro seleccionado (${filterVal}).</td></tr>`;
            return;
        }

        filtered.forEach(r => {
            const tr = document.createElement('tr');
            const estadoRaw = (r.Estado || '').toUpperCase();
            const isClosed = estadoRaw.includes('CERRADA');
            const isExecuted = estadoRaw.includes('EJECUTADA') || estadoRaw.includes('EN CURSO') || estadoRaw.includes('ABIERTA');
            const isRejected = estadoRaw.includes('RECHAZADA');
            const isLong = (r.Direccion || '').toUpperCase() === 'LONG';
            const sym = r.Simbolo || 'ES';
            
            const netPnl = parseFloat(r.PnL_Neto_USD || 0);
            const grossPnl = parseFloat(r.PnL_Bruto_USD || 0);
            const comms = parseFloat(r.Comisiones_USD || 0);
            
            let badgeClass = 'badge-rejected';
            if (isClosed) {
                badgeClass = netPnl >= 0 ? 'badge-executed' : 'badge-short';
            } else if (isExecuted) {
                badgeClass = 'badge-long';
            }
            
            const entryPrice = parseFloat(r.Precio_Entrada_Real || r.Precio_Mercado || 0);
            const exitPrice = r.Precio_Salida_Real && r.Precio_Salida_Real !== 'N/A' && r.Precio_Salida_Real !== 'PENDIENTE'
                ? parseFloat(r.Precio_Salida_Real).toLocaleString('en-US', { minimumFractionDigits: 2 })
                : (r.Precio_Salida_Real || '--.--');
            
            // Bracket OCO / Tipo Cierre Badge
            const hito = r.Hito_Alcanzado || 'Fijo (OCO)';
            let hitoBadge = `<span style="color:#64748B; font-size:11px;">${escapeHtml(hito)}</span>`;
            if (hito.includes('Take Profit') || hito.includes('TP (Fijo)')) {
                hitoBadge = `<span class="badge" style="background:rgba(16,185,129,0.15); color:#10B981; border:1px solid #10B981; font-weight:600;">🎯 ${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Stop Loss') || hito.includes('SL (Fijo)')) {
                hitoBadge = `<span class="badge" style="background:rgba(239,68,68,0.15); color:#EF4444; border:1px solid #EF4444; font-weight:600;">🛑 ${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Break-Even')) {
                hitoBadge = `<span class="badge" style="background:rgba(59,130,246,0.15); color:#3B82F6; border:1px solid #3B82F6; font-weight:600;">⚖️ ${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Fijo (OCO)')) {
                hitoBadge = `<span class="badge" style="background:rgba(100,116,139,0.15); color:#94A3B8; border:1px solid #64748B;">🔒 ${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Hito 4')) {
                hitoBadge = `<span class="badge badge-hito-4">${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Hito 3')) {
                hitoBadge = `<span class="badge badge-hito-3">${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Hito 2')) {
                hitoBadge = `<span class="badge badge-hito-2">${escapeHtml(hito)}</span>`;
            } else if (hito.includes('Hito 1')) {
                hitoBadge = `<span class="badge badge-hito-1">${escapeHtml(hito)}</span>`;
            } else if (hito === 'N/A') {
                hitoBadge = `<span style="color:#64748B; font-size:11px;">--</span>`;
            }

            // Máx Avance TP %
            const maxPctStr = r.Max_Pct_TP || '0.0%';
            const maxPctVal = parseFloat(maxPctStr);
            let maxPctColor = '#64748B';
            let maxPctDisplay = maxPctStr;
            if (!isNaN(maxPctVal)) {
                if (maxPctVal >= 100) {
                    maxPctColor = '#FACC15';
                    maxPctDisplay = `+${maxPctVal.toFixed(1)}%`;
                } else if (maxPctVal >= 50) {
                    maxPctColor = '#10B981';
                    maxPctDisplay = `+${maxPctVal.toFixed(1)}%`;
                } else if (maxPctVal > 0) {
                    maxPctColor = '#38BDF8';
                    maxPctDisplay = `+${maxPctVal.toFixed(1)}%`;
                } else {
                    maxPctColor = '#64748B';
                    maxPctDisplay = '0.0%';
                }
            } else if (maxPctStr === 'N/A') {
                maxPctDisplay = '--';
            }

            let motivoRechazo = '--';
            if (isRejected) {
                if (r.Motivo_Rechazo && r.Motivo_Rechazo !== 'N/A') {
                    const cod = (r.Codigo_Error && r.Codigo_Error !== 'N/A') ? ` [Cod: ${r.Codigo_Error}]` : '';
                    motivoRechazo = `${r.Motivo_Rechazo}${cod}`;
                } else if (r.Estado && r.Estado.includes(':')) {
                    motivoRechazo = r.Estado.split(':')[1].trim();
                } else {
                    motivoRechazo = 'Rechazo TopstepX';
                }
            }
            
            const horaApertura = r.Hora_Apertura || r.Hora || '--:--:--';
            let horaCierre = r.Hora_Cierre;
            if (!horaCierre || horaCierre === 'N/A' || horaCierre === 'PENDIENTE') {
                if (isClosed) {
                    horaCierre = (r.Ultima_Actualizacion && r.Ultima_Actualizacion.includes(' ')) 
                        ? r.Ultima_Actualizacion.split(' ')[1] 
                        : (r.Hora || '--:--:--');
                } else {
                    horaCierre = '<span style="color:#10B981; font-weight:600;">EN CURSO</span>';
                }
            }
            
            tr.innerHTML = `
                <td>${escapeHtml(r.Fecha || '')}</td>
                <td style="font-family:var(--font-mono); font-weight:600; color:#38BDF8;">${escapeHtml(horaApertura)}</td>
                <td style="font-family:var(--font-mono); font-weight:600; color:${isClosed ? '#94A3B8' : '#10B981'};">${horaCierre}</td>
                <td><span class="badge-sym">${sym}</span></td>
                <td><span class="badge ${badgeClass}">${escapeHtml(r.Estado || 'N/A')}</span></td>
                <td><span class="badge ${isLong ? 'badge-long' : 'badge-short'}">${r.Direccion || ''}</span></td>
                <td>${entryPrice > 0 ? entryPrice.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--'}</td>
                <td>${exitPrice}</td>
                <td>${hitoBadge}</td>
                <td style="color:${maxPctColor}; font-weight:700; font-family:var(--font-mono);">${escapeHtml(maxPctDisplay)}</td>
                <td style="color:${grossPnl >= 0 ? 'var(--bull-color)' : 'var(--bear-color)'}; font-weight:600;">${grossPnl >= 0 ? '+' : ''}$${grossPnl.toFixed(2)}</td>
                <td style="color:#94A3B8;">${comms > 0 ? `-$${comms.toFixed(2)}` : '$0.00'}</td>
                <td style="color:${netPnl >= 0 ? 'var(--bull-color)' : 'var(--bear-color)'}; font-weight:700;">${netPnl >= 0 ? '+' : ''}$${netPnl.toFixed(2)}</td>
                <td>${r.Order_ID || 'N/A'}</td>
                <td>${r.Exit_Order_ID || 'N/A'}</td>
                <td style="color:${isRejected ? '#EF4444' : '#94A3B8'}; font-weight:${isRejected ? '600' : '400'}; max-width:210px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(motivoRechazo)}">${escapeHtml(motivoRechazo)}</td>
                <td style="max-width:210px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(r.Condicion_Tecnica || '')}">${escapeHtml(r.Condicion_Tecnica || '')}</td>
            `;
            operationsTableBody.appendChild(tr);
        });
    }

    // ==========================================
    // 8.1 RANKING CUANTITATIVO Y TOTALES POR SÍMBOLO (NUEVA SECCIÓN)
    // ==========================================
    async function loadRankingData() {
        try {
            const res = await fetch('/api/ranking');
            if (!res.ok) return;
            const data = await res.json();
            renderRankingSection(data);
        } catch (e) {
            console.error('Error al cargar ranking de operaciones:', e);
        }
    }

    function renderRankingSection(data) {
        if (!data) return;
        latestRankingData = data;
        const gt = data.grand_total || {};
        const ranking = data.ranking || [];

        // Ribbon de Totales Globales
        if (rankTotalNet) {
            const netVal = gt.net_pnl || 0;
            rankTotalNet.textContent = `${netVal >= 0 ? '+' : ''}$${netVal.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
            rankTotalNet.style.color = netVal >= 0 ? '#10B981' : '#EF4444';
        }
        if (rankTotalGrossSub) {
            rankTotalGrossSub.textContent = `Bruto: $${(gt.gross_pnl || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })} | Fees: -$${(gt.commissions || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
        }
        if (rankTotalWinrate) {
            rankTotalWinrate.textContent = `${(gt.win_rate || 0).toFixed(1)}%`;
        }
        if (rankTotalTradesSub) {
            rankTotalTradesSub.textContent = `${gt.total_trades || 0} Operaciones (${gt.winning_trades || 0}W / ${gt.losing_trades || 0}L / ${gt.breakeven_trades || 0}BE)`;
        }
        if (rankTotalPf) {
            rankTotalPf.textContent = (gt.profit_factor || 0).toFixed(2);
        }
        if (rankLeaderSymbol) {
            const leader = ranking.find(r => r.total_trades > 0) || ranking[0];
            if (leader && leader.total_trades > 0) {
                rankLeaderSymbol.textContent = leader.symbol;
                if (rankLeaderPnl) {
                    const pnlVal = leader.net_pnl || 0;
                    rankLeaderPnl.textContent = `${pnlVal >= 0 ? '+' : ''}$${pnlVal.toFixed(2)} USD (${leader.win_rate.toFixed(1)}% WR)`;
                    rankLeaderPnl.style.color = pnlVal >= 0 ? '#10B981' : '#EF4444';
                }
            } else {
                rankLeaderSymbol.textContent = '--';
                if (rankLeaderPnl) rankLeaderPnl.textContent = 'En espera de operaciones';
            }
        }

        // Tabla Ranking por Símbolo (Totales Agrupados)
        if (rankingTableBody) {
            rankingTableBody.innerHTML = '';
            ranking.forEach(item => {
                const tr = document.createElement('tr');
                const net = item.net_pnl || 0;
                const gross = item.gross_pnl || 0;
                const comms = item.commissions || 0;
                const wr = item.win_rate || 0;
                const rankNum = item.rank || 1;

                let medal = `#${rankNum}`;
                if (rankNum === 1 && item.total_trades > 0) medal = '🥇 1';
                else if (rankNum === 2 && item.total_trades > 0) medal = '🥈 2';
                else if (rankNum === 3 && item.total_trades > 0) medal = '🥉 3';

                tr.innerHTML = `
                    <td style="font-weight:700; color:${rankNum <= 3 && item.total_trades > 0 ? '#FACC15' : '#94A3B8'}; text-align:center;">${medal}</td>
                    <td><span class="badge-sym" style="cursor:pointer;" onclick="switchSymbol('${item.symbol}')">${item.symbol}</span></td>
                    <td style="font-size:12px; color:#F8FAFC;">${escapeHtml(item.name || item.symbol)}</td>
                    <td style="text-align:center; font-family:var(--font-mono); font-weight:700;">${item.total_trades}</td>
                    <td style="text-align:center; font-family:var(--font-mono); font-size:12px;">
                        <span style="color:#10B981;">${item.winning_trades}</span> / <span style="color:#EF4444;">${item.losing_trades}</span> / <span style="color:#94A3B8;">${item.breakeven_trades}</span>
                    </td>
                    <td style="text-align:center;">
                        <span style="font-weight:700; font-family:var(--font-mono); color:${wr >= 50 ? '#10B981' : '#EF4444'};">${wr.toFixed(1)}%</span>
                    </td>
                    <td style="text-align:right; font-family:var(--font-mono); color:${gross >= 0 ? '#10B981' : '#EF4444'};">${gross >= 0 ? '+' : ''}$${gross.toFixed(2)}</td>
                    <td style="text-align:right; font-family:var(--font-mono); color:#94A3B8;">-$${comms.toFixed(2)}</td>
                    <td style="text-align:right; font-family:var(--font-mono); font-weight:700; font-size:13px; color:${net >= 0 ? '#10B981' : '#EF4444'};">${net >= 0 ? '+' : ''}$${net.toFixed(2)}</td>
                    <td style="text-align:center; font-family:var(--font-mono); font-weight:600; color:#38BDF8;">${item.profit_factor.toFixed(2)}</td>
                    <td style="text-align:right; font-family:var(--font-mono); color:#10B981;">${item.best_trade > 0 ? '+' : ''}$${item.best_trade.toFixed(2)}</td>
                    <td style="text-align:right; font-family:var(--font-mono); color:#EF4444;">${item.worst_trade !== 0 ? '' : ''}$${item.worst_trade.toFixed(2)}</td>
                    <td style="text-align:center;">
                        <button class="btn-small" style="font-size:10px; padding:3px 8px;" onclick="filterRankingDetail('${item.symbol}')">Ver Detalle</button>
                    </td>
                `;
                rankingTableBody.appendChild(tr);
            });
        }

        // Fila de Totales en Pie de Tabla
        if (rankingTableFoot) {
            const netG = gt.net_pnl || 0;
            const grossG = gt.gross_pnl || 0;
            const commsG = gt.commissions || 0;
            const wrG = gt.win_rate || 0;

            rankingTableFoot.innerHTML = `
                <tr>
                    <td colspan="3" style="text-align:left; font-weight:700; color:#FACC15; padding:10px;">TOTAL GENERAL CONSOLIDADO</td>
                    <td style="text-align:center; font-family:var(--font-mono); font-weight:700;">${gt.total_trades || 0}</td>
                    <td style="text-align:center; font-family:var(--font-mono); font-size:12px;">
                        <span style="color:#10B981;">${gt.winning_trades || 0}</span> / <span style="color:#EF4444;">${gt.losing_trades || 0}</span> / <span style="color:#94A3B8;">${gt.breakeven_trades || 0}</span>
                    </td>
                    <td style="text-align:center; font-weight:700; font-family:var(--font-mono); color:${wrG >= 50 ? '#10B981' : '#EF4444'};">${wrG.toFixed(1)}%</td>
                    <td style="text-align:right; font-family:var(--font-mono); color:${grossG >= 0 ? '#10B981' : '#EF4444'};">${grossG >= 0 ? '+' : ''}$${grossG.toFixed(2)}</td>
                    <td style="text-align:right; font-family:var(--font-mono); color:#94A3B8;">-$${commsG.toFixed(2)}</td>
                    <td style="text-align:right; font-family:var(--font-mono); font-weight:700; font-size:14px; color:${netG >= 0 ? '#10B981' : '#EF4444'};">${netG >= 0 ? '+' : ''}$${netG.toFixed(2)}</td>
                    <td style="text-align:center; font-family:var(--font-mono); font-weight:700; color:#38BDF8;">${(gt.profit_factor || 0).toFixed(2)}</td>
                    <td colspan="3" style="text-align:center; color:#64748B; font-size:11px;">Consolidado Auditoría TopstepX</td>
                </tr>
            `;
        }

        renderRankingOperationsDetail();
    }

    function renderRankingOperationsDetail() {
        if (!rankingOperationsDetailBody || !latestRankingData) return;
        rankingOperationsDetailBody.innerHTML = '';

        const filterSym = selectRankingDetailFilter ? selectRankingDetailFilter.value : 'ALL';
        const allTrades = latestRankingData.all_trades || [];

        const filtered = allTrades.filter(t => {
            if (filterSym === 'ALL') return true;
            return (t.simbolo || '').toUpperCase() === filterSym;
        });

        if (filtered.length === 0) {
            rankingOperationsDetailBody.innerHTML = `<tr><td colspan="17" style="text-align:center; color:#64748B; padding:24px;">No hay operaciones registradas para el filtro seleccionado (${filterSym}).</td></tr>`;
            return;
        }

        filtered.forEach(t => {
            const tr = document.createElement('tr');
            const isLong = (t.direccion || '').toUpperCase() === 'LONG';
            const net = parseFloat(t.pnl_neto || 0);
            const gross = parseFloat(t.pnl_bruto || 0);
            const comms = parseFloat(t.comisiones || 0);
            const estado = (t.estado || '').toUpperCase();
            const isClosed = estado.includes('CERRADA');
            const isLive = estado.includes('EN CURSO') || estado.includes('ABIERTA');

            let badgeClass = 'badge-rejected';
            if (isClosed) badgeClass = net >= 0 ? 'badge-executed' : 'badge-short';
            else if (isLive) badgeClass = 'badge-long';

            const pIn = parseFloat(t.precio_entrada || 0);
            const pInStr = pIn > 0 ? pIn.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--.--';
            const pOut = parseFloat(t.precio_salida || 0);
            const pOutStr = !isNaN(pOut) && pOut > 0 ? pOut.toLocaleString('en-US', { minimumFractionDigits: 2 }) : (t.precio_salida || '--.--');

            tr.innerHTML = `
                <td>${escapeHtml(t.fecha || '')}</td>
                <td style="font-family:var(--font-mono); font-weight:600; color:#38BDF8;">${escapeHtml(t.hora_apertura || '--:--:--')}</td>
                <td style="font-family:var(--font-mono); font-weight:600; color:${isClosed ? '#94A3B8' : '#10B981'};">${isLive ? '<span style="color:#10B981; font-weight:700;">EN CURSO</span>' : escapeHtml(t.hora_cierre || '--:--:--')}</td>
                <td><span class="badge-sym" style="cursor:pointer;" onclick="switchSymbol('${t.simbolo}')">${t.simbolo}</span></td>
                <td><span class="badge ${isLong ? 'badge-long' : 'badge-short'}">${t.direccion || ''}</span></td>
                <td><span class="badge ${badgeClass}">${escapeHtml(t.estado || '')}</span></td>
                <td style="font-family:var(--font-mono); font-weight:600;">${pInStr}</td>
                <td style="font-family:var(--font-mono);">${pOutStr}</td>
                <td style="font-family:var(--font-mono); color:#38BDF8;">${escapeHtml(String(t.vwap_entrada || '--'))}</td>
                <td style="font-family:var(--font-mono); color:#10B981;">${escapeHtml(String(t.tp_inicial || '--'))}</td>
                <td style="font-family:var(--font-mono); font-weight:700; color:#10B981;">🎯 ${escapeHtml(String(t.tp_max_alcanzado || '--'))}</td>
                <td style="font-family:var(--font-mono); color:#EF4444;">${escapeHtml(String(t.sl_inicial || '--'))}</td>
                <td style="font-family:var(--font-mono); font-weight:700; color:#EF4444;">🛑 ${escapeHtml(String(t.sl_max_alcanzado || '--'))}</td>
                <td style="font-family:var(--font-mono); color:${gross >= 0 ? '#10B981' : '#EF4444'};">${gross >= 0 ? '+' : ''}$${gross.toFixed(2)}</td>
                <td style="font-family:var(--font-mono); color:#94A3B8;">${comms > 0 ? `-$${comms.toFixed(2)}` : '$0.00'}</td>
                <td style="font-family:var(--font-mono); font-weight:700; color:${net >= 0 ? '#10B981' : '#EF4444'};">${net >= 0 ? '+' : ''}$${net.toFixed(2)}</td>
                <td style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px;" title="${escapeHtml(t.condicion || '')}">${escapeHtml(t.condicion || '')}</td>
            `;
            rankingOperationsDetailBody.appendChild(tr);
        });
    }

    window.filterRankingDetail = function(sym) {
        if (selectRankingDetailFilter) {
            selectRankingDetailFilter.value = sym;
            renderRankingOperationsDetail();
            selectRankingDetailFilter.scrollIntoView({ behavior: 'smooth' });
        }
    };

    // ==========================================
    // 9. MULTI-SYMBOL CONTROLS & REST ACTIONS
    // ==========================================
    async function switchSymbol(symbolKey) {
        try {
            activeSymbol = symbolKey;
            // Update tabs visually immediately
            document.querySelectorAll('.symbol-tab').forEach(t => {
                t.classList.toggle('active', t.getAttribute('data-symbol') === symbolKey);
            });
            document.querySelectorAll('.fleet-card').forEach(c => {
                c.classList.toggle('active-view', c.getAttribute('data-symbol') === symbolKey);
            });
            
            const specSym = document.getElementById('spec-symbol');
            if (specSym) specSym.innerText = symbolKey;
            if (hudSymbol) hudSymbol.innerText = symbolKey;
            if (radarSymbolBadge) radarSymbolBadge.innerText = symbolKey;

            const res = await fetch('/api/engine/symbol', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: symbolKey })
            });
            const data = await res.json();
            if (data.success) {
                // Refresh status immediately to update controls and specs for this symbol
                try {
                    const stRes = await fetch('/api/status');
                    if (stRes.ok) {
                        const st = await stRes.json();
                        updateStatusUI(st);
                    }
                } catch (_) {}
                fetchChartData(symbolKey);
            }
        } catch (e) {
            console.error('Error al cambiar de símbolo:', e);
        }
    }

    async function toggleSymbol(symbolKey) {
        try {
            const sym = symbolKey || activeSymbol;
            const res = await fetch('/api/symbol/toggle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: sym })
            });
            const data = await res.json();
            if (data.success) {
                // Refresh status
                const st = await (await fetch('/api/status')).json();
                updateStatusUI(st);
                if (selectedTerminalSymbol === sym || selectedTerminalSymbol === 'ALL') {
                    reloadTerminalLogsForSymbol(selectedTerminalSymbol);
                }
            }
        } catch (e) {
            alert('Error al modificar estado de símbolo: ' + e);
        }
    }

    async function startEngine() {
        try {
            const res = await fetch('/api/engine/start', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                isRunning = true;
                if (statusPill) statusPill.className = 'status-badge running';
                if (statusText) statusText.innerText = 'MOTOR EN EJECUCIÓN';
            }
        } catch (e) {
            alert('Error al iniciar el motor: ' + e);
        }
    }

    async function stopEngine() {
        try {
            const res = await fetch('/api/engine/stop', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                isRunning = false;
                if (statusPill) statusPill.className = 'status-badge stopped';
                if (statusText) statusText.innerText = 'MOTOR DETENIDO';
            }
        } catch (e) {
            alert('Error al detener el motor: ' + e);
        }
    }

    async function emergencyFlatten() {
        if (!confirm(`⚠️ ¿CONFIRMAR KILL SWITCH? Se cerrará la posición del símbolo activo (${activeSymbol}) en TopstepX.`)) {
            return;
        }
        try {
            const res = await fetch('/api/engine/flatten', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: activeSymbol })
            });
            const data = await res.json();
            alert('Respuesta de Cierre de Emergencia: ' + JSON.stringify(data));
        } catch (e) {
            alert('Error en Kill Switch: ' + e);
        }
    }

    // ==========================================
    // 10. EVENT LISTENERS
    // ==========================================
    if (btnStart) btnStart.addEventListener('click', startEngine);
    if (btnStop) btnStop.addEventListener('click', stopEngine);
    if (btnFlatten) btnFlatten.addEventListener('click', emergencyFlatten);
    if (btnEmergencyTop) btnEmergencyTop.addEventListener('click', emergencyFlatten);

    if (btnSyncTopstep) {
        btnSyncTopstep.addEventListener('click', async () => {
            btnSyncTopstep.disabled = true;
            btnSyncTopstep.innerHTML = `<span class="pulse-dot" style="background:#00F2FE;"></span> SINCRONIZANDO...`;
            try {
                const res = await fetch('/api/sync', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    if (data.stats) updateMetricsUI(data.stats, isRunning);
                    if (data.operations) {
                        rawOperations = data.operations;
                        renderOperationsTable();
                    }
                }
            } catch (e) {
                console.error('Error al sincronizar con TopstepX:', e);
            } finally {
                btnSyncTopstep.disabled = false;
                btnSyncTopstep.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg> SINCRONIZAR TOPSTEP`;
            }
        });
    }

    if (btnToggleActiveTrading) {
        btnToggleActiveTrading.addEventListener('click', () => {
            toggleSymbol(activeSymbol);
        });
    }

    if (btnSpecToggle) {
        btnSpecToggle.addEventListener('click', () => {
            toggleSymbol(activeSymbol);
        });
    }

    // Chart symbol tabs
    if (chartSymbolTabs) {
        chartSymbolTabs.querySelectorAll('.symbol-tab').forEach(tab => {
            const sym = tab.getAttribute('data-symbol');
            tab.addEventListener('click', () => {
                switchSymbol(sym);
            });
            // Double click on tab directly toggles trading for that symbol
            tab.addEventListener('dblclick', (e) => {
                e.preventDefault();
                toggleSymbol(sym);
            });
        });
    }

    // Operations symbol filter
    if (selectOpFilter) {
        selectOpFilter.addEventListener('change', renderOperationsTable);
    }

    // Ranking detail filter
    if (selectRankingDetailFilter) {
        selectRankingDetailFilter.addEventListener('change', renderRankingOperationsDetail);
    }

    // Dedicated terminal symbol filter buttons
    if (terminalSymBar) {
        terminalSymBar.querySelectorAll('.term-sym-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const sym = btn.getAttribute('data-symbol');
                selectedTerminalSymbol = sym;
                terminalSymBar.querySelectorAll('.term-sym-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                reloadTerminalLogsForSymbol(sym);
            });
        });
    }

    if (btnDownloadCsv) {
        btnDownloadCsv.addEventListener('click', () => {
            window.location.href = '/api/operations/download';
        });
    }

    if (btnClearLogs) {
        btnClearLogs.addEventListener('click', () => {
            if (terminalLogs) terminalLogs.innerHTML = '';
        });
    }

    // Deck tab switching
    deckTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const targetPaneId = tab.getAttribute('data-tab');
            deckTabs.forEach(t => t.classList.remove('active'));
            tabPanes.forEach(p => p.classList.remove('active'));
            
            tab.classList.add('active');
            const targetPane = document.getElementById(targetPaneId);
            if (targetPane) targetPane.classList.add('active');
        });
    });

    // Boot Sequence
    initChart();
    fetchChartData();
    connectWebSocket();
    loadOperations();
    loadRankingData();
    
    // Refresh operations and chart every 45s
    setInterval(() => {
        fetchChartData();
        loadOperations();
        loadRankingData();
    }, 45000);
});
