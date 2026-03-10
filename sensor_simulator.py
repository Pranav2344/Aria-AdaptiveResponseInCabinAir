"""
Sensor Simulator — Industry-grade with CAN bus abstraction
──────────────────────────────────────────────────────────
Models realistic cabin physics:
  - CO2 accumulates in recirculation (~50 ppm/min/person)
  - CO2 decays toward ambient (~400 ppm) in fresh air mode
  - PM2.5 filtered in recirc (~30% reduction/min)
    - Cabin humidity drifts with occupants, HVAC, and outside exchange
  - Temperature drifts toward outside when fresh air is on
  - Sensor health monitoring (stuck, out-of-range, warm-up)

Provides CAN bus interface abstraction for real hardware integration.
"""

import time
import random
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict
from enum import Enum


class SensorHealth(Enum):
    OK = "ok"
    WARMING_UP = "warming_up"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass
class SensorReading:
    timestamp: float
    co2: int                  # ppm (inside cabin)
    aqi: int                  # 0-500 (outside air quality)
    pm25: float               # µg/m³ (outside)
    temperature: float        # °C (outside)
    humidity: float           # % (inside cabin)
    cabin_temp: float         # °C (inside cabin)
    cabin_pm25: float         # µg/m³ (inside cabin)
    cabin_humidity: float     # % (inside cabin)
    occupants: int            # passenger count
    speed: float              # km/h — affects fresh air intake volume
    sensor_health: Dict[str, str] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class CANBusInterface:
    """
    Abstraction layer for CAN bus / OBD-II sensor integration.
    In production: replace read() methods with actual CAN frame parsing.
    Protocol: SAE J1939 / ISO 15765-4
    """

    def read_co2(self) -> Optional[int]:
        """Read cabin CO2 from NDIR sensor (CAN ID: 0x18FEF100)."""
        return None  # Override in hardware implementation

    def read_aqi(self) -> Optional[int]:
        """Read outside AQI from intake sensor (CAN ID: 0x18FEF200)."""
        return None

    def read_pm25(self) -> Optional[float]:
        """Read PM2.5 from laser particle sensor (CAN ID: 0x18FEF201)."""
        return None

    def read_temperature(self) -> Optional[float]:
        """Read outside temp from ambient sensor (CAN ID: 0x18FEF300)."""
        return None

    def read_cabin_temp(self) -> Optional[float]:
        """Read cabin temp from HVAC sensor (CAN ID: 0x18FEF301)."""
        return None

    def read_humidity(self) -> Optional[float]:
        """Read humidity from cabin sensor (CAN ID: 0x18FEF400)."""
        return None

    def read_speed(self) -> Optional[float]:
        """Read vehicle speed (CAN ID: 0x0CF00400)."""
        return None

    def read_occupants(self) -> Optional[int]:
        """Read occupant count from seat pressure sensors."""
        return None

    def write_recirc_flap(self, position: int):
        """Command recirc flap: 0=open(fresh), 100=closed(recirc). CAN ID: 0x18FEF500."""
        pass


