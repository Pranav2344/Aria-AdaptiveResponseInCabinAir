/**
 * ARIA v2 — 5-sensor dashboard with ON/OFF notifications & suggestions
 */

const SENSORS = ['aqi', 'co2', 'pm25', 'temperature', 'humidity'];

const INDUSTRY_BARRIERS = [
    { sensor: 'CO2', safe: '< 800 ppm', warning: '800-1200 ppm', critical: '> 1200 ppm', source: 'ASHRAE 62.1' },
    { sensor: 'AQI', safe: '0-50', warning: '51-150', critical: '> 150', source: 'EPA Standards' },
    { sensor: 'PM2.5', safe: '< 35 µg/m³', warning: '35-55 µg/m³', critical: '> 55 µg/m³', source: 'EPA 24-hour' },
    { sensor: 'Temperature', safe: '18-24°C', warning: '15-18 or 24-28°C', critical: '< 15 or > 28°C', source: 'ASHRAE 55' },
    { sensor: 'Humidity', safe: '30-60%', warning: '20-30 or 60-70%', critical: '< 20 or > 70%', source: 'ASHRAE 55' }
];

const state = {
    startTime: Date.now(),
    connected: false,
    chart: null,
    chartData: { labels: [], aqi: [], co2: [], pm25: [], temp: [], hum: [] },
    maxPoints: 50,
    currentManualMode: null,
    lastRecommendedMode: null,
    pendingHistoryDecision: false,
};

// ── WebSocket ──────────────────────────────────────────────────
let ws = null;
let reconnectAttempts = 0;
const maxReconnectAttempts = 5;
const baseReconnectDelay = 1000;

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    
    ws.onopen = () => {
        console.log('✓ WebSocket connected');
        state.connected = true;
        reconnectAttempts = 0;
        updateConnectionUI(true);
        updateLoadingOverlay(true);
        showToast('ARIA Agent connected', 'success', 'fa-check-circle');
    };
    
    ws.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type === 'connection_ack') {
            return;
        }

        if (!payload.sensors || !payload.decision) {
            console.warn('Ignoring incomplete websocket payload', payload);
            return;
        }

        updateSensors(payload.sensors);
        updateDecision(payload.decision);
        updateGauge(payload.decision.risk_score);
        updateSensorDots(payload.decision.sensor_status);
        updateSensorBars(payload.sensors);
        updateChart(payload.sensors);
        maybeLogUserModeChange(payload.decision);
        fireToastAlerts(payload.decision);
    };
    
    ws.onclose = () => {
        console.log('✗ WebSocket disconnected');
        state.connected = false;
        updateConnectionUI(false);
        showToast('Connection lost - reconnecting...', 'critical', 'fa-plug-circle-exclamation');
        attemptReconnect();
    };
    
    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
        state.connected = false;
        updateConnectionUI(false);
    };
}

function attemptReconnect() {
    if (reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts++;
        const delay = baseReconnectDelay * Math.pow(2, reconnectAttempts - 1);
        console.log(`Reconnecting... (attempt ${reconnectAttempts}/${maxReconnectAttempts})`);
        setTimeout(() => connectWebSocket(), delay);
    }
}

// ── Connection UI ───────────────────────────────────────────
function updateConnectionUI(online) {
    const pill = document.getElementById('connection-status');
    const label = pill.querySelector('.conn-label');
    if (label) label.textContent = online ? 'Live' : 'Offline';
    pill.classList.toggle('online', online);
}

