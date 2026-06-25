"""
=============================================================================
app.py
Smart Urban Water Management System — Flask Dashboard Backend

Author  : Anass Krim
Version : 1.0.1
Date    : 2025-10-01

Description:
    Flask application serving:
      - REST API endpoints for sensor data, nodes, alerts, and AI status
      - WebSocket (Flask-SocketIO) for live telemetry push to browser clients
      - Background thread that polls the SQLite database for new readings
        and broadcasts them to connected WebSocket clients every 5 seconds
      - Integration with the LeakDetector for on-demand inference

Usage:
    python src/dashboard/app.py --config config/config.yml
    python src/dashboard/app.py --host 0.0.0.0 --port 5000 --debug

License: MIT
=============================================================================
"""

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit

# ── Optional AI import ────────────────────────────────────────────────────────
try:
    from src.ai.leak_detection import LeakDetector
    AI_AVAILABLE = True
except ImportError:
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        from src.ai.leak_detection import LeakDetector
        AI_AVAILABLE = True
    except ImportError:
        AI_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# =============================================================================
# MINIMAL DASHBOARD HTML (served at /)
# =============================================================================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>💧 Smart Water Dashboard</title>
  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
  <style>
    body { font-family: system-ui, sans-serif; background: #0a1628; color: #e0f0ff; margin: 0; padding: 20px; }
    h1   { color: #00b4d8; }
    .card { background: #112240; border: 1px solid #1d3461; border-radius: 8px;
            padding: 16px; margin-bottom: 12px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; }
    .CRITICAL { background: #c0392b; }
    .WARNING  { background: #d68910; }
    .INFO     { background: #1a6fa8; }
    #live-log { height: 300px; overflow-y: auto; background: #0a1628;
                border: 1px solid #1d3461; padding: 10px; font-size: 0.85em; font-family: monospace; }
  </style>
</head>
<body>
  <h1>💧 Smart Urban Water Management Dashboard</h1>
  <div class="card">
    <h3>Live Telemetry Feed</h3>
    <div id="live-log">[Connecting to WebSocket...]</div>
  </div>
  <div class="card">
    <h3>Latest Readings</h3>
    <div id="latest"></div>
  </div>
  <script>
    const socket = io();
    const log = document.getElementById('live-log');

    socket.on('connect', () => {
      log.innerHTML = '[Connected to live feed]\\n';
    });

    socket.on('telemetry', (data) => {
      const ts = new Date().toLocaleTimeString();
      const line = `[${ts}] Node ${data.node_id} | Flow: ${data.flow_rate} L/min | Pressure: ${data.pressure} kPa | Turbidity: ${data.turbidity} NTU | Temp: ${data.temperature}°C\\n`;
      log.innerHTML += line;
      log.scrollTop = log.scrollHeight;
    });

    socket.on('alert', (data) => {
      const ts = new Date().toLocaleTimeString();
      const line = `[${ts}] ⚠️ ALERT [${data.severity}] Node ${data.node_id}: ${data.message}\\n`;
      log.innerHTML += line;
      log.scrollTop = log.scrollHeight;
    });

    // Fetch latest readings every 10s
    async function refreshLatest() {
      try {
        const resp = await fetch('/api/sensors/latest');
        const data = await resp.json();
        const div  = document.getElementById('latest');
        div.innerHTML = data.readings.map(r =>
          `<p><strong>${r.node_id}</strong> — Flow: ${r.flow_rate} L/min, Pressure: ${r.pressure} kPa, Turbidity: ${r.turbidity} NTU, Temp: ${r.temperature}°C @ ${r.timestamp}</p>`
        ).join('');
      } catch(e) { console.error(e); }
    }
    refreshLatest();
    setInterval(refreshLatest, 10000);
  </script>
</body>
</html>
"""


# =============================================================================
# DATABASE HELPER (lightweight, read-only for dashboard)
# =============================================================================
class DashboardDB:
    """Read-only database helper for the dashboard process."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_latest_per_node(self) -> List[Dict]:
        if not os.path.exists(self.db_path):
            return []
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT r.*
                FROM readings r
                INNER JOIN (
                    SELECT node_id, MAX(timestamp) AS max_ts
                    FROM readings
                    GROUP BY node_id
                ) latest ON r.node_id = latest.node_id AND r.timestamp = latest.max_ts
                ORDER BY r.node_id
            """).fetchall()
        return [dict(r) for r in rows]

    def get_recent_readings(self, limit: int = 100) -> List[Dict]:
        if not os.path.exists(self.db_path):
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_unacknowledged_alerts(self, limit: int = 20) -> List[Dict]:
        if not os.path.exists(self.db_path):
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE acknowledged=0 ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_stats(self) -> List[Dict]:
        """Aggregate daily stats: avg flow, avg pressure, reading count."""
        if not os.path.exists(self.db_path):
            return []
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT
                    node_id,
                    DATE(timestamp) AS day,
                    COUNT(*)        AS reading_count,
                    AVG(flow_rate)  AS avg_flow,
                    AVG(pressure)   AS avg_pressure,
                    AVG(turbidity)  AS avg_turbidity,
                    AVG(temperature) AS avg_temperature,
                    MIN(pressure)   AS min_pressure,
                    MAX(flow_rate)  AS max_flow
                FROM readings
                GROUP BY node_id, DATE(timestamp)
                ORDER BY day DESC, node_id
                LIMIT 100
            """).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# FLASK APPLICATION FACTORY
# =============================================================================
def create_app(config: Dict) -> tuple:
    """
    Create and configure the Flask application and SocketIO instance.

    Returns:
        (app, socketio) tuple.
    """
    app      = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "water-mgmt-dev-secret-2025")
    CORS(app)
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

    db       = DashboardDB(config["database"]["path"])
    detector = None

    if AI_AVAILABLE:
        try:
            model_path = config.get("ai_model", {}).get("model_path", "models/leak_detector.joblib")
            detector   = LeakDetector(model_path=model_path)
            logger.info("LeakDetector loaded for dashboard.")
        except Exception as e:
            logger.warning(f"Dashboard AI init failed: {e}")

    # ── HTTP Routes ──────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/health")
    def health():
        return jsonify({
            "status":    "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ai_available": AI_AVAILABLE and detector is not None and detector._is_fitted,
        })

    @app.route("/api/sensors/latest")
    def latest():
        readings = db.get_latest_per_node()
        return jsonify({"count": len(readings), "readings": readings})

    @app.route("/api/sensors")
    def sensors():
        limit    = min(int(request.args.get("limit", 100)), 500)
        readings = db.get_recent_readings(limit)
        return jsonify({"count": len(readings), "readings": readings})

    @app.route("/api/alerts")
    def alerts():
        limit  = min(int(request.args.get("limit", 20)), 200)
        alerts = db.get_unacknowledged_alerts(limit)
        return jsonify({"count": len(alerts), "alerts": alerts})

    @app.route("/api/stats/daily")
    def daily_stats():
        stats = db.get_daily_stats()
        return jsonify({"count": len(stats), "stats": stats})

    @app.route("/api/model/status")
    def model_status():
        if detector:
            return jsonify(detector.get_model_info())
        return jsonify({"error": "AI model not available"}), 503

    @app.route("/api/predict", methods=["POST"])
    def predict():
        """On-demand anomaly prediction endpoint."""
        if not detector or not detector._is_fitted:
            return jsonify({"error": "Model not fitted"}), 503
        data = request.get_json(force=True)
        features = [
            float(data.get("flow_rate", 0)),
            float(data.get("pressure", 0)),
            float(data.get("turbidity", 0)),
            float(data.get("temperature", 0)),
        ]
        result = detector.predict_single(features)
        return jsonify(result)

    # ── WebSocket Events ─────────────────────────────────────────────────────

    @socketio.on("connect")
    def handle_connect():
        logger.info(f"WebSocket client connected: {request.sid}")
        emit("status", {"message": "Connected to Smart Water Dashboard"})

    @socketio.on("disconnect")
    def handle_disconnect():
        logger.info(f"WebSocket client disconnected: {request.sid}")

    @socketio.on("subscribe_node")
    def handle_subscribe(data):
        node_id = data.get("node_id", "all")
        logger.info(f"Client {request.sid} subscribed to node: {node_id}")
        emit("status", {"message": f"Subscribed to node: {node_id}"})

    # ── Background Broadcast Thread ──────────────────────────────────────────

    def broadcast_loop():
        """
        Background thread: every 5 seconds push latest readings and
        unacknowledged alerts to all connected WebSocket clients.
        """
        last_reading_id = 0
        while True:
            time.sleep(5)
            try:
                readings = db.get_recent_readings(limit=10)
                for r in readings:
                    if r["id"] > last_reading_id:
                        socketio.emit("telemetry", r)
                        last_reading_id = max(last_reading_id, r["id"])

                alerts = db.get_unacknowledged_alerts(limit=5)
                for alert in alerts:
                    socketio.emit("alert", alert)

            except Exception as e:
                logger.error(f"Broadcast loop error: {e}")

    broadcast_thread = threading.Thread(target=broadcast_loop, daemon=True)
    broadcast_thread.start()

    return app, socketio


# =============================================================================
# ENTRY POINT
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Smart Water Dashboard Backend")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=5000)
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        logger.warning(f"Config not found at {args.config} — using defaults")
        config = {
            "database": {"path": "data/water_data.db"},
            "ai_model": {"model_path": "models/leak_detector.joblib"},
        }
    else:
        with open(args.config, "r") as f:
            config = yaml.safe_load(f)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    app, socketio = create_app(config)

    logger.info(f"Starting dashboard on http://{args.host}:{args.port}")
    socketio.run(app, host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
