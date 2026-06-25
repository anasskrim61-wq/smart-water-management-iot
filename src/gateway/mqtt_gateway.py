"""
=============================================================================
mqtt_gateway.py
Smart Urban Water Management System — MQTT Gateway & Data Aggregator

Author  : Anass Krim
Version : 1.2.0
Platform: Raspberry Pi 4 (Python 3.10+)
Date    : 2025-10-01

Description:
    This gateway process runs persistently on the Raspberry Pi. It:
      1. Subscribes to all water node MQTT topics via paho-mqtt
      2. Validates and sanitises incoming JSON payloads
      3. Persists clean readings to a local SQLite database
      4. Exposes a REST API via Flask for dashboard and external consumers
      5. Triggers the AI leak detection module on each new reading
      6. Dispatches webhook/email alerts for critical anomalies
      7. Provides a /api/alerts endpoint with filtering and pagination

Usage:
    python mqtt_gateway.py --config config/config.yml
    python mqtt_gateway.py --config config/config.local.yml --debug

License: MIT
=============================================================================
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt
import requests
import yaml
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Optional: import AI module if available ──────────────────────────────────
try:
    from src.ai.leak_detection import LeakDetector
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
    logging.warning("LeakDetector not available — AI anomaly detection disabled.")

# =============================================================================
# LOGGING SETUP
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/gateway.log", mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mqtt_gateway")

os.makedirs("logs", exist_ok=True)

# =============================================================================
# CONFIGURATION LOADER
# =============================================================================
def load_config(config_path: str) -> Dict[str, Any]:
    """Load YAML configuration file and return as a dict."""
    if not os.path.exists(config_path):
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    logger.info(f"Configuration loaded from: {config_path}")
    return cfg


# =============================================================================
# DATABASE MANAGER
# =============================================================================
class DatabaseManager:
    """
    Manages SQLite connection, schema creation, and CRUD operations.
    Uses thread-local connections to be safe with Flask + paho-mqtt threads.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._local = threading.local()
        self._init_schema()
        logger.info(f"Database initialised at: {db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL;")
            self._local.conn.execute("PRAGMA foreign_keys=ON;")
        return self._local.conn

    @contextmanager
    def transaction(self):
        """Context manager for atomic database transactions."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_schema(self) -> None:
        """Create database schema if it does not already exist."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id          TEXT PRIMARY KEY,
                location         TEXT,
                latitude         REAL,
                longitude        REAL,
                status           TEXT DEFAULT 'unknown',
                last_seen        DATETIME,
                firmware_version TEXT
            );

            CREATE TABLE IF NOT EXISTS readings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id       TEXT NOT NULL REFERENCES nodes(node_id),
                flow_rate     REAL,
                pressure      REAL,
                turbidity     REAL,
                temperature   REAL,
                battery_mv    INTEGER,
                wifi_rssi     INTEGER,
                anomaly_score REAL DEFAULT NULL,
                timestamp     DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id       TEXT NOT NULL REFERENCES nodes(node_id),
                severity      TEXT NOT NULL CHECK(severity IN ('INFO','WARNING','CRITICAL')),
                alert_type    TEXT NOT NULL,
                message       TEXT,
                acknowledged  INTEGER DEFAULT 0,
                created_at    DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                resolved_at   DATETIME DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_readings_node_ts
                ON readings (node_id, timestamp DESC);

            CREATE INDEX IF NOT EXISTS idx_alerts_node_sev
                ON alerts (node_id, severity, acknowledged);
        """)
        conn.commit()
        conn.close()

    def upsert_node(self, node_id: str, firmware: str) -> None:
        """Insert or update a node record, updating last_seen timestamp."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO nodes (node_id, firmware_version, status, last_seen)
                VALUES (?, ?, 'online', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(node_id) DO UPDATE SET
                    firmware_version = excluded.firmware_version,
                    status           = 'online',
                    last_seen        = excluded.last_seen
            """, (node_id, firmware))

    def insert_reading(self, node_id: str, sensors: Dict, system: Dict) -> int:
        """Insert a validated sensor reading and return the new row id."""
        with self.transaction() as conn:
            cursor = conn.execute("""
                INSERT INTO readings (node_id, flow_rate, pressure, turbidity,
                                      temperature, battery_mv, wifi_rssi)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                node_id,
                sensors.get("flow_rate"),
                sensors.get("pressure"),
                sensors.get("turbidity"),
                sensors.get("temperature"),
                system.get("battery_mv"),
                system.get("wifi_rssi"),
            ))
            return cursor.lastrowid

    def update_anomaly_score(self, reading_id: int, score: float) -> None:
        """Backfill anomaly score for a reading after AI inference."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE readings SET anomaly_score = ? WHERE id = ?",
                (score, reading_id)
            )

    def insert_alert(self, node_id: str, severity: str, alert_type: str, message: str) -> int:
        """Insert a new alert record and return its id."""
        with self.transaction() as conn:
            cursor = conn.execute("""
                INSERT INTO alerts (node_id, severity, alert_type, message)
                VALUES (?, ?, ?, ?)
            """, (node_id, severity, alert_type, message))
            return cursor.lastrowid

    def get_readings(self, node_id: Optional[str], limit: int, offset: int,
                     from_ts: Optional[str], to_ts: Optional[str]) -> List[Dict]:
        """Query sensor readings with optional filtering and pagination."""
        conn = self._get_conn()
        where_clauses = []
        params: List[Any] = []

        if node_id:
            where_clauses.append("node_id = ?")
            params.append(node_id)
        if from_ts:
            where_clauses.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            where_clauses.append("timestamp <= ?")
            params.append(to_ts)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.extend([limit, offset])

        rows = conn.execute(f"""
            SELECT * FROM readings
            {where_sql}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
        return [dict(r) for r in rows]

    def get_alerts(self, node_id: Optional[str], severity: Optional[str],
                   acknowledged: Optional[bool], limit: int) -> List[Dict]:
        """Query alerts with optional filters."""
        conn = self._get_conn()
        where_clauses = []
        params: List[Any] = []

        if node_id:
            where_clauses.append("node_id = ?")
            params.append(node_id)
        if severity:
            where_clauses.append("severity = ?")
            params.append(severity.upper())
        if acknowledged is not None:
            where_clauses.append("acknowledged = ?")
            params.append(1 if acknowledged else 0)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(limit)

        rows = conn.execute(f"""
            SELECT * FROM alerts
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
        """, params).fetchall()
        return [dict(r) for r in rows]

    def get_nodes(self) -> List[Dict]:
        """Return all registered nodes."""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        return [dict(r) for r in rows]

    def acknowledge_alert(self, alert_id: int) -> bool:
        """Acknowledge an alert by ID. Returns True if a row was updated."""
        with self.transaction() as conn:
            result = conn.execute(
                "UPDATE alerts SET acknowledged = 1, resolved_at = "
                "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (alert_id,)
            )
            return result.rowcount > 0


