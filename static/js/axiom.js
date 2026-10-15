/**
 * AXIOM ALGORITHMIC - INSTITUTIONAL CLIENT ENGINE
 * Handles TradingView Lightweight Charts, WebSocket streaming, UI Telemetry & Controls
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
    let activeFilter = 'ALL';
    
    // UI Elements
    const statusPill = document.getElementById('engine-status-pill');
    const statusText = document.getElementById('engine-status-text');
    const btnStart = document.getElementById('btn-start-engine');
    const btnStop = document.getElementById('btn-stop-engine');
    const btnFlatten = document.getElementById('btn-flatten');
    const btnEmergencyTop = document.getElementById('btn-emergency-top');
    const symbolTabs = document.querySelectorAll('.symbol-tab');
    
    // Telemetry HUD Elements
    const hudSymbol = document.getElementById('hud-symbol');
    const hudPrice = document.getElementById('hud-price');
    const hudVwap = document.getElementById('hud-vwap');
    const hudUpper = document.getElementById('hud-upper');
    const hudLower = document.getElementById('hud-lower');
    const hudSpread = document.getElementById('hud-spread');
    
    // Metrics Ribbon
    const metricPnl = document.getElementById('metric-pnl');
    const metricWinrate = document.getElementById('metric-winrate');
    const metricTrades = document.getElementById('metric-trades');
    const metricPosition = document.getElementById('metric-position');
    const metricCooldown = document.getElementById('metric-cooldown');
    
    // Clocks
    const clockNy = document.getElementById('clock-ny');
    const clockUtc = document.getElementById('clock-utc');
    const marketStatusPill = document.getElementById('market-status-pill');
    
    // Bottom Deck Tabs & Terminal
    const deckTabs = document.querySelectorAll('.deck-tab');
    const tabPanes = document.querySelectorAll('.tab-pane');
    const terminalLogs = document.getElementById('terminal-logs');
    const btnClearLogs = document.getElementById('btn-clear-logs');
    const chkAutoScroll = document.getElementById('chk-autoscroll');
    const operationsTableBody = document.getElementById('operations-table-body');
    const btnDownloadCsv = document.getElementById('btn-download-csv');
    const matrixTableBody = document.getElementById('matrix-table-body');

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

        // 5m Candlestick series (Institutional Bull/Bear palette)
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

        // Auto resize chart
        window.addEventListener('resize', () => {
            if (chart && chartContainer) {
                chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
            }
        });
    }

    // ==========================================
    // 2. FETCH & RENDER CHART DATA
    // ==========================================
    async function fetchChartData() {
        try {
            const res = await fetch('/api/chart/data');
            if (!res.ok) return;
            const data = await res.json();
            
            if (data.candles && data.candles.length > 0) {
                candleSeries.setData(data.candles);
                if (data.vwap) vwapSeries.setData(data.vwap);
                if (data.upper_band) upperBandSeries.setData(data.upper_band);
                if (data.lower_band) lowerBandSeries.setData(data.lower_band);
                
                // Add execution markers on candles if available
                loadChartMarkers();
                chart.timeScale().fitContent();
            }
        } catch (e) {
            console.error('Error fetching chart data:', e);
        }
    }

    async function loadChartMarkers() {
        try {
            const res = await fetch('/api/operations');
            if (!res.ok) return;
            const data = await res.json();
            const markers = [];
            
            (data.csv_records || []).forEach(record => {
                if (record.Estado && record.Estado.includes('EJECUTADA')) {
                    // Try to approximate timestamp or place on matching price
                    const isLong = record.Direccion === 'LONG';
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
        
        // UTC Time
        const utcStr = now.toISOString().slice(11, 19) + ' UTC';
        if (clockUtc) clockUtc.innerText = utcStr;
        
        // New York Time
        const nyFormatter = new Intl.DateTimeFormat('en-US', {
            timeZone: 'America/New_York',
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
        const nyStr = nyFormatter.format(now) + ' ET';
        if (clockNy) clockNy.innerText = nyStr;
        
        // CME Session status: Futures trade nearly 24h, break between 17:00-18:00 ET
        const nyHour = parseInt(nyFormatter.format(now).split(':')[0], 10);
        const day = now.getUTCDay();
        let isSessionOpen = true;
        if (day === 6) isSessionOpen = false; // Saturday
        if (day === 0 && nyHour < 18) isSessionOpen = false; // Sunday before 18:00
        if (nyHour === 17) isSessionOpen = false; // Daily maintenance break
        
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
                message: '✅ Conexión WebSocket establecida con el servidor Axiom.'
            });
        };

        ws.onmessage = (event) => {
            try {
                const msg = json = JSON.parse(event.data);
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
                    updateTelemetryUI(msg.telemetry);
                    updateMetricsUI(msg.stats, msg.running);
                } else if (msg.type === 'log') {
                    appendTerminalLine(msg.data);
                    // Refresh operations table on trade event
                    if (msg.data.category === 'ORDER' || msg.data.category === 'SYSTEM') {
                        loadOperations();
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
                message: '⚠️ Desconexión WebSocket. Reintentando en 3 segundos...'
            });
            setTimeout(connectWebSocket, 3000);
        };
    }

    // ==========================================
    // 5. UI UPDATERS
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
        
        // Highlight active symbol tab
        symbolTabs.forEach(tab => {
            if (tab.getAttribute('data-symbol') === activeSymbol) {
                tab.classList.add('active');
            } else {
                tab.classList.remove('active');
            }
        });
        
        if (hudSymbol) hudSymbol.innerText = activeSymbol;
        
        // Update specs
        const specSym = document.getElementById('spec-symbol');
        const specContract = document.getElementById('spec-contract');
        const specAccount = document.getElementById('spec-account');
        if (specSym) specSym.innerText = activeSymbol;
        if (specContract) specContract.innerText = status.contract_name || 'Detectando...';
        if (specAccount) specAccount.innerText = `${status.account_name} (${status.account_id})`;
        
        if (status.telemetry) updateTelemetryUI(status.telemetry);
        if (status.stats) updateMetricsUI(status.stats, isRunning);
        if (status.symbols_matrix) updateMatrixUI(status.symbols_matrix);
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
        
        // Cooldown timer update
        if (metricCooldown) {
            const cd = telemetry.cooldown_remaining || 0;
            if (cd > 0) {
                metricCooldown.innerText = `${cd}s (PAUSA)`;
                metricCooldown.className = 'metric-value bear';
            } else {
                metricCooldown.innerText = '0s (LISTO)';
                metricCooldown.className = 'metric-value bull';
            }
        }
        
        // Setup checklist dynamic updates
        const checkTrend = document.getElementById('check-trend');
        const checkTouch = document.getElementById('check-touch');
        if (checkTrend) {
            if (Math.abs(spread) < 4.0) {
                checkTrend.innerText = 'PROXIMIDAD ALTA';
                checkTrend.className = 'check-indicator ready';
            } else {
                checkTrend.innerText = 'BUSCANDO TEST';
                checkTrend.className = 'check-indicator pending';
            }
        }
        if (checkTouch) {
            checkTouch.innerText = (telemetry.high >= telemetry.vwap && telemetry.low <= telemetry.vwap) ? 'IMPACTO VWAP' : 'EN CANAL';
            checkTouch.className = (telemetry.high >= telemetry.vwap && telemetry.low <= telemetry.vwap) ? 'check-indicator ready' : 'check-indicator pending';
        }
    }

    function updateMetricsUI(stats, running) {
        if (!stats) return;
        if (metricPnl) {
            const pnl = stats.net_pnl_usd || 0;
            metricPnl.innerText = (pnl >= 0 ? '+' : '') + `$${pnl.toFixed(2)}`;
            metricPnl.className = pnl >= 0 ? 'metric-value bull' : 'metric-value bear';
        }
        if (metricWinrate) {
            metricWinrate.innerText = `${stats.win_rate || 0}%`;
        }
        if (metricTrades) {
            metricTrades.innerText = `${stats.total_trades || 0} (${stats.longs || 0}L / ${stats.shorts || 0}S)`;
        }
        if (metricPosition) {
            metricPosition.innerText = running ? 'MONITOR FLAT' : 'OFFLINE';
            metricPosition.className = running ? 'metric-value cyan' : 'metric-value dim';
        }
    }

    // ==========================================
    // 6. TERMINAL & LOGGING STREAM
    // ==========================================
    function appendTerminalLine(log) {
        if (!terminalLogs) return;
        
        // Check filter
        if (activeFilter !== 'ALL' && log.category !== activeFilter) {
            return;
        }

        const line = document.createElement('div');
        line.className = 'terminal-line';
        
        let catClass = 'term-info';
        if (log.category === 'TELEMETRY') catClass = 'term-telemetry';
        else if (log.category === 'SETUP') catClass = 'term-setup';
        else if (log.category === 'ORDER') catClass = 'term-order';
        else if (log.category === 'ERROR') catClass = 'term-error';
        else if (log.category === 'SYSTEM') catClass = 'term-system';

        line.innerHTML = `<span class="term-time">[${log.timestamp || ''}]</span> <span class="${catClass}">${escapeHtml(log.message || '')}</span>`;
        terminalLogs.appendChild(line);

        if (chkAutoScroll && chkAutoScroll.checked) {
            terminalLogs.scrollTop = terminalLogs.scrollHeight;
        }
    }

    function escapeHtml(str) {
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ==========================================
    // 7. OPERATIONS TABLE LOADER
    // ==========================================
    async function loadOperations() {
        try {
            const res = await fetch('/api/operations');
            if (!res.ok) return;
            const data = await res.json();
            
            if (!operationsTableBody) return;
            operationsTableBody.innerHTML = '';
            
            const records = (data.csv_records || []).slice().reverse(); // newest first
            if (records.length === 0) {
                operationsTableBody.innerHTML = '<tr><td colspan="9" style="text-align:center; color:#64748B; padding:20px;">No hay registros de operaciones en la caja negra aún.</td></tr>';
                return;
            }

            records.forEach(r => {
                const tr = document.createElement('tr');
                const isExecuted = (r.Estado || '').includes('EJECUTADA');
                const isLong = (r.Direccion || '').toUpperCase() === 'LONG';
                
                tr.innerHTML = `
                    <td>${r.Fecha || ''} ${r.Hora || ''}</td>
                    <td><span class="badge ${isExecuted ? 'badge-executed' : 'badge-rejected'}">${r.Estado || 'N/A'}</span></td>
                    <td><span class="badge ${isLong ? 'badge-long' : 'badge-short'}">${r.Direccion || ''}</span></td>
                    <td>${parseFloat(r.Precio_Mercado || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                    <td>${r.SL_Ticks || ''} ticks</td>
                    <td>${r.TP_Ticks || ''} ticks</td>
                    <td style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${r.Condicion_Tecnica || ''}">${r.Condicion_Tecnica || ''}</td>
                    <td>${r.Order_ID || 'N/A'}</td>
                    <td>${r.Respuesta_Servidor || ''}</td>
                `;
                operationsTableBody.appendChild(tr);
            });
        } catch (e) {
            console.error('Error loading operations:', e);
        }
    }

    // ==========================================
    // 8. MULTI-SYMBOL MATRIX LOADER
    // ==========================================
    function updateMatrixUI(matrix) {
        if (!matrixTableBody || !matrix) return;
        matrixTableBody.innerHTML = '';
        
        Object.keys(matrix).forEach(sym => {
            const item = matrix[sym];
            const tr = document.createElement('tr');
            const spread = item.spread_vwap || 0;
            const spreadClass = spread >= 0 ? 'bull' : 'bear';
            
            tr.innerHTML = `
                <td><strong>${sym}</strong></td>
                <td>${(item.price || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                <td style="color:var(--vwap-color);">${(item.vwap || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                <td style="color:var(--upper-band-color);">${(item.upper || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                <td style="color:var(--lower-band-color);">${(item.lower || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                <td class="${spreadClass}">${spread >= 0 ? '+' : ''}${spread.toFixed(2)}</td>
                <td><button class="btn-small btn-switch-matrix" data-symbol="${sym}">Operar</button></td>
            `;
            matrixTableBody.appendChild(tr);
        });

        document.querySelectorAll('.btn-switch-matrix').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const s = e.target.getAttribute('data-symbol');
                switchSymbol(s);
            });
        });
    }

    // ==========================================
    // 9. ENGINE REST ACTIONS & CONTROLS
    // ==========================================
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
        if (!confirm('⚠️ ¿CONFIRMAR KILL SWITCH? Se cerrarán inmediatamente todas las posiciones abiertas en TopstepX.')) {
            return;
        }
        try {
            const res = await fetch('/api/engine/flatten', { method: 'POST' });
            const data = await res.json();
            alert('Respuesta de Cierre de Emergencia: ' + JSON.stringify(data));
        } catch (e) {
            alert('Error en Kill Switch: ' + e);
        }
    }

    async function switchSymbol(symbolKey) {
        try {
            const res = await fetch('/api/engine/symbol', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: symbolKey })
            });
            const data = await res.json();
            if (data.success) {
                activeSymbol = symbolKey;
                symbolTabs.forEach(t => {
                    t.classList.toggle('active', t.getAttribute('data-symbol') === symbolKey);
                });
                if (hudSymbol) hudSymbol.innerText = symbolKey;
                // Reload chart for new symbol
                fetchChartData();
            }
        } catch (e) {
            alert('Error al cambiar de símbolo: ' + e);
        }
    }

    // Event Listeners
    if (btnStart) btnStart.addEventListener('click', startEngine);
    if (btnStop) btnStop.addEventListener('click', stopEngine);
    if (btnFlatten) btnFlatten.addEventListener('click', emergencyFlatten);
    if (btnEmergencyTop) btnEmergencyTop.addEventListener('click', emergencyFlatten);

    symbolTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const sym = tab.getAttribute('data-symbol');
            switchSymbol(sym);
        });
    });

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

    // Deck Tab Switching
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
    
    // Refresh operations and chart every 60 seconds as a secondary safeguard
    setInterval(() => {
        fetchChartData();
        loadOperations();
    }, 60000);
});