class SensorSimulator:
    """
    Realistic in-cabin sensor simulation with physics model.
    Designed as drop-in replacement for CANBusInterface in testing.
    """

    # Sensor valid ranges (ISO 16750 compliance)
    VALID_RANGES = {
        "co2": (300, 10000),
        "aqi": (0, 500),
        "pm25": (0, 1000),
        "temperature": (-40, 85),
        "humidity": (0, 100),
        "cabin_temp": (-40, 85),
    }

    def __init__(self, occupants: int = 2):
        self._start_time = time.time()

        # Cabin physics state
        self._cabin_co2 = 500.0        # ppm — starts near ambient
        self._cabin_temp = 24.0        # °C
        self._cabin_pm25 = 15.0        # µg/m³ inside cabin
        self._cabin_humidity = 48.0    # % RH inside cabin

        # Outside environment (drifts naturally)
        self._outside = {
            "aqi": 70.0,
            "pm25": 30.0,
            "temperature": 32.0,
            "humidity": 55.0,
        }
        self._env_targets = dict(self._outside)
        self._last_env_shift = time.time()

        # Vehicle state
        self._occupants = occupants
        self._speed = 40.0
        self._recirc_mode = False  # False = fresh air, True = recirculating

        # Sensor health tracking
        self._sensor_readings_count = 0
        self._sensor_stuck_check: Dict[str, list] = {
            "co2": [], "aqi": [], "pm25": [], "temperature": [], "humidity": []
        }

        # CAN bus interface (for future hardware)
        self._can_bus = CANBusInterface()

    def set_recirc_mode(self, recirc: bool):
        """Called by decision engine to update the recirc state for physics model."""
        self._recirc_mode = recirc

    def set_occupants(self, count: int):
        self._occupants = max(1, min(7, count))

    def _drift_environment(self):
        """Gradually shift outside conditions (simulates driving through areas)."""
        now = time.time()
        if now - self._last_env_shift > random.uniform(20, 45):
            self._env_targets = {
                "aqi": random.uniform(15, 300),
                "pm25": random.uniform(5, 180),
                "temperature": random.uniform(10, 45),
                "humidity": random.uniform(20, 85),
            }
            self._last_env_shift = now

        rate = 0.06
        for key in self._outside:
            diff = self._env_targets[key] - self._outside[key]
            self._outside[key] += diff * rate + random.gauss(0, abs(diff) * 0.01)

    def _update_cabin_physics(self, dt: float = 4.0):
        """
        Update cabin state based on physics model.
        dt: time step in seconds (matches sensor update interval).
        """
        dt_min = dt / 60.0  # convert to minutes

        # ── CO2 Model ──
        ambient_co2 = 415  # outdoor baseline ppm
        if self._recirc_mode:
            # CO2 rises: ~50 ppm/min per occupant (adult at rest)
            # Rate decreases slightly at very high levels (cabin pressure)
            rise_rate = 50 * self._occupants * max(0.3, 1 - self._cabin_co2 / 8000)
            self._cabin_co2 += rise_rate * dt_min
        else:
            # CO2 decays toward ambient — faster at higher speeds (more air volume)
            speed_factor = max(0.3, min(2.0, self._speed / 50))
            decay_rate = 0.15 * speed_factor  # fraction per minute
            self._cabin_co2 += (ambient_co2 - self._cabin_co2) * decay_rate * dt_min
            # Also gets some outside air pollution contribution
            self._cabin_co2 = max(ambient_co2, self._cabin_co2)

        # ── PM2.5 Model ──
        outside_pm25 = max(0, self._outside["pm25"])
        if self._recirc_mode:
            # Cabin filter removes ~30% per pass per minute
            self._cabin_pm25 *= (1 - 0.30 * dt_min)
            self._cabin_pm25 = max(2, self._cabin_pm25)  # can't go below 2
        else:
            # PM2.5 approaches outside level (cabin filter still helps ~40%)
            effective_outside = outside_pm25 * 0.6  # filter removes 40%
            self._cabin_pm25 += (effective_outside - self._cabin_pm25) * 0.2 * dt_min

        # ── Cabin Temperature Model ──
        hvac_target = 23.0  # HVAC set temperature
        outside_temp = self._outside["temperature"]
        if self._recirc_mode:
            # Cabin temp converges to HVAC target faster (no outside air interference)
            self._cabin_temp += (hvac_target - self._cabin_temp) * 0.08 * dt_min
        else:
            # Blended: HVAC tries to reach target but outside air pulls toward outside temp
            blend_target = hvac_target * 0.7 + outside_temp * 0.3
            self._cabin_temp += (blend_target - self._cabin_temp) * 0.06 * dt_min

        # ── Cabin Humidity Model ──
        outside_humidity = self._outside["humidity"]
        if self._recirc_mode:
            # Occupants add moisture; HVAC dehumidifies gradually.
            moisture_gain = 0.12 * self._occupants
            dehumidify = 0.10 * max(0.0, self._cabin_humidity - 45.0)
            self._cabin_humidity += moisture_gain * dt_min
            self._cabin_humidity -= dehumidify * dt_min
        else:
            # Fresh-air exchange pulls cabin RH toward outside RH with HVAC moderation.
            target_rh = outside_humidity * 0.75 + 45.0 * 0.25
            exchange_rate = 0.22 * max(0.4, min(1.8, self._speed / 60.0))
            self._cabin_humidity += (target_rh - self._cabin_humidity) * exchange_rate * dt_min

        self._cabin_humidity = max(10.0, min(95.0, self._cabin_humidity))

        # ── Speed variation ──
        self._speed += random.gauss(0, 3)
        self._speed = max(0, min(140, self._speed))

    def _check_sensor_health(self, sensor: str, value: float) -> str:
        """Check if a sensor reading is healthy (not stuck, not out of range)."""
        lo, hi = self.VALID_RANGES.get(sensor, (None, None))

        # Out of range check
        if lo is not None and (value < lo or value > hi):
            return SensorHealth.FAILED.value

        # Warm-up period (first 10 readings)
        if self._sensor_readings_count < 10:
            return SensorHealth.WARMING_UP.value

        # Stuck sensor check (same value for 10+ consecutive readings)
        history = self._sensor_stuck_check.get(sensor, [])
        history.append(round(value, 1))
        if len(history) > 12:
            history.pop(0)
        self._sensor_stuck_check[sensor] = history

        if len(history) >= 10 and len(set(history[-10:])) == 1:
            return SensorHealth.DEGRADED.value

        return SensorHealth.OK.value

    def get_reading(self) -> SensorReading:
        """Generate a complete sensor reading with physics simulation."""
        self._drift_environment()
        self._update_cabin_physics()
        self._sensor_readings_count += 1

        # Try CAN bus first (for real hardware), fall back to simulation
        co2 = self._can_bus.read_co2() or max(300, min(10000, int(self._cabin_co2)))
        aqi = self._can_bus.read_aqi() or max(0, min(500, int(self._outside["aqi"])))
        pm25 = self._can_bus.read_pm25() or round(max(0, self._outside["pm25"]), 1)
        temperature = self._can_bus.read_temperature() or round(self._outside["temperature"], 1)
        humidity = self._can_bus.read_humidity() or round(
            max(5, min(100, self._cabin_humidity)), 1
        )
        cabin_temp = self._can_bus.read_cabin_temp() or round(self._cabin_temp, 1)
        cabin_pm25 = round(max(0, self._cabin_pm25), 1)
        speed = self._can_bus.read_speed() or round(max(0, self._speed), 1)
        occupants = self._can_bus.read_occupants() or self._occupants

        # Add realistic sensor noise (±1-2% ADC noise)
        co2 = max(300, co2 + random.randint(-8, 8))
        aqi = max(0, min(500, aqi + random.randint(-3, 3)))
        pm25 = round(max(0, pm25 + random.gauss(0, max(0.3, pm25 * 0.02))), 1)
        cabin_pm25 = round(max(0, cabin_pm25 + random.gauss(0, max(0.2, cabin_pm25 * 0.025))), 1)
        humidity = round(max(5, min(100, humidity + random.gauss(0, 0.4))), 1)

        # Check sensor health
        sensor_health = {
            "co2": self._check_sensor_health("co2", co2),
            "aqi": self._check_sensor_health("aqi", aqi),
            "pm25": self._check_sensor_health("pm25", pm25),
            "temperature": self._check_sensor_health("temperature", temperature),
            "humidity": self._check_sensor_health("humidity", humidity),
        }

        return SensorReading(
            timestamp=time.time(),
            co2=co2, aqi=aqi, pm25=pm25,
            temperature=temperature, humidity=humidity,
            cabin_temp=cabin_temp,
            cabin_pm25=cabin_pm25,
            cabin_humidity=humidity,
            occupants=occupants,
            speed=speed, sensor_health=sensor_health,
        )