# =============================================================================
# PAYLOAD VALIDATOR
# =============================================================================
class PayloadValidator:
    """Validates incoming MQTT JSON payloads against schema and value ranges."""

    REQUIRED_FIELDS = {"node_id", "sensors"}
    SENSOR_FIELDS   = {"flow_rate", "pressure", "turbidity", "temperature"}

    def __init__(self, thresholds: Dict) -> None:
        self.thresholds = thresholds

    def validate(self, payload: Dict) -> Tuple[bool, List[str]]:
        """
        Validate a deserialized payload dict.

        Returns:
            (is_valid: bool, errors: List[str])
        """
        errors = []

        # Check required top-level fields
        for field in self.REQUIRED_FIELDS:
            if field not in payload:
                errors.append(f"Missing required field: '{field}'")

        if errors:
            return False, errors

        sensors = payload.get("sensors", {})

        # Check sensor sub-fields
        for field in self.SENSOR_FIELDS:
            if field not in sensors:
                errors.append(f"Missing sensor field: 'sensors.{field}'")
                continue
            val = sensors[field]
            if not isinstance(val, (int, float)):
                errors.append(f"Non-numeric value for sensors.{field}: {val!r}")

        if errors:
            return False, errors

        # Range checks
        flow    = float(sensors["flow_rate"])
        pres    = float(sensors["pressure"])
        turb    = float(sensors["turbidity"])
        temp    = float(sensors["temperature"])

        if not (0 <= flow <= 50):
            errors.append(f"flow_rate {flow} out of range [0, 50] L/min")
        if not (0 <= pres <= 800):
            errors.append(f"pressure {pres} out of range [0, 800] kPa")
        if not (0 <= turb <= 3000):
            errors.append(f"turbidity {turb} out of range [0, 3000] NTU")
        if not (-10 <= temp <= 80):
            errors.append(f"temperature {temp} out of range [-10, 80] °C")

        return len(errors) == 0, errors


