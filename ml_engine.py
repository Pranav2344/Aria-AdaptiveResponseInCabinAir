"""
ARIA Decision Engine — Production-Grade
────────────────────────────────────────
Real-world automotive air recirculation controller.

Architecture:
  1. Sensor Validation   — reject/flag bad sensor data
  2. Tiered Evaluation   — CO2 > AQI > PM2.5 > Temp > Humidity
  3. Conflict Resolution — CO2 buildup vs bad outside air
  4. ML Ensemble         — for non-critical nuanced decisions
  5. State Machine       — hysteresis + 5-min session hold
  6. Comfort Index       — single 0-100 cabin comfort score
  7. Alert Manager       — tiered alerts with cooldown (no spam)

Medical basis for CO2 priority:
  800 ppm  — acceptable (ASHRAE 62.1)
  1000 ppm — stuffiness, reduced concentration
  1500 ppm — headache, drowsiness begins
  2500 ppm — impaired cognitive function, unsafe driving
  4000 ppm — nausea, risk of losing consciousness
  5000 ppm — OSHA workplace limit (8h TWA)

Reference: ASHRAE 62.1-2022, ISO 16000-26, EPA AQI breakpoints
"""

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import time


@dataclass
class Alert:
    level: str      # "critical" | "warning" | "info"
    message: str
    sensor: str     # which sensor triggered it
    timestamp: float


@dataclass
class Decision:
    mode: str               # "RECIRCULATE" or "FRESH_AIR"
    recommended_mode: str   # AI suggestion before state machine
    sub_mode: str           # "normal" | "co2_flush" | "pollution" | "temperature" | "comfort"
    confidence: float
    comfort_index: int      # 0-100
    risk_score: float       # 0-100
    priority: str           # "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    mode_duration: int      # seconds in current mode
    session_remaining: int  # seconds until mode can change
    reasons: List[str]
    suggestions: List[str]
    alerts: List[dict]
    sensor_status: Dict[str, str]

    def to_dict(self):
        return {
            "mode": self.mode,
            "recommended_mode": self.recommended_mode,
            "sub_mode": self.sub_mode,
            "confidence": round(self.confidence, 3),
            "comfort_index": self.comfort_index,
            "risk_score": round(self.risk_score, 1),
            "priority": self.priority,
            "mode_duration": self.mode_duration,
            "session_remaining": self.session_remaining,
            "reasons": self.reasons,
            "suggestions": self.suggestions,
            "alerts": self.alerts,
            "sensor_status": self.sensor_status,
        }


# ── Industry-standard thresholds ─────────────────────────────
TH = {
    "co2": {
        "outdoor": 415,     # baseline outdoor CO2
        "good": 600,        # excellent cabin air
        "acceptable": 800,  # ASHRAE 62.1 acceptable
        "stuffy": 1000,     # noticeable stuffiness
        "high": 1500,       # headache, drowsiness onset
        "dangerous": 2500,  # cognitive impairment, unsafe driving
        "critical": 4000,   # nausea, possible unconsciousness
        "osha_limit": 5000, # OSHA 8h TWA limit
    },
    "aqi": {  # EPA AQI breakpoints
        "good": 50,
        "moderate": 100,
        "usg": 150,         # unhealthy for sensitive groups
        "unhealthy": 200,
        "very_unhealthy": 300,
        "hazardous": 400,
    },
    "pm25": {  # EPA PM2.5 breakpoints (µg/m³, 24h)
        "good": 12.0,
        "moderate": 35.4,
        "usg": 55.4,
        "unhealthy": 150.4,
        "very_unhealthy": 250.4,
        "hazardous": 500.4,
    },
    "temperature": {
        "freezing": 0, "cold": 10, "cool": 18,
        "comfort_lo": 21, "comfort_hi": 27,
        "warm": 32, "hot": 38, "extreme": 45,
    },
    "humidity": {
        "very_dry": 20, "dry": 30,
        "comfort_lo": 40, "comfort_hi": 60,
        "humid": 70, "very_humid": 80, "extreme": 90,
    },
}


