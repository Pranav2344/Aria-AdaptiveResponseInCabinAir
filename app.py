"""ARIA — Adaptive Recirculation Intelligence Agent."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from sensor_simulator import SensorSimulator
from ml_engine import MLDecisionEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

LOGGER = logging.getLogger("aria")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
UPDATE_INTERVAL_SECONDS = float(os.getenv("SENSOR_UPDATE_INTERVAL", "4"))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

simulator = SensorSimulator(occupants=2)
engine = MLDecisionEngine()
active_connections: set[WebSocket] = set()
manual_mode_override: str | None = None
sensor_overrides: dict[str, float] = {}


def apply_sensor_overrides(data: dict) -> dict:
    """Apply user-provided sensor overrides to a reading payload."""
    overridden = dict(data)
    for sensor, value in sensor_overrides.items():
        if sensor not in overridden:
            continue
        if sensor in {"co2", "aqi"}:
            overridden[sensor] = int(value)
        else:
            overridden[sensor] = value
    return overridden


async def sensor_broadcast_loop() -> None:
    """Continuously publish sensor and decision updates to connected clients."""
    while True:
        try:
            data = apply_sensor_overrides(simulator.get_reading().to_dict())
            decision = engine.make_decision(data)

            if manual_mode_override:
                decision.mode = manual_mode_override

            simulator.set_recirc_mode(decision.mode == "RECIRCULATE")

            payload = {
                "sensors": data,
                "decision": decision.to_dict(),
                "timestamp": time.time(),
            }

            stale_connections = []
            for websocket in list(active_connections):
                try:
                    await websocket.send_json(payload)
                except Exception:
                    stale_connections.append(websocket)

            for websocket in stale_connections:
                active_connections.discard(websocket)
        except Exception:
            LOGGER.exception("Error in sensor broadcast loop")

        await asyncio.sleep(UPDATE_INTERVAL_SECONDS)


def log_startup_banner() -> None:
    LOGGER.info("%s", "=" * 58)
    LOGGER.info("  ARIA - Adaptive Recirculation Intelligence Agent")
    LOGGER.info("  Production Build | 5-Sensor | CAN Bus Ready")
    LOGGER.info("  Sensors: CO2 | AQI | PM2.5 | Temperature | Humidity")
    LOGGER.info("%s", "=" * 58)
    LOGGER.info("  Dashboard: http://localhost:%s", PORT)
    LOGGER.info("  Update interval: %ss", UPDATE_INTERVAL_SECONDS)
    LOGGER.info("%s", "=" * 58)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(sensor_broadcast_loop())
    log_startup_banner()
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        for websocket in list(active_connections):
            with suppress(Exception):
                await websocket.close()
        active_connections.clear()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time sensor updates"""
    await websocket.accept()
    active_connections.add(websocket)
    
    await websocket.send_json({"type": "connection_ack", "status": "connected"})
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.discard(websocket)


@app.get("/healthz")
async def healthcheck():
    """Basic health endpoint for deployment probes."""
    return {
        "status": "ok",
        "service": "aria",
        "connections": len(active_connections),
    }


@app.get("/")
async def index(request: Request):
    """Serve the main dashboard"""
    try:
        return templates.TemplateResponse("index.html", {"request": request})
    except Exception as e:
        logging.error(f"Template error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/sensor-data")
async def get_sensor_data():
    """Get the latest simulated sensor reading."""
    try:
        return apply_sensor_overrides(simulator.get_reading().to_dict())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/history")
async def get_history(limit: int = 50):
    """Get decision history for diagnostics"""
    try:
        safe_limit = max(1, min(limit, 200))
        return engine.get_history(safe_limit)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/manual-mode")
async def set_manual_mode(request: Request):
    """Set manual mode override for local testing controls."""
    global manual_mode_override

    try:
        body = await request.json()
        mode = body.get("mode")

        if mode not in ["RECIRCULATE", "FRESH_AIR", None]:
            return JSONResponse({"error": f"Invalid mode: {mode}"}, status_code=400)

        manual_mode_override = mode
        return {
            "status": "success",
            "mode": mode,
            "message": f"Manual mode set to {mode if mode else 'AUTO'}",
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/sensor-override")
async def set_sensor_override(request: Request):
    """Override a supported sensor value for local testing controls."""
    try:
        body = await request.json()
        sensor = body.get("sensor")
        value = body.get("value")

        valid_sensors = {
            "co2",
            "aqi",
            "pm25",
            "temperature",
            "humidity",
            "cabin_temp",
            "cabin_pm25",
            "cabin_humidity",
        }
        if sensor not in valid_sensors:
            return JSONResponse({"error": f"Invalid sensor: {sensor}"}, status_code=400)

        if value is None:
            sensor_overrides.pop(sensor, None)
            return {
                "status": "success",
                "sensor": sensor,
                "value": None,
                "message": f"Cleared {sensor} override",
            }

        sensor_overrides[sensor] = float(value)
        return {
            "status": "success",
            "sensor": sensor,
            "value": sensor_overrides[sensor],
            "message": f"Override {sensor} = {value}",
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/clear-all-overrides")
async def clear_all_overrides():
    """Clear all active sensor overrides."""
    try:
        sensor_overrides.clear()
        return {"status": "success", "message": "All overrides cleared"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