# =============================================================================
# ALERT DISPATCHER
# =============================================================================
class AlertDispatcher:
    """Sends alert notifications via webhook or email."""

    def __init__(self, webhook_url: Optional[str]) -> None:
        self.webhook_url = webhook_url

    def dispatch(self, node_id: str, severity: str, alert_type: str, message: str) -> None:
        """Fire-and-forget alert dispatch in a daemon thread."""
        thread = threading.Thread(
            target=self._send_webhook,
            args=(node_id, severity, alert_type, message),
            daemon=True
        )
        thread.start()

    def _send_webhook(self, node_id: str, severity: str, alert_type: str, message: str) -> None:
        if not self.webhook_url:
            return
        payload = {
            "text": f"[{severity}] Water Node {node_id} — {alert_type}: {message}",
            "severity": severity,
            "node_id": node_id,
            "alert_type": alert_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=5)
            logger.info(f"Webhook dispatched → HTTP {resp.status_code}")
        except requests.RequestException as e:
            logger.error(f"Webhook dispatch failed: {e}")


# =============================================================================
# MQTT GATEWAY
# =============================================================================
class MQTTGateway:
    """
    Core gateway class. Manages MQTT client lifecycle, message routing,
    and orchestrates database writes and AI inference calls.
    """

    SUBSCRIBE_TOPICS = [
        ("water/nodes/+/telemetry", 1),
        ("water/nodes/+/status",    1),
    ]

    def __init__(self, config: Dict) -> None:
        self.config    = config
        self.db        = DatabaseManager(config["database"]["path"])
        self.validator = PayloadValidator(config.get("thresholds", {}))
        self.dispatcher = AlertDispatcher(
            config.get("notifications", {}).get("webhook_url")
        )
        self.detector: Optional["LeakDetector"] = None
        if AI_AVAILABLE:
            try:
                self.detector = LeakDetector(
                    contamination=config.get("ai_model", {}).get("contamination", 0.05)
                )
                logger.info("LeakDetector initialised.")
            except Exception as e:
                logger.warning(f"LeakDetector init failed: {e}")

        self.client = mqtt.Client(
            client_id=f"gateway_{os.getpid()}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

        self._running = threading.Event()
        self._running.set()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            logger.info("Connected to MQTT broker.")
            for topic, qos in self.SUBSCRIBE_TOPICS:
                client.subscribe(topic, qos)
                logger.info(f"Subscribed: [{topic}] QoS={qos}")
        else:
            logger.error(f"MQTT connection refused — rc={rc}")

    def _on_disconnect(self, client, userdata, rc) -> None:
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnect (rc={rc}). Will auto-reconnect...")

    def _on_message(self, client, userdata, msg) -> None:
        """Entry point for every received MQTT message."""
        topic   = msg.topic
        raw     = msg.payload.decode("utf-8", errors="replace")
        logger.debug(f"MSG [{topic}]: {raw[:200]}")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[{topic}] JSON parse error: {e} — payload: {raw[:100]}")
            return

        # Route by topic pattern
        if "/telemetry" in topic:
            self._handle_telemetry(topic, payload)
        elif "/status" in topic:
            self._handle_status(topic, payload)

    def _handle_telemetry(self, topic: str, payload: Dict) -> None:
        """Process a sensor telemetry message."""
        # Step 1: Validate
        valid, errors = self.validator.validate(payload)
        if not valid:
            logger.warning(f"[{topic}] Validation failed: {errors}")
            return

        node_id  = payload["node_id"]
        sensors  = payload["sensors"]
        system   = payload.get("system", {})
        firmware = payload.get("firmware", "unknown")

        # Step 2: Upsert node record
        self.db.upsert_node(node_id, firmware)

        # Step 3: Persist reading
        reading_id = self.db.insert_reading(node_id, sensors, system)
        logger.info(
            f"[{node_id}] Reading #{reading_id} stored — "
            f"flow={sensors['flow_rate']} L/min, "
            f"pressure={sensors['pressure']} kPa, "
            f"turbidity={sensors['turbidity']} NTU, "
            f"temp={sensors['temperature']} °C"
        )

        # Step 4: Threshold-based rule alerts
        self._check_thresholds(node_id, sensors)

        # Step 5: AI anomaly detection (non-blocking)
        if self.detector:
            threading.Thread(
                target=self._run_ai_detection,
                args=(node_id, reading_id, sensors),
                daemon=True
            ).start()

    def _handle_status(self, topic: str, payload: Dict) -> None:
        """Process a node status heartbeat message."""
        node_id  = payload.get("node_id", "unknown")
        status   = payload.get("status", "unknown")
        firmware = payload.get("firmware", "unknown")
        self.db.upsert_node(node_id, firmware)
        logger.info(f"[{node_id}] Status update: {status}")

    def _check_thresholds(self, node_id: str, sensors: Dict) -> None:
        """Compare readings against configured thresholds and fire alerts."""
        t = self.config.get("thresholds", {})

        checks = [
            (
                sensors["flow_rate"] < t.get("flow_rate_min", 0.5),
                "WARNING", "LOW_FLOW",
                f"Flow rate {sensors['flow_rate']:.2f} L/min below minimum {t.get('flow_rate_min', 0.5)} L/min"
            ),
            (
                sensors["pressure"] < t.get("pressure_min", 100),
                "CRITICAL", "LOW_PRESSURE",
                f"Pressure {sensors['pressure']:.2f} kPa below minimum {t.get('pressure_min', 100)} kPa — possible leak!"
            ),
            (
                sensors["turbidity"] > t.get("turbidity_max", 100),
                "WARNING", "HIGH_TURBIDITY",
                f"Turbidity {sensors['turbidity']:.2f} NTU exceeds limit {t.get('turbidity_max', 100)} NTU"
            ),
            (
                sensors["temperature"] > t.get("temperature_max", 35.0),
                "WARNING", "HIGH_TEMPERATURE",
                f"Temperature {sensors['temperature']:.2f} °C exceeds limit {t.get('temperature_max', 35.0)} °C"
            ),
        ]

        for (condition, severity, alert_type, message) in checks:
            if condition:
                alert_id = self.db.insert_alert(node_id, severity, alert_type, message)
                logger.warning(f"[ALERT #{alert_id}] [{severity}] {node_id} — {message}")
                self.dispatcher.dispatch(node_id, severity, alert_type, message)

    def _run_ai_detection(self, node_id: str, reading_id: int, sensors: Dict) -> None:
        """Run AI inference and store the anomaly score."""
        try:
            features = [
                sensors["flow_rate"],
                sensors["pressure"],
                sensors["turbidity"],
                sensors["temperature"],
            ]
            result = self.detector.predict_single(features)
            self.db.update_anomaly_score(reading_id, result["score"])

            if result["is_anomaly"]:
                severity = "CRITICAL" if result["score"] < -0.3 else "WARNING"
                msg = (
                    f"AI anomaly detected — score={result['score']:.4f}, "
                    f"severity={severity}"
                )
                alert_id = self.db.insert_alert(node_id, severity, "AI_ANOMALY", msg)
                logger.warning(f"[AI ALERT #{alert_id}] {msg}")
                self.dispatcher.dispatch(node_id, severity, "AI_ANOMALY", msg)
        except Exception as e:
            logger.error(f"AI detection error for reading #{reading_id}: {e}")

    def start(self) -> None:
        """Connect to broker and start the blocking MQTT loop."""
        broker = self.config["mqtt"]["broker_host"]
        port   = self.config["mqtt"]["broker_port"]
        ka     = self.config["mqtt"].get("keepalive", 60)

        logger.info(f"Connecting to MQTT broker at {broker}:{port} ...")
        self.client.connect(broker, port, keepalive=ka)
        self.client.loop_start()
        logger.info("MQTT gateway running. Press Ctrl+C to stop.")

    def stop(self) -> None:
        """Gracefully disconnect from broker."""
        logger.info("Shutting down MQTT gateway...")
        self.client.loop_stop()
        self.client.disconnect()


# =============================================================================
# FLASK REST API
# =============================================================================
def create_flask_app(db: DatabaseManager) -> Flask:
    """Factory function: creates and configures the Flask REST API."""
    app = Flask(__name__)
    CORS(app)

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

    @app.route("/api/nodes", methods=["GET"])
    def get_nodes():
        nodes = db.get_nodes()
        return jsonify({"count": len(nodes), "nodes": nodes})

    @app.route("/api/sensors", methods=["GET"])
    def get_sensors():
        node_id = request.args.get("node_id")
        limit   = min(int(request.args.get("limit", 100)), 1000)
        offset  = int(request.args.get("offset", 0))
        from_ts = request.args.get("from")
        to_ts   = request.args.get("to")

        readings = db.get_readings(node_id, limit, offset, from_ts, to_ts)
        return jsonify({"count": len(readings), "offset": offset, "readings": readings})

    @app.route("/api/sensors/latest", methods=["GET"])
    def get_latest():
        nodes = db.get_nodes()
        latest = []
        for node in nodes:
            readings = db.get_readings(node["node_id"], 1, 0, None, None)
            if readings:
                latest.append(readings[0])
        return jsonify({"count": len(latest), "readings": latest})

    @app.route("/api/alerts", methods=["GET"])
    def get_alerts():
        node_id      = request.args.get("node_id")
        severity     = request.args.get("severity")
        ack_str      = request.args.get("acknowledged")
        acknowledged = None
        if ack_str is not None:
            acknowledged = ack_str.lower() in ("true", "1", "yes")
        limit = min(int(request.args.get("limit", 50)), 500)

        alerts = db.get_alerts(node_id, severity, acknowledged, limit)
        return jsonify({"count": len(alerts), "alerts": alerts})

    @app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
    def acknowledge_alert(alert_id: int):
        ok = db.acknowledge_alert(alert_id)
        if ok:
            return jsonify({"status": "acknowledged", "alert_id": alert_id})
        return jsonify({"error": "Alert not found"}), 404

    return app


# =============================================================================
# ENTRY POINT
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Smart Water MQTT Gateway")
    parser.add_argument("--config", default="config/config.yml", help="Path to config YAML")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config  = load_config(args.config)
    gateway = MQTTGateway(config)
    gateway.start()

    # Start Flask API in a separate daemon thread
    flask_app = create_flask_app(gateway.db)
    api_cfg   = config.get("api", {})
    flask_thread = threading.Thread(
        target=flask_app.run,
        kwargs={
            "host":  api_cfg.get("host", "0.0.0.0"),
            "port":  api_cfg.get("port", 5000),
            "debug": False,
            "use_reloader": False,
        },
        daemon=True,
    )
    flask_thread.start()
    logger.info(f"REST API listening on {api_cfg.get('host','0.0.0.0')}:{api_cfg.get('port',5000)}")

    # Handle graceful shutdown on SIGINT / SIGTERM
    def _shutdown(signum, frame):
        logger.info(f"Signal {signum} received — shutting down.")
        gateway.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep main thread alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
