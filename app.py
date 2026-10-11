import time
import json
import math
import sqlite3
import threading
from pathlib import Path

from flask import Flask, Response, request, send_from_directory


# Raspberry Pi 5 hardware imports.
# If the required libraries are unavailable, use simulation mode.
try:
    from smbus2 import SMBus
    from gpiozero import AngularServo

    HARDWARE_AVAILABLE = True

except ImportError:
    HARDWARE_AVAILABLE = False

    print(
        "Running in Simulation Sandbox Mode "
        "(No Pi hardware libraries detected)"
    )


app = Flask(__name__)


# --- GLOBAL CONFIGURATION SETTINGS ---

PAN_PIN = 12
TILT_PIN = 13

BH1750_ADDR = 0x23
INA219_ADDR = 0x40

DEADZONE = 30
SERVO_STEP = 2.0
HISTORY_SAMPLE_SECONDS = 5.0
HISTORY_MAX_SECONDS = 7 * 24 * 60 * 60
HISTORY_DB_PATH = Path(__file__).resolve().parent / "energy_history.db"


# --- SHARED TELEMETRY STATE ---

telemetry = {
    "v": 0.0,
    "ma": 0.0,
    "w": 0.0,
    "lux": 0,
    "l1": 0,
    "l2": 0,
    "l3": 0,
    "l4": 0,
    "t": 22.0,
    "h": 45.0,
    "pan": 90.0,
    "tilt": 90.0,
    "status": "IDLE",
}

energy_history_lock = threading.Lock()
energy_database = sqlite3.connect(
    HISTORY_DB_PATH,
    check_same_thread=False,
)
energy_database.execute("""
    CREATE TABLE IF NOT EXISTS energy_samples (
        timestamp REAL PRIMARY KEY,
        energy_wh REAL NOT NULL,
        watts REAL NOT NULL,
        interval_wh REAL NOT NULL
    )
""")
energy_database.commit()
energy_wh_total = energy_database.execute(
    "SELECT COALESCE(MAX(energy_wh), 0) FROM energy_samples"
).fetchone()[0]


# --- SERVO POSITION VARIABLES ---

current_pan = 90.0
current_tilt = 90.0


# --- HARDWARE INITIALIZATION ---

if HARDWARE_AVAILABLE:
    try:
        pan_servo = AngularServo(
            PAN_PIN,
            min_angle=0,
            max_angle=180,
            min_pulse_width=0.0005,
            max_pulse_width=0.0025,
        )

        tilt_servo = AngularServo(
            TILT_PIN,
            min_angle=0,
            max_angle=180,
            min_pulse_width=0.0005,
            max_pulse_width=0.0025,
        )

        bus = SMBus(1)

    except Exception as error:
        print(f"Hardware initialization error: {error}")
        print("Defaulting to simulation mode.")

        HARDWARE_AVAILABLE = False


# --- BH1750 LIGHT SENSOR ---

def read_bh1750():
    """Read ambient light intensity in lux."""

    if not HARDWARE_AVAILABLE:
        return int(
            40000 + math.sin(time.time() / 5) * 10000
        )

    try:
        data = bus.read_i2c_block_data(
            BH1750_ADDR,
            0x10,
            2,
        )

        # Combine the two returned bytes into one reading.
        raw_value = (data[0] << 8) | data[1]

        return int(raw_value / 1.2)

    except Exception as error:
        print(f"BH1750 read error: {error}")
        return 0


# --- INA219 VOLTAGE AND CURRENT SENSOR ---

def read_ina219():
    """Read panel voltage and current."""

    if not HARDWARE_AVAILABLE:
        voltage = 5.0 + math.sin(time.time()) * 0.1

        current_ma = (
            450.0 + math.cos(time.time()) * 150.0
        )

        return voltage, current_ma

    try:
        raw_voltage = bus.read_word_data(
            INA219_ADDR,
            0x02,
        )

        voltage = (
            (raw_voltage >> 3) & 0x1FFF
        ) * 0.004

        raw_current = bus.read_word_data(
            INA219_ADDR,
            0x04,
        )

        current_ma = raw_current * 0.1

        return voltage, current_ma

    except Exception as error:
        print(f"INA219 read error: {error}")
        return 0.0, 0.0