function updateLoadingOverlay(isConnected) {
    const overlay = document.getElementById('connection-overlay');
    const title = document.getElementById('overlay-title');
    const message = document.getElementById('overlay-message');
    const spinner = document.querySelector('.overlay-spinner');
    const progress = document.querySelector('.loading-progress');
    
    if (isConnected) {
        // Update content for connected state
        title.textContent = 'Connected Successfully';
        message.innerHTML = '<i class="fas fa-check-circle" style="color: var(--accent-green); margin-right: 0.5rem; font-size: 1.1em;"></i>ARIA Agent initialized';
        if (spinner) spinner.style.animation = 'none';
        if (spinner) spinner.style.opacity = '0';
        if (progress) progress.style.display = 'none';
        
        // Show success animation and fade out
        overlay.style.animation = 'successFade 0.8s ease 0.5s both';
        setTimeout(() => {
            overlay.classList.add('hidden');
        }, 1400);
    }
}

// ── Sensor Values ───────────────────────────────────────────
function updateSensors(s) {
    setVal('val-aqi', s.aqi);
    setVal('val-co2', s.co2);
    setVal('val-pm25', s.pm25);
    setVal('val-temperature', s.temperature);
    setVal('val-humidity', s.humidity);
}

function setVal(id, v) {
    const el = document.getElementById(id);
    if (!el) return;
    const display = typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(1)) : v;
    el.style.opacity = '0.3';
    setTimeout(() => { el.textContent = display; el.style.opacity = '1'; }, 120);
}

// ── Sensor Status Dots & Card Alerts ────────────────────────
function updateSensorDots(status) {
    SENSORS.forEach(s => {
        const dot = document.getElementById(`dot-${s}`);
        const card = document.getElementById(`card-${s}`);
        const level = status[s] || 'good';
        if (dot) dot.className = 'sensor-status-dot ' + level;
        if (card) {
            card.classList.remove('alert-warning', 'alert-danger', 'alert-critical');
            if (level === 'critical' || level === 'danger') card.classList.add('alert-danger');
            else if (level === 'warning' || level === 'caution') card.classList.add('alert-warning');
        }
    });
}

// ── Sensor Bars ─────────────────────────────────────────────
function updateSensorBars(s) {
    setBBar('bar-aqi', s.aqi, 500);
    setBBar('bar-co2', s.co2, 3000);
    setBBar('bar-pm25', s.pm25, 300);
    setBBar('bar-temperature', Math.max(0, s.temperature), 55);
    setBBar('bar-humidity', s.humidity, 100);
}

function setBBar(id, val, max) {
    const el = document.getElementById(id);
    if (!el) return;
    const pct = Math.min(100, (val / max) * 100);
    el.style.width = pct + '%';
    if (pct > 75) el.style.background = '#ef4444';
    else if (pct > 50) el.style.background = '#f97316';
    else if (pct > 30) el.style.background = '#f59e0b';
    else el.style.background = '#10b981';
}

// ── Decision Display ────────────────────────────────────────
function updateDecision(d) {
    const recommendedMode = d.recommended_mode || d.mode;
    state.lastRecommendedMode = recommendedMode;
    
    // Store timestamps for live countdown
    state.decisionTime = Date.now();
    state.sessionRemaining = d.session_remaining;
    state.modeDuration = d.mode_duration;
    
    const card = document.getElementById('decision-card');
    const isOn = d.mode === 'RECIRCULATE';
    card.className = 'card decision-card ' + (isOn ? 'on' : 'off');

    const modeIcon = document.querySelector('#mode-icon-area i');
    if (modeIcon) modeIcon.className = `fas ${isOn ? 'fa-recycle' : 'fa-wind'}`;
    
    const modeName = document.getElementById('mode-name');
    if (modeName) modeName.textContent = isOn ? 'RECIRCULATION: ON' : 'FRESH AIR MODE';
    
    const subMode = document.getElementById('sub-mode');
    if (subMode) {
        const recommendedLabel = recommendedMode === 'RECIRCULATE' ? 'RECIRCULATE' : 'FRESH AIR';
        subMode.textContent = d.mode !== recommendedMode
            ? `${d.sub_mode} · System suggests ${recommendedLabel}`
            : d.sub_mode;
    }
    
    const prioEl = document.getElementById('decision-priority');
    if (prioEl) {
        prioEl.textContent = d.priority;
        prioEl.className = `priority-badge ${d.priority.toLowerCase()}`;
    }

    const confFill = document.getElementById('confidence-fill');
    const confText = document.getElementById('confidence-text');
    const confPct = Math.round(d.confidence * 100);
    if (confFill) confFill.style.width = confPct + '%';
    if (confText) confText.textContent = confPct + '%';

    // Update Comfort Index Gauge (Live)
    updateComfortGauge(d.comfort_index);

    const problematicSensors = extractProblematicSensors(d.reasons);
    
    // Display reasons with sensor linkage
    const reasons = document.getElementById('decision-reasons');
    if (reasons) {
        reasons.innerHTML = d.reasons.map((r, i) => {
            const linkedSensor = problematicSensors.find(s => r.toLowerCase().includes(s.name.toLowerCase()));
            const sensorBadge = linkedSensor ? `<span class="sensor-problem-badge" data-sensor="${linkedSensor.name}" style="${linkedSensor.color}">${linkedSensor.name.toUpperCase()}</span>` : '';
            
            return `<div class="reason-item animated" style="animation-delay: ${i * 0.1}s;">
                <div class="reason-content">
                    <i class="fas fa-check-circle reason-icon"></i>
                    <span class="reason-text">${r}</span>
                    ${sensorBadge}
                </div>
                <div class="reason-live-indicator">
                    <span class="live-dot"></span>
                    <span class="live-text">Triggered</span>
                </div>
            </div>`;
        }).join('');
    }

    // Display suggestions linked to problematic sensors and recommended action
    const suggestions = document.getElementById('decision-suggestions');
    if (suggestions) {
        const modeIcon = recommendedMode === 'RECIRCULATE' ? 'fa-recycle' : 'fa-wind';
        const modeName = recommendedMode === 'RECIRCULATE' ? 'Recirculate' : 'Fresh Air';
        const sensorLinks = problematicSensors.length > 0 ? problematicSensors.map(s => `<span class="linked-sensor" data-sensor="${s.name}" style="${s.color}">${s.name}</span>`).join('') : '';
        
        suggestions.innerHTML = d.suggestions.map((s, i) => {
            return `<div class="suggestion-item animated" style="animation-delay: ${i * 0.1}s;">
                <div class="suggestion-icon">
                    <i class="fas fa-lightbulb"></i>
                </div>
                <div class="suggestion-content">
                    <p class="suggestion-text">${s}</p>
                    <div class="suggestion-monitor">
                        <span class="monitor-status">Addresses: ${sensorLinks || 'Overall'}</span>
                        <span class="monitor-action"><i class="fas ${modeIcon}"></i> <strong>${modeName}</strong></span>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    const actionHintContainer = document.getElementById('action-hint-container');
    if (actionHintContainer) {
        actionHintContainer.innerHTML = getActionHint(d.mode, recommendedMode, problematicSensors);
    }

    highlightDecisionButtons(recommendedMode);
}

function extractProblematicSensors(reasons) {
    const sensorMap = {
        'PM2.5': { name: 'PM2.5', color: 'color: var(--accent-red);' },
        'AQI': { name: 'AQI', color: 'color: var(--accent-yellow);' },
        'CO2': { name: 'CO2', color: 'color: var(--accent-cyan);' },
        'Temperature': { name: 'Temperature', color: 'color: var(--accent-purple);' },
        'Humidity': { name: 'Humidity', color: 'color: var(--accent-blue);' }
    };
    
    const problematic = [];
    Object.keys(sensorMap).forEach(key => {
        const reason = reasons.find(r => r.toLowerCase().includes(key.toLowerCase()));
        if (reason) {
            problematic.push(sensorMap[key]);
        }
    });
    
    return problematic;
}

function getActionHint(currentMode, recommendedMode, problematicSensors = []) {
    const currentLabel = currentMode === 'RECIRCULATE' ? 'RECIRCULATE' : 'FRESH AIR';
    const modeIcon = recommendedMode === 'RECIRCULATE' ? 'fa-recycle' : 'fa-wind';
    const sensorList = problematicSensors.map(s => `<span class="badge-sensor">${s.name}</span>`).join('');
    
    if (recommendedMode !== currentMode) {
        const actionText = recommendedMode === 'RECIRCULATE'
            ? 'Turn on recirculation mode'
            : 'Turn on fresh air mode';

        return `<div class="action-hint-recommended">
            <div class="hint-badge"><i class="fas fa-bolt"></i> RECOMMENDED</div>
            <div class="hint-content">
                <i class="fas fa-clock"></i>
                <span class="directive-text"><strong>${actionText}</strong>${sensorList ? ` to address ${sensorList}.` : '.'}</span>
            </div>
            <div class="hint-action"><i class="fas ${modeIcon}"></i></div>
        </div>`;
    }
    
    return `<div class="action-hint-optimal">
        <i class="fas fa-check-circle"></i>
        <span class="directive-text">System is operating in <strong>optimal mode</strong> (${currentLabel}).</span>
    </div>`;
}

function highlightDecisionButtons(recommendedMode) {
    const btnRecirculate = document.getElementById('btn-recirculate-on');
    const btnFreshAir = document.getElementById('btn-fresh-air-on');
    const btnAuto = document.getElementById('btn-manual-clear');

    [btnRecirculate, btnFreshAir, btnAuto].forEach(btn => {
        btn?.classList.remove('active');
        btn?.classList.remove('recommended');
    });

    if (state.currentManualMode === 'RECIRCULATE') {
        btnRecirculate?.classList.add('active');
        return;
    }

    if (state.currentManualMode === 'FRESH_AIR') {
        btnFreshAir?.classList.add('active');
        return;
    }

    btnAuto?.classList.add('active');
    if (recommendedMode === 'RECIRCULATE') {
        btnRecirculate?.classList.add('recommended');
    } else if (recommendedMode === 'FRESH_AIR') {
        btnFreshAir?.classList.add('recommended');
    }
}

function updateManualModeUI() {
    const badgeValue = document.getElementById('badge-value');

    if (badgeValue) {
        badgeValue.className = 'badge-value';
        if (state.currentManualMode === 'RECIRCULATE') {
            badgeValue.textContent = 'MANUAL: RECIRCULATE';
            badgeValue.classList.add('override-mismatch');
        } else if (state.currentManualMode === 'FRESH_AIR') {
            badgeValue.textContent = 'MANUAL: FRESH AIR';
            badgeValue.classList.add('override-mismatch');
        } else {
            badgeValue.textContent = 'AUTO';
        }
    }

    highlightDecisionButtons(state.lastRecommendedMode);
}

async function setManualMode(mode) {
    try {
        const response = await fetch('/api/manual-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode })
        });

        const result = await response.json();
        if (!response.ok) {
            showToast(`Control error: ${result.error || 'Unknown error'}`, 'critical', 'fa-exclamation-triangle');
            return;
        }

        const modeChanged = state.currentManualMode !== mode;
        state.currentManualMode = mode;
        state.pendingHistoryDecision = modeChanged;
        updateManualModeUI();

        if (mode === 'RECIRCULATE') {
            showToast('Manual recirculation mode enabled', 'warning', 'fa-redo');
        } else if (mode === 'FRESH_AIR') {
            showToast('Manual fresh air mode enabled', 'warning', 'fa-wind');
        } else {
            showToast('Returned to auto mode', 'success', 'fa-robot');
        }
    } catch (error) {
        showToast(`Connection error: ${error.message}`, 'critical', 'fa-exclamation-triangle');
    }
}

function renderIndustryThresholds() {
    const container = document.getElementById('industry-thresholds');
    if (!container) return;

    container.innerHTML = INDUSTRY_BARRIERS.map(item => `
        <div class="threshold-row">
            <div class="threshold-sensor">${item.sensor}</div>
            <div class="threshold-bands">
                <span class="threshold-band safe">Safe: ${item.safe}</span>
                <span class="threshold-band warning">Warning: ${item.warning}</span>
                <span class="threshold-band critical">Critical: ${item.critical}</span>
            </div>
            <div class="threshold-source">Source: ${item.source}</div>
        </div>
    `).join('') + '<div class="threshold-note">Thresholds are reference barriers for driver guidance in typical conditions.</div>';
}

// ── Risk Gauge ──────────────────────────────────────────────
function updateGauge(score) {
    const maxArc = 251.2;
    document.getElementById('gauge-arc').setAttribute('stroke-dasharray', `${(score / 100) * maxArc} ${maxArc}`);
    const valEl = document.getElementById('gauge-value');
    valEl.textContent = Math.round(score);
    let fill = '#10b981';
    if (score > 70) fill = '#ef4444';
    else if (score > 45) fill = '#f97316';
    else if (score > 25) fill = '#f59e0b';
    valEl.style.color = fill;
}

// ── Comfort Index Gauge ──────────────────────────────────────
function updateComfortGauge(comfortIndex) {
    const maxArc = 251.2;
    const comfortArc = document.getElementById('comfort-arc');
    if (comfortArc) {
        comfortArc.setAttribute('stroke-dasharray', `${(comfortIndex / 100) * maxArc} ${maxArc}`);
    }
    const comfortVal = document.getElementById('comfort-value');
    if (comfortVal) {
        comfortVal.textContent = Math.round(comfortIndex);
        // Color: Red (poor) → Yellow (moderate) → Green (excellent)
        let fill = '#10b981'; // green
        if (comfortIndex < 30) fill = '#ef4444'; // red
        else if (comfortIndex < 60) fill = '#f59e0b'; // yellow
        comfortVal.style.color = fill;
    }
}

// ── Notifications Panel ─────────────────────────────────────

// ── Chart ───────────────────────────────────────────────────
function showChartFallback(message) {
    const container = document.querySelector('.chart-container');
    const canvas = document.getElementById('chart-main');

    if (canvas) {
        canvas.style.display = 'none';
    }

    if (!container || container.querySelector('.chart-fallback')) {
        return;
    }

    const fallback = document.createElement('div');
    fallback.className = 'chart-fallback';
    fallback.textContent = message;
    container.appendChild(fallback);
}

function initChart() {
    const canvas = document.getElementById('chart-main');
    if (!canvas) {
        console.warn('Chart canvas not found; skipping chart initialization.');
        return;
    }

    if (typeof Chart === 'undefined') {
        console.warn('Chart.js failed to load; continuing without the trend chart.');
        showChartFallback('Trend chart unavailable. Live sensor telemetry is still active.');
        return;
    }

    const ctx = canvas.getContext('2d');
    if (!ctx) {
        console.warn('Chart canvas context unavailable; skipping chart initialization.');
        showChartFallback('Trend chart unavailable. Live sensor telemetry is still active.');
        return;
    }

    state.chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                { label: 'AQI', data: [], borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.08)', fill: true, tension: 0.35, borderWidth: 2, pointRadius: 0 },
                { label: 'CO2 (÷10)', data: [], borderColor: '#8b5cf6', backgroundColor: 'transparent', tension: 0.35, borderWidth: 2, pointRadius: 0 },
                { label: 'PM2.5', data: [], borderColor: '#ef4444', backgroundColor: 'transparent', tension: 0.35, borderWidth: 2, pointRadius: 0 },
                { label: 'Temp', data: [], borderColor: '#f97316', backgroundColor: 'transparent', tension: 0.35, borderWidth: 2, pointRadius: 0 },
                { label: 'Humidity', data: [], borderColor: '#06b6d4', backgroundColor: 'transparent', tension: 0.35, borderWidth: 2, pointRadius: 0 },
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: { duration: 300 },
            scales: {
                x: { grid: { color: 'rgba(42,52,86,0.4)' }, ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 10 } } },
                y: { grid: { color: 'rgba(42,52,86,0.4)' }, ticks: { color: '#64748b', font: { size: 10 } } }
            },
            plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 }, usePointStyle: true, pointStyle: 'circle' } } }
        }
    });
}

function updateChart(s) {
    if (!state.chart) {
        return;
    }

    const cd = state.chartData;
    const t = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    cd.labels.push(t); cd.aqi.push(s.aqi); cd.co2.push(s.co2 / 10);
    cd.pm25.push(s.pm25); cd.temp.push(s.temperature); cd.hum.push(s.humidity);

    if (cd.labels.length > state.maxPoints) {
        cd.labels.shift(); cd.aqi.shift(); cd.co2.shift(); cd.pm25.shift(); cd.temp.shift(); cd.hum.shift();
    }

    const c = state.chart;
    c.data.labels = [...cd.labels];
    c.data.datasets[0].data = [...cd.aqi];
    c.data.datasets[1].data = [...cd.co2];
    c.data.datasets[2].data = [...cd.pm25];
    c.data.datasets[3].data = [...cd.temp];
    c.data.datasets[4].data = [...cd.hum];
    c.update('none');
}

// ── Decision Log ────────────────────────────────────────────
function addLogEntry(d) {
    const log = document.getElementById('decision-log');
    if (!log) return;

    const emptyState = log.querySelector('.log-empty');
    if (emptyState) {
        emptyState.remove();
    }

    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const entry = document.createElement('div');
    const modeClass = d.mode === 'RECIRCULATE' ? 'on' : 'off';
    const actionLabel = d.mode === 'RECIRCULATE' ? 'RECIRC' : 'FRESH';
    entry.className = `log-entry ${modeClass}`;

    let riskBg = 'rgba(16,185,129,0.15)', riskCol = '#10b981';
    if (d.risk_score > 70) { riskBg = 'rgba(239,68,68,0.15)'; riskCol = '#ef4444'; }
    else if (d.risk_score > 45) { riskBg = 'rgba(249,115,22,0.15)'; riskCol = '#f97316'; }
    else if (d.risk_score > 25) { riskBg = 'rgba(245,158,11,0.15)'; riskCol = '#f59e0b'; }

    entry.innerHTML = `
        <span class="log-time">${now}</span>
        <span class="log-action">${actionLabel}</span>
        <span class="log-reason">${d.reasons[0] || ''}</span>
        <span class="log-risk" style="background:${riskBg};color:${riskCol}">${Math.round(d.risk_score)}</span>
    `;
    log.prepend(entry);
    while (log.children.length > 80) log.removeChild(log.lastChild);
}

function maybeLogUserModeChange(decision) {
    if (!state.pendingHistoryDecision) {
        return;
    }

    addLogEntry(decision);
    state.pendingHistoryDecision = false;
}

// ── Toast Alerts ────────────────────────────────────────────
let lastToastTime = 0;
function fireToastAlerts(d) {
    const now = Date.now();
    if (now - lastToastTime < 5000) return;
    const primaryMessage = d.alerts?.[0]?.message || d.reasons?.[0] || 'Air quality event detected';
    if (d.priority === 'CRITICAL') {
        showToast(primaryMessage, 'critical', 'fa-triangle-exclamation');
        lastToastTime = now;
    } else if (d.priority === 'HIGH' && d.risk_score > 55) {
        showToast(primaryMessage, 'warning', 'fa-exclamation-circle');
        lastToastTime = now;
    }
}

function showToast(msg, type, icon) {
    const c = document.getElementById('toast-container');
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.innerHTML = `<i class="fas ${icon}"></i><span>${msg}</span>`;
    c.appendChild(t);
    setTimeout(() => { t.style.opacity='0'; t.style.transform='translateX(40px)'; t.style.transition='all 0.4s'; setTimeout(() => t.remove(), 400); }, 4500);
}

document.querySelectorAll('.slider').forEach(slider => {
    slider.addEventListener('input', () => {
        const valueTarget = document.getElementById(`sval-${slider.dataset.sensor}`);
        if (valueTarget) {
            valueTarget.textContent = slider.value;
        }
    });
});

document.querySelectorAll('.btn-apply').forEach(button => {
    button.addEventListener('click', async () => {
        const sensor = button.dataset.sensor;
        const slider = document.getElementById(`slider-${sensor}`);
        if (!slider) {
            return;
        }

        try {
            const response = await fetch('/api/sensor-override', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sensor, value: Number(slider.value) })
            });

            const result = await response.json();
            if (!response.ok) {
                showToast(`Override error: ${result.error || 'Unknown error'}`, 'critical', 'fa-exclamation-triangle');
                return;
            }

            showToast(`Applied ${sensor.toUpperCase()} override`, 'success', 'fa-sliders');
        } catch (error) {
            showToast(`Connection error: ${error.message}`, 'critical', 'fa-exclamation-triangle');
        }
    });
});

document.querySelectorAll('.btn-clear').forEach(button => {
    button.addEventListener('click', async () => {
        const sensor = button.dataset.sensor;

        try {
            const response = await fetch('/api/sensor-override', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sensor, value: null })
            });

            const result = await response.json();
            if (!response.ok) {
                showToast(`Override error: ${result.error || 'Unknown error'}`, 'critical', 'fa-exclamation-triangle');
                return;
            }

            showToast(`Cleared ${sensor.toUpperCase()} override`, 'success', 'fa-rotate-left');
        } catch (error) {
            showToast(`Connection error: ${error.message}`, 'critical', 'fa-exclamation-triangle');
        }
    });
});

document.getElementById('btn-reset-all')?.addEventListener('click', async () => {
    try {
        const response = await fetch('/api/clear-all-overrides', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const result = await response.json();
        if (!response.ok) {
            showToast(`Override error: ${result.error || 'Unknown error'}`, 'critical', 'fa-exclamation-triangle');
            return;
        }

        showToast('All sensor overrides cleared', 'success', 'fa-rotate-left');
    } catch (error) {
        showToast(`Connection error: ${error.message}`, 'critical', 'fa-exclamation-triangle');
    }
});

document.getElementById('btn-recirculate-on')?.addEventListener('click', () => setManualMode('RECIRCULATE'));
document.getElementById('btn-fresh-air-on')?.addEventListener('click', () => setManualMode('FRESH_AIR'));
document.getElementById('btn-manual-clear')?.addEventListener('click', () => setManualMode(null));

// ── Uptime ──────────────────────────────────────────────────
setInterval(() => {
    const s = Math.floor((Date.now() - state.startTime) / 1000);
    const h = String(Math.floor(s / 3600)).padStart(2, '0');
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(s % 60).padStart(2, '0');
    document.getElementById('uptime').textContent = `${h}:${m}:${sec}`;
}, 1000);

// ── Init ────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    try {
        initChart();
    } catch (error) {
        console.error('Chart initialization failed:', error);
        showChartFallback('Trend chart unavailable. Live sensor telemetry is still active.');
    }

    try {
        renderIndustryThresholds();
    } catch (error) {
        console.error('Failed to render industry thresholds:', error);
    }

    try {
        connectWebSocket();
    } catch (error) {
        console.error('WebSocket initialization failed:', error);
    }
    
    // Fallback: Force close loading overlay after 5 seconds if not auto-closed
    setTimeout(() => {
        const overlay = document.getElementById('connection-overlay');
        if (overlay && !overlay.classList.contains('hidden')) {
            console.log('Fallback: Forcing loading overlay close');
            updateLoadingOverlay(true);
        }
    }, 5000);
});