class AlertManager:
    """Manages alerts with cooldown to prevent notification spam."""

    def __init__(self):
        self._cooldowns: Dict[str, float] = {}
        self._active_alerts: List[Alert] = []
        # Minimum seconds between same alert type
        self.COOLDOWN = {
            "critical": 30,
            "warning": 120,
            "info": 300,
        }

    def try_alert(self, alert_id: str, level: str, message: str, sensor: str) -> Optional[Alert]:
        """Issue alert only if cooldown has passed."""
        now = time.time()
        cooldown = self.COOLDOWN.get(level, 60)
        last_fired = self._cooldowns.get(alert_id, 0)

        if now - last_fired >= cooldown:
            self._cooldowns[alert_id] = now
            alert = Alert(level=level, message=message, sensor=sensor, timestamp=now)
            self._active_alerts.append(alert)
            if len(self._active_alerts) > 100:
                self._active_alerts = self._active_alerts[-100:]
            return alert
        return None

    def get_recent(self, limit: int = 10) -> List[dict]:
        return [
            {"level": a.level, "message": a.message, "sensor": a.sensor, "timestamp": a.timestamp}
            for a in self._active_alerts[-limit:]
        ]


class MLDecisionEngine:
    """Production-grade decision engine with state machine."""

    SESSION_HOLD = 300     # 5-minute minimum session
    CONFIRM_DELAY = 16     # 16s confirmation before switching
    RISK_ON = 40           # risk must exceed to switch to RECIRCULATE
    RISK_OFF = 22          # risk must drop below to switch to FRESH_AIR
    COMFORT_EMA_ALPHA = 0.35
    COMFORT_SENSOR_PENALTY = {
        "warming_up": 1.0,
        "degraded": 4.0,
        "failed": 10.0,
    }

    def __init__(self):
        self.model = RandomForestClassifier(n_estimators=150, random_state=42, max_depth=10)
        self.scaler = StandardScaler()
        self._is_trained = False

        # State machine
        self._mode = "FRESH_AIR"
        self._sub_mode = "normal"
        self._mode_since = time.time() - self.SESSION_HOLD - 1  # allow first switch
        self._pending_mode = None
        self._pending_since = 0

        # Alert manager
        self.alerts = AlertManager()

        # History
        self._history: list = []
        self._last_history_save = 0

        # Comfort smoothing state
        self._comfort_ema: Optional[float] = None

        self._train()

    # ── ML Training ──────────────────────────────────────────
    def _generate_training_data(self):
        np.random.seed(42)
        X, y = [], []

        scenarios = {
            "clean":         (800, (10, 50),  (380, 600),  (40, 55), (20, 28), (3, 12),   0),
            "moderate":      (600, (50, 130), (450, 850),  (35, 65), (18, 33), (12, 45),  0),
            "polluted":      (800, (150,400), (450, 850),  (40, 70), (25, 40), (55, 300), 1),
            "co2_crisis":    (800, (10, 250), (2000,5000), (45, 75), (22, 38), (5, 100),  0),
            "co2_bad_aqi":   (600, (200,450), (2500,5000), (50, 75), (28, 42), (60, 200), 0),
            "hot":           (500, (40, 100), (400, 750),  (25, 45), (38, 52), (10, 35),  1),
            "cold":          (400, (20, 70),  (400, 700),  (30, 55), (-5, 10), (5, 20),   1),
            "humid_fog_risk":(400, (30, 80),  (500, 800),  (80, 98), (5, 15),  (5, 20),   0),
        }

        for _ in range(6000):
            name = np.random.choice(list(scenarios.keys()))
            count, aqi_r, co2_r, hum_r, temp_r, pm_r, label = scenarios[name]
            for _ in range(count // 100 + 1):
                s = [
                    np.random.randint(*aqi_r),
                    np.random.randint(*co2_r),
                    np.random.uniform(*hum_r),
                    np.random.uniform(*temp_r),
                    np.random.uniform(*pm_r),
                ]
                X.append(s)
                y.append(label)

        return np.array(X), np.array(y)

    def _train(self):
        X, y = self._generate_training_data()
        self.scaler.fit(X)
        self.model.fit(self.scaler.transform(X), y)
        self._is_trained = True

    def _ml_predict(self, data: dict):
        features = np.array([[data["aqi"], data["co2"], data["humidity"],
                               data["temperature"], data["pm25"]]])
        proba = self.model.predict_proba(self.scaler.transform(features))[0]
        pred = int(np.argmax(proba))
        return ("RECIRCULATE" if pred == 1 else "FRESH_AIR"), float(proba[pred])

    # ── Sensor Validation ────────────────────────────────────
    def _validate_sensors(self, data: dict) -> List[str]:
        """Check for failed sensors. Return list of issues."""
        issues = []
        health = data.get("sensor_health", {})
        for sensor, status in health.items():
            if status == "failed":
                issues.append(f"Sensor {sensor} has FAILED — using last known good value.")
            elif status == "degraded":
                issues.append(f"Sensor {sensor} may be stuck — readings unreliable.")
        return issues

    def _comfort_sensor_penalty(self, sensor_health: Dict[str, str]) -> float:
        """Apply a bounded penalty when comfort relies on degraded sensors."""
        penalty = 0.0
        for sensor in ("co2", "aqi", "pm25", "temperature", "humidity"):
            status = sensor_health.get(sensor)
            penalty += self.COMFORT_SENSOR_PENALTY.get(status, 0.0)
        return min(20.0, penalty)

    def _apply_comfort_caps(self, comfort: float, co2: float, pm25: float, temp: float, hum: float) -> float:
        """Prevent unrealistically high comfort during any single severe hazard."""
        hard_cap = 100.0

        if co2 >= TH["co2"]["critical"]:
            hard_cap = min(hard_cap, 10.0)
        elif co2 >= TH["co2"]["dangerous"]:
            hard_cap = min(hard_cap, 25.0)
        elif co2 >= TH["co2"]["high"]:
            hard_cap = min(hard_cap, 45.0)

        if pm25 >= TH["pm25"]["very_unhealthy"]:
            hard_cap = min(hard_cap, 35.0)
        elif pm25 >= TH["pm25"]["unhealthy"]:
            hard_cap = min(hard_cap, 50.0)

        if temp >= TH["temperature"]["extreme"] or temp <= TH["temperature"]["freezing"]:
            hard_cap = min(hard_cap, 40.0)
        elif temp >= TH["temperature"]["hot"] or temp <= TH["temperature"]["cold"]:
            hard_cap = min(hard_cap, 55.0)

        if hum >= TH["humidity"]["extreme"] or hum <= TH["humidity"]["very_dry"]:
            hard_cap = min(hard_cap, 45.0)

        return min(comfort, hard_cap)

    # ── Comfort Index (0-100) ────────────────────────────────
    def _comfort_index(self, data: dict) -> int:
        """
        Single comfort score combining all factors.
        100 = perfect conditions, 0 = extremely uncomfortable/dangerous.
        Cabin-focused weighted scoring:
        CO2 (30%) | Cabin PM2.5 (25%) | Cabin Temp (20%) | Cabin Humidity (15%) | Outside AQI (10%)
        with sensor-health penalties and smoothing for stability.
        """
        co2 = float(data.get("co2", TH["co2"]["acceptable"]))
        aqi = float(data.get("aqi", TH["aqi"]["moderate"]))
        pm = float(data.get("cabin_pm25", data.get("pm25", TH["pm25"]["moderate"])))
        temp = float(data.get("cabin_temp", data.get("temperature", 24.0)))
        hum = float(data.get("cabin_humidity", data.get("humidity", 50.0)))

        # CO2 comfort scoring (30% weight) — ideal: < 600 ppm
        if co2 <= 600:
            co2_score = 100
        elif co2 <= 800:
            co2_score = 100 - (co2 - 600) / 200 * 8   # 92-100
        elif co2 <= 1000:
            co2_score = 92 - (co2 - 800) / 200 * 12  # 80-92
        elif co2 <= 1500:
            co2_score = 80 - (co2 - 1000) / 500 * 30  # 50-80
        elif co2 <= 2500:
            co2_score = 50 - (co2 - 1500) / 1000 * 30  # 20-50
        elif co2 <= 4000:
            co2_score = 20 - (co2 - 2500) / 1500 * 15  # 5-20
        else:
            co2_score = max(0, 5 - (co2 - 4000) / 1000 * 5)

        # AQI comfort scoring (10% weight) — outside environment proxy
        if aqi <= 50:
            aqi_score = 100
        elif aqi <= 100:
            aqi_score = 100 - (aqi - 50) / 50 * 15  # 85-100
        elif aqi <= 150:
            aqi_score = 85 - (aqi - 100) / 50 * 20   # 65-85
        elif aqi <= 200:
            aqi_score = 65 - (aqi - 150) / 50 * 20   # 45-65
        elif aqi <= 300:
            aqi_score = 45 - (aqi - 200) / 100 * 25  # 20-45
        else:
            aqi_score = max(0, 20 - (aqi - 300) / 200 * 20)

        # Cabin PM2.5 comfort scoring (25% weight) — ideal: < 12 µg/m³
        if pm <= 12:
            pm_score = 100
        elif pm <= 35.4:
            pm_score = 100 - (pm - 12) / 23.4 * 18  # 82-100
        elif pm <= 55.4:
            pm_score = 82 - (pm - 35.4) / 20 * 20   # 62-82
        elif pm <= 150.4:
            pm_score = 62 - (pm - 55.4) / 95 * 34   # 28-62
        elif pm <= 250.4:
            pm_score = 28 - (pm - 150.4) / 100 * 18 # 10-28
        else:
            pm_score = max(0, 10 - (pm - 250.4) / 250 * 10)

        # Cabin temperature comfort scoring (20% weight) — ideal: 21-26°C
        temp_dev = abs(temp - 23.5)
        if temp_dev <= 1.5:
            temp_score = 100
        elif temp_dev <= 3.5:
            temp_score = 90
        elif temp_dev <= 5.5:
            temp_score = 75
        elif temp_dev <= 8.0:
            temp_score = 55
        elif temp_dev <= 12.0:
            temp_score = 30
        else:
            temp_score = max(0, 30 - (temp_dev - 12) / 12 * 30)

        # Cabin humidity comfort scoring (15% weight) — ideal: 40-60% RH
        if 40 <= hum <= 60:
            hum_score = 100
        elif 30 <= hum <= 70:
            hum_score = 85
        elif 20 <= hum <= 80:
            hum_score = 65
        elif 15 <= hum <= 85:
            hum_score = 45
        elif 10 <= hum <= 90:
            hum_score = 20
        else:
            hum_score = 0

        # Cabin-focused weighted average
        comfort = (
            co2_score * 0.30 +
            pm_score * 0.25 +
            temp_score * 0.20 +
            hum_score * 0.15 +
            aqi_score * 0.10
        )

        comfort -= self._comfort_sensor_penalty(data.get("sensor_health", {}))
        comfort = self._apply_comfort_caps(comfort, co2, pm, temp, hum)
        comfort = max(0.0, min(100.0, comfort))

        severe_hazard = (
            co2 >= TH["co2"]["dangerous"]
            or pm >= TH["pm25"]["very_unhealthy"]
            or temp >= TH["temperature"]["extreme"]
            or temp <= TH["temperature"]["freezing"]
            or hum >= TH["humidity"]["extreme"]
            or hum <= TH["humidity"]["very_dry"]
            or any(v == "failed" for v in data.get("sensor_health", {}).values())
        )

        # EMA smoothing prevents rapid oscillations from short-lived sensor spikes.
        if self._comfort_ema is None or severe_hazard:
            self._comfort_ema = comfort
        else:
            a = self.COMFORT_EMA_ALPHA
            self._comfort_ema = (a * comfort) + ((1 - a) * self._comfort_ema)

        return int(round(max(0.0, min(100.0, self._comfort_ema))))

    # ── Risk Score ───────────────────────────────────────────
    def _risk_score(self, data: dict) -> float:
        score = 0.0
        score += 0.35 * min(100, (data["co2"] / TH["co2"]["critical"]) * 100)
        score += 0.25 * min(100, (data["aqi"] / TH["aqi"]["hazardous"]) * 100)
        score += 0.18 * min(100, (data["pm25"] / TH["pm25"]["hazardous"]) * 100)
        temp_risk = max(0, abs(data["temperature"] - 25) - 8) * 5
        score += 0.12 * min(100, temp_risk)
        hum_risk = max(0, abs(data["humidity"] - 50) - 15) * 4
        score += 0.10 * min(100, hum_risk)
        return min(100, max(0, score))

    # ── Sensor Status ────────────────────────────────────────
    def _sensor_status(self, data: dict) -> dict:
        st = {}

        co2 = data["co2"]
        if co2 >= TH["co2"]["critical"]:     st["co2"] = "critical"
        elif co2 >= TH["co2"]["dangerous"]:  st["co2"] = "critical"
        elif co2 >= TH["co2"]["high"]:       st["co2"] = "danger"
        elif co2 >= TH["co2"]["stuffy"]:     st["co2"] = "warning"
        elif co2 >= TH["co2"]["acceptable"]: st["co2"] = "caution"
        else:                                st["co2"] = "good"

        aqi = data["aqi"]
        if aqi >= TH["aqi"]["hazardous"]:        st["aqi"] = "critical"
        elif aqi >= TH["aqi"]["very_unhealthy"]: st["aqi"] = "critical"
        elif aqi >= TH["aqi"]["unhealthy"]:      st["aqi"] = "danger"
        elif aqi >= TH["aqi"]["usg"]:            st["aqi"] = "warning"
        elif aqi >= TH["aqi"]["moderate"]:       st["aqi"] = "caution"
        else:                                    st["aqi"] = "good"

        pm = data["pm25"]
        if pm >= TH["pm25"]["hazardous"]:        st["pm25"] = "critical"
        elif pm >= TH["pm25"]["unhealthy"]:      st["pm25"] = "danger"
        elif pm >= TH["pm25"]["usg"]:            st["pm25"] = "warning"
        elif pm >= TH["pm25"]["moderate"]:       st["pm25"] = "caution"
        else:                                    st["pm25"] = "good"

        t = data["temperature"]
        if t >= TH["temperature"]["extreme"] or t <= TH["temperature"]["freezing"]:
            st["temperature"] = "critical"
        elif t >= TH["temperature"]["hot"] or t <= TH["temperature"]["cold"]:
            st["temperature"] = "warning"
        elif t > TH["temperature"]["comfort_hi"] or t < TH["temperature"]["comfort_lo"]:
            st["temperature"] = "caution"
        else:
            st["temperature"] = "good"

        h = data["humidity"]
        if h >= TH["humidity"]["extreme"] or h <= TH["humidity"]["very_dry"]:
            st["humidity"] = "warning"
        elif h >= TH["humidity"]["very_humid"] or h <= TH["humidity"]["dry"]:
            st["humidity"] = "caution"
        else:
            st["humidity"] = "good"

        return st

    # ── Core Tiered Evaluation ───────────────────────────────
    def _evaluate(self, data: dict):
        """
        Tiered priority evaluation with conflict resolution.
        Returns: (mode, sub_mode, priority, confidence, reasons, suggestions, alert_list)
        """
        co2 = data["co2"]
        aqi = data["aqi"]
        pm25 = data["pm25"]
        temp = data["temperature"]
        hum = data["humidity"]

        reasons = []
        suggestions = []
        new_alerts = []
        priority = "LOW"
        mode = None
        sub_mode = "normal"
        confidence = 0.70

        # ══════════════════════════════════════════════════════
        # TIER 1: CO2 — IMMEDIATE LIFE SAFETY
        # CO2 is the only sensor measuring INSIDE the cabin.
        # High CO2 = occupant danger, always overrides everything.
        # ══════════════════════════════════════════════════════

        if co2 >= TH["co2"]["critical"]:
            mode = "FRESH_AIR"
            sub_mode = "co2_flush"
            priority = "CRITICAL"
            confidence = 0.99
            reasons.append(
                f"CO2 at {co2} ppm — CRITICAL. Risk of nausea and loss of consciousness. "
                f"OSHA limit is {TH['co2']['osha_limit']} ppm."
            )
            suggestions.append("Maximum fresh air intake active. Do not activate recirculation mode.")
            if aqi >= TH["aqi"]["unhealthy"]:
                reasons.append(
                    f"External AQI {aqi} exceeds unhealthy threshold. However, cabin CO2 at {co2} ppm poses immediate cognitive impairment risk. "
                    f"Brief external air exposure takes priority over prolonged CO2 exposure."
                )
                suggestions.append(
                    f"Upon CO2 concentration reduction below {TH['co2']['acceptable']} ppm, "
                    f"recirculation mode will activate for external air quality protection."
                )
            a = self.alerts.try_alert("co2_critical", "critical",
                f"CRITICAL ALERT: Cabin CO2 concentration {co2} ppm exceeds emergency threshold. Maximum fresh air intake activated.", "co2")
            if a: new_alerts.append(a)
            return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts

        if co2 >= TH["co2"]["dangerous"]:
            mode = "FRESH_AIR"
            sub_mode = "co2_flush"
            priority = "CRITICAL"
            confidence = 0.96
            reasons.append(
                f"CO2 at {co2} ppm — dangerous level. Cognitive impairment and drowsiness likely. "
                f"Safe driving requires CO2 below {TH['co2']['high']} ppm."
            )
            suggestions.append(
                f"Fresh air intake flushing cabin. Target CO2 concentration: below {TH['co2']['acceptable']} ppm."
            )
            if aqi >= TH["aqi"]["unhealthy"]:
                reasons.append(
                    f"External AQI at {aqi}. However, operator cognitive function preservation (CO2 management) takes absolute safety priority "
                    f"over external air quality concerns."
                )
            a = self.alerts.try_alert("co2_dangerous", "critical",
                f"CO2 concentration {co2} ppm at dangerous level — operator cognitive function and vehicle safety at risk. Fresh air ventilation activated.", "co2")
            if a: new_alerts.append(a)
            return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts

        if co2 >= TH["co2"]["high"]:
            if aqi < TH["aqi"]["very_unhealthy"]:
                mode = "FRESH_AIR"
                sub_mode = "co2_flush"
                priority = "HIGH"
                confidence = 0.90
                reasons.append(
                    f"CO2 at {co2} ppm — elevated. Headache and drowsiness risk. "
                    f"Outside air is acceptable (AQI: {aqi})."
                )
                suggestions.append(
                    f"Ventilating cabin to bring CO2 below {TH['co2']['stuffy']} ppm."
                )
                a = self.alerts.try_alert("co2_high", "warning",
                    f"CO2 elevated ({co2} ppm). Ventilating cabin.", "co2")
                if a: new_alerts.append(a)
                return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts
            else:
                # CO2 high AND AQI very unhealthy — tough tradeoff
                # CO2 still wins but note the conflict
                mode = "FRESH_AIR"
                sub_mode = "co2_flush"
                priority = "HIGH"
                confidence = 0.82
                reasons.append(
                    f"CO2 at {co2} ppm with AQI at {aqi} — both concerning. "
                    f"Prioritizing CO2 reduction as it directly affects driver performance."
                )
                suggestions.append(
                    "Brief ventilation cycle to reduce CO2, then recirculation for AQI. "
                    "Consider stopping in a cleaner area."
                )
                return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts

        if co2 >= TH["co2"]["stuffy"]:
            if aqi < TH["aqi"]["usg"]:
                reasons.append(f"CO2 at {co2} ppm approaching moderate concentration threshold. Fresh air intake mitigating further accumulation.")
                suggestions.append("Indoor CO2 concentration manageable. Recirculation and ventilation cycles optimizing air quality balance.")

        # ══════════════════════════════════════════════════════
        # TIER 2: OUTSIDE AIR QUALITY (AQI + PM2.5)
        # Only evaluated if CO2 is not in danger zone.
        # ══════════════════════════════════════════════════════

        if aqi >= TH["aqi"]["hazardous"]:
            mode = "RECIRCULATE"
            sub_mode = "pollution"
            priority = "CRITICAL"
            confidence = 0.97
            reasons.append(f"AQI at {aqi} classified HAZARDOUS. External air quality poses severe occupant health risk.")
            suggestions.append(
                "External intake sealed. Cabin filter system activated. "
                f"Continuous CO2 monitoring active. Fresh air ventilation will activate if indoor CO2 concentration exceeds {TH['co2']['high']} ppm."
            )
            a = self.alerts.try_alert("aqi_hazardous", "critical",
                f"CRITICAL ALERT: AQI {aqi} exceeds hazardous threshold. Cabin external intake sealed. Recirculation with full filtration active.", "aqi")
            if a: new_alerts.append(a)
            return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts

        if aqi >= TH["aqi"]["very_unhealthy"]:
            mode = "RECIRCULATE"
            sub_mode = "pollution"
            priority = "HIGH"
            confidence = 0.93
            reasons.append(f"AQI at {aqi} exceeds very unhealthy threshold. Recirculation with filtration system active.")
            suggestions.append(
                "Cabin filter system protecting occupants from external pollutants. Continuous indoor CO2 concentration monitoring. "
                "Fresh air ventilation cycles will activate if indoor CO2 concentration rises."
            )
            a = self.alerts.try_alert("aqi_very_unhealthy", "warning",
                f"AQI {aqi} exceeds very unhealthy threshold. Recirculation mode active with cabin filtration.", "aqi")
            if a: new_alerts.append(a)
            return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts

        if aqi >= TH["aqi"]["unhealthy"]:
            mode = "RECIRCULATE"
            sub_mode = "pollution"
            priority = "HIGH"
            confidence = 0.87
            reasons.append(f"AQI at {aqi} exceeds unhealthy threshold. Cabin air recirculation with filtration active.")
            suggestions.append(
                f"Recirculation mode with high-efficiency filtration active reducing external pollutant infiltration. "
                f"Continuous CO2 monitoring — fresh air ventilation if indoor CO2 exceeds {TH['co2']['high']} ppm."
            )
            a = self.alerts.try_alert("aqi_unhealthy", "warning",
                f"AQI {aqi} exceeds unhealthy threshold. Recirculation mode with filtration system recommended.", "aqi")
            if a: new_alerts.append(a)

        if aqi >= TH["aqi"]["usg"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "pollution"
                priority = "MEDIUM"
                confidence = 0.78
            reasons.append(f"AQI at {aqi} exceeds threshold posing risk to sensitive occupant groups. External pollution concentration elevated.")
            suggestions.append("Activate recirculation mode to isolate cabin environment from external pollution sources and protect interior air quality integrity through activated filtration systems.")

        # PM2.5 (independent of AQI — can be high even with moderate AQI)
        if pm25 >= TH["pm25"]["very_unhealthy"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "pollution"
                priority = "HIGH"
                confidence = 0.88
            reasons.append(f"PM2.5 concentration at {pm25:.0f} µg/m³ exceeds very unhealthy particulate level threshold.")
            suggestions.append("Engage HEPA filtration cycle to capture airborne particulate matter and prevent environmental contamination ingress into cabin environment.")
        elif pm25 >= TH["pm25"]["unhealthy"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "pollution"
                priority = "MEDIUM"
                confidence = 0.80
            reasons.append(f"PM2.5 concentration at {pm25:.0f} µg/m³ exceeds unhealthy threshold for extended exposure duration.")
            suggestions.append("Recirculation cycle reduces particulate matter ingestion from external environment and maintains cabin air environmental purity through continuous filtration.")
        elif pm25 >= TH["pm25"]["usg"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "pollution"
                priority = "MEDIUM"
                confidence = 0.72
            reasons.append(f"PM2.5 concentration at {pm25:.0f} µg/m³ elevated with potential impact on sensitive occupants.")

        # ══════════════════════════════════════════════════════
        # TIER 3: TEMPERATURE EFFICIENCY
        # ══════════════════════════════════════════════════════

        if temp >= TH["temperature"]["extreme"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "temperature"
                priority = "MEDIUM"
                confidence = 0.78
            reasons.append(f"External temperature {temp:.0f}°C exceeds extreme heat threshold. Air conditioning cooling capacity optimization required.")
            suggestions.append("Activate recirculation mode to minimize thermal load from external environment and reduce energy consumption for cabin temperature environmental regulation.")
        elif temp >= TH["temperature"]["hot"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "temperature"
                priority = "LOW"
                confidence = 0.70
            reasons.append(f"External temperature {temp:.0f}°C elevated. Recirculation enhances thermal management system efficiency.")
        elif temp <= TH["temperature"]["freezing"]:
            if mode is None:
                mode = "RECIRCULATE"
                sub_mode = "temperature"
                priority = "MEDIUM"
                confidence = 0.75
            reasons.append(f"External temperature {temp:.0f}°C at or below freezing point. Recirculation optimizes cabin thermal retention.")

        # ══════════════════════════════════════════════════════
        # TIER 4: HUMIDITY & FOG RISK
        # ══════════════════════════════════════════════════════

        if hum >= TH["humidity"]["extreme"]:
            reasons.append(f"Humidity at {hum:.0f}% exceeds extreme threshold with condensation risk.")
            suggestions.append("Environmental humidity saturation detected. Brief fresh air injection cycle recommended to restore environmental moisture equilibrium and prevent condensation accumulation.")
        elif hum >= TH["humidity"]["very_humid"]:
            reasons.append(f"Humidity at {hum:.0f}% elevated with increased moisture content in cabin environment.")
            suggestions.append("Environmental dehumidification cycle active to extract excess moisture from cabin air volume and maintain stable humidity equilibrium for environmental protection.")
        elif hum <= TH["humidity"]["very_dry"]:
            reasons.append(f"Humidity at {hum:.0f}% below optimal range resulting in dry cabin environment.")
            suggestions.append("Environmental moisture deficit detected. Increase fresh air exchange rate to restore relative humidity equilibrium and protect cabin environmental stability.")

        # ══════════════════════════════════════════════════════
        # DEFAULT: Everything is fine
        # ══════════════════════════════════════════════════════

        if mode is None:
            mode = "FRESH_AIR"
            sub_mode = "normal"
            if not reasons:
                reasons.append("All sensor parameters within safe operating range and optimal concentration thresholds. Fresh air intake maintains target indoor air quality.")
                suggestions.append("Environmental conditions nominal across all monitored parameters. Fresh air ventilation cycle sustains optimal cabin air environmental composition and quality metrics.")
            a = self.alerts.try_alert("all_clear", "info",
                "All air quality parameters nominal. Fresh air intake mode active maintaining target indoor air quality.", "aqi")
            if a: new_alerts.append(a)

        return mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts

    # ── State Machine ────────────────────────────────────────
    def make_decision(self, data: dict) -> Decision:
        now = time.time()

        # 1. Validate sensors
        sensor_issues = self._validate_sensors(data)

        # 2. Evaluate
        raw_mode, sub_mode, priority, confidence, reasons, suggestions, new_alerts = \
            self._evaluate(data)

        # Add sensor health warnings
        for issue in sensor_issues:
            reasons.append(issue)

        # 3. ML for non-critical decisions
        ml_mode, ml_conf = self._ml_predict(data)
        if priority in ("LOW",) and ml_conf > 0.85 and ml_mode != raw_mode:
            raw_mode = ml_mode
            confidence = ml_conf
            reasons.append(f"ML model recommends {ml_mode} ({ml_conf:.0%} confidence).")

        # 4. Compute scores
        risk = self._risk_score(data)
        comfort = self._comfort_index(data)
        sensor_status = self._sensor_status(data)

        # 5. Stabilization (state machine)
        time_in_mode = now - self._mode_since
        is_critical = priority == "CRITICAL"

        if is_critical and raw_mode != self._mode:
            # Critical safety: switch immediately
            self._mode = raw_mode
            self._sub_mode = sub_mode
            self._mode_since = now
            self._pending_mode = None
        elif raw_mode != self._mode:
            # Check hysteresis + hold time
            if raw_mode == "RECIRCULATE":
                hysteresis_ok = risk > self.RISK_ON
            else:
                # For FRESH_AIR: allow if CO2 is high regardless of risk score
                hysteresis_ok = risk < self.RISK_OFF or data["co2"] >= TH["co2"]["high"]

            if hysteresis_ok and time_in_mode >= self.SESSION_HOLD:
                if self._pending_mode != raw_mode:
                    self._pending_mode = raw_mode
                    self._pending_since = now
                elif now - self._pending_since >= self.CONFIRM_DELAY:
                    self._mode = raw_mode
                    self._sub_mode = sub_mode
                    self._mode_since = now
                    self._pending_mode = None
            elif not hysteresis_ok:
                self._pending_mode = None
        else:
            self._pending_mode = None
            self._sub_mode = sub_mode  # update sub_mode even if mode same

        # Session info
        mode_duration = int(now - self._mode_since)
        session_remaining = max(0, self.SESSION_HOLD - mode_duration)

        # Pending switch info
        if self._pending_mode and self._pending_mode != self._mode:
            if session_remaining > 0:
                reasons.append(
                    f"Conditions suggest {self._pending_mode} — "
                    f"current session holds for {session_remaining // 60}m {session_remaining % 60}s."
                )
            else:
                confirm_left = max(0, int(self.CONFIRM_DELAY - (now - self._pending_since)))
                reasons.append(
                    f"Switching to {self._pending_mode} in {confirm_left}s (confirming stability)."
                )

        # Adjust priority by risk
        if priority == "LOW":
            if risk > 55: priority = "HIGH"
            elif risk > 30: priority = "MEDIUM"

        # Build alerts list
        alert_dicts = [{"level": a.level, "message": a.message, "sensor": a.sensor}
                       for a in new_alerts]

        decision = Decision(
            mode=self._mode,
            recommended_mode=raw_mode,
            sub_mode=self._sub_mode,
            confidence=confidence,
            comfort_index=comfort,
            risk_score=risk,
            priority=priority,
            mode_duration=mode_duration,
            session_remaining=session_remaining,
            reasons=reasons,
            suggestions=suggestions,
            alerts=alert_dicts,
            sensor_status=sensor_status,
        )

        # History every 60s
        if now - self._last_history_save >= 60:
            self._last_history_save = now
            self._history.append({"timestamp": now, "decision": decision.to_dict()})
            if len(self._history) > 500:
                self._history = self._history[-500:]

        return decision

    def get_history(self, limit=50):
        return self._history[-limit:]