# --- FOUR LIGHT-DEPENDENT RESISTORS ---

def read_ldrs():
    """Read the four light sensor values."""

    if not HARDWARE_AVAILABLE:
        current_time = time.time()

        l1 = int(
            512 + math.sin(current_time * 1.5) * 400
        )

        l2 = int(
            512 + math.cos(current_time * 1.5) * 400
        )

        l3 = int(
            512 - math.sin(current_time * 1.5) * 400
        )

        l4 = int(
            512 - math.cos(current_time * 1.5) * 400
        )

        return l1, l2, l3, l4

    # Placeholder values: replace with actual ADC readings.
    return 600, 620, 580, 590


# --- BACKGROUND SOLAR TRACKING LOOP ---

def hardware_control_loop():
    """Continuously update sensor readings and servo positions."""

    global current_pan, current_tilt, telemetry, energy_wh_total

    print("Hardware control loop initialized.")
    last_measurement_time = time.monotonic()
    last_history_time = last_measurement_time

    while True:
        time.sleep(0.05)

        # Read sensor measurements.
        lux = read_bh1750()

        voltage, current_ma = read_ina219()

        l1, l2, l3, l4 = read_ldrs()

        # Calculate electrical power in watts.
        watts = voltage * (current_ma / 1000.0)
        now = time.monotonic()
        elapsed_hours = (now - last_measurement_time) / 3600.0
        energy_wh_total += max(0.0, watts) * elapsed_hours
        last_measurement_time = now

        if now - last_history_time >= HISTORY_SAMPLE_SECONDS:
            timestamp = time.time()
            interval_hours = (now - last_history_time) / 3600.0

            with energy_history_lock:
                energy_database.execute(
                    """
                    INSERT INTO energy_samples
                    (timestamp, energy_wh, watts, interval_wh)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        energy_wh_total,
                        watts,
                        max(0.0, watts) * interval_hours,
                    ),
                )
                energy_database.commit()

            last_history_time = now

        # Calculate average light levels.
        avg_top = (l1 + l2) / 2
        avg_bottom = (l3 + l4) / 2

        avg_left = (l1 + l3) / 2
        avg_right = (l2 + l4) / 2

        # Determine tracking errors.
        diff_elevation = avg_top - avg_bottom
        diff_azimuth = avg_left - avg_right

        status = "ALIGNED"

        # Track the brightest direction when sufficient light exists.
        if lux > 500:

            # Vertical tracking.
            if abs(diff_elevation) > DEADZONE:
                status = "SEARCHING"

                if diff_elevation > 0:
                    current_tilt = min(
                        current_tilt + SERVO_STEP,
                        180.0,
                    )

                else:
                    current_tilt = max(
                        current_tilt - SERVO_STEP,
                        0.0,
                    )

            # Horizontal tracking.
            if abs(diff_azimuth) > DEADZONE:
                status = "SEARCHING"

                if diff_azimuth > 0:
                    current_pan = max(
                        current_pan - SERVO_STEP,
                        0.0,
                    )

                else:
                    current_pan = min(
                        current_pan + SERVO_STEP,
                        180.0,
                    )

            # Apply the new servo positions.
            if HARDWARE_AVAILABLE:
                try:
                    pan_servo.angle = current_pan
                    tilt_servo.angle = current_tilt

                except Exception as error:
                    print(f"Servo write error: {error}")

        else:
            status = "NIGHT MODE"

        # Publish the latest readings to the dashboard.
        telemetry.update({
            "v": voltage,
            "ma": current_ma,
            "w": watts,
            "lux": lux,
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "l4": l4,
            "pan": current_pan,
            "tilt": current_tilt,
            "status": status,
        })


# --- DASHBOARD ROUTE ---

@app.route("/")
def home():
    """Serve the dashboard HTML file."""

    try:
        dashboard_path = Path(__file__).resolve().parent / "index.html"

        with dashboard_path.open("r", encoding="utf-8") as html_file:

            return html_file.read()

    except FileNotFoundError:
        return (
            "System File Error: Ensure index.html "
            "is in the application folder.",
            404,
        )


@app.route("/helios_logo.png")
def helios_logo():
    """Serve the dashboard logo."""

    return send_from_directory(
        Path(__file__).resolve().parent,
        "helios_logo.png",
    )


@app.route("/energy-history")
def energy_history_data():
    """Return timestamped cumulative energy readings for the dashboard."""

    ranges = {
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
        "7d": HISTORY_MAX_SECONDS,
        "month": 30 * 24 * 60 * 60,
        "all": None,
    }
    selected_range = request.args.get("range", "24h")
    seconds = ranges.get(selected_range)
    view = request.args.get("view", "cumulative")
    time_ranges = {
        "morning": (6 * 60, 12 * 60),
        "midday": (10 * 60, 15 * 60),
        "afternoon": (12 * 60, 18 * 60),
        "evening": (17 * 60, 22 * 60),
        "full-day": (0, 24 * 60),
    }
    selected_time_range = request.args.get("time_range", "full-day")

    if selected_range not in ranges:
        return {"error": "Unsupported history range."}, 400
    if view not in {"cumulative", "average"}:
        return {"error": "Unsupported history view."}, 400
    if selected_time_range not in time_ranges:
        return {"error": "Unsupported time-of-day range."}, 400

    cutoff = (
        time.time() - seconds
        if seconds is not None
        else 0
    )
    with energy_history_lock:
        if view == "average":
            time_start, time_end = time_ranges[selected_time_range]
            rows = energy_database.execute(
                """
                SELECT
                    CAST(strftime('%H', timestamp, 'unixepoch', 'localtime')
                        AS INTEGER) * 60
                    + CAST(strftime('%M', timestamp, 'unixepoch', 'localtime')
                        AS INTEGER) / 30 * 30 AS minute_of_day,
                    AVG(interval_wh) AS average_interval_wh,
                    AVG(watts) AS average_watts,
                    COUNT(*) AS samples
                FROM energy_samples
                WHERE timestamp >= ?
                AND (
                    CAST(strftime('%H', timestamp, 'unixepoch', 'localtime')
                        AS INTEGER) * 60
                    + CAST(strftime('%M', timestamp, 'unixepoch', 'localtime')
                        AS INTEGER)
                ) >= ?
                AND (
                    CAST(strftime('%H', timestamp, 'unixepoch', 'localtime')
                        AS INTEGER) * 60
                    + CAST(strftime('%M', timestamp, 'unixepoch', 'localtime')
                        AS INTEGER)
                ) < ?
                GROUP BY minute_of_day
                ORDER BY minute_of_day
                """,
                (cutoff, time_start, time_end),
            ).fetchall()
            average_readings = [
                {
                    "minute_of_day": row[0],
                    "average_interval_wh": row[1],
                    "average_watts": row[2],
                    "samples": row[3],
                }
                for row in rows
            ]
            return {
                "range": selected_range,
                "view": view,
                "time_range": selected_time_range,
                "total_wh": energy_wh_total,
                "average_readings": average_readings,
            }

        rows = energy_database.execute(
            """
            SELECT timestamp, energy_wh, watts
            FROM energy_samples
            WHERE timestamp >= ?
            ORDER BY timestamp
            """,
            (cutoff,),
        ).fetchall()
        readings = [
            {
                "timestamp": row[0],
                "energy_wh": row[1],
                "watts": row[2],
            }
            for row in rows
        ]
        window_total = sum(
            row[0] for row in energy_database.execute(
                """
                SELECT interval_wh
                FROM energy_samples
                WHERE timestamp >= ?
                """,
                (cutoff,),
            ).fetchall()
        )

    return {
        "range": selected_range,
        "view": view,
        "total_wh": energy_wh_total,
        "window_wh": window_total,
        "readings": readings,
    }


# --- LIVE TELEMETRY STREAM ---

@app.route("/stream")
def stream_telemetry():
    """Send live sensor data using Server-Sent Events."""

    def generate_data_packets():
        while True:
            time.sleep(0.1)

            payload = json.dumps(telemetry)

            yield f"data: {payload}\n\n"

    return Response(
        generate_data_packets(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --- APPLICATION ENTRY POINT ---

if __name__ == "__main__":

    # Start the background sensor and tracking loop.
    hardware_thread = threading.Thread(
        target=hardware_control_loop,
        daemon=True,
    )

    hardware_thread.start()

    # Start the Flask web server.
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
    )