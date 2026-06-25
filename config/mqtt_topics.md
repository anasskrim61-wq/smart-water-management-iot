# MQTT Topic Specification
**Smart Urban Water Management System**  
*Author: Anass Krim | Last Updated: 2025-10-01*

---

## Overview

All MQTT communication in this system follows a structured topic hierarchy rooted at `water/`. The Mosquitto broker runs on the Raspberry Pi gateway at port **1883** (plaintext) or **8883** (TLS). Each ESP32 field node has a unique `{node_id}` assigned at firmware flash time.

```
water/
├── nodes/
│   ├── {node_id}/
│   │   ├── telemetry       ← Sensor readings (published by node)
│   │   └── status          ← Heartbeat & online/offline (published by node; LWT)
└── commands/
    └── {node_id}/
        ├── sleep           ← Remote sleep command (published by gateway)
        └── reboot          ← Remote reboot command (published by gateway)
├── alerts/
│   └── {node_id}           ← Processed alerts (published by gateway)
```

---

## Topic Reference Table

| Topic | Publisher | Subscriber | QoS | Retain | Description |
|-------|-----------|-----------|-----|--------|-------------|
| `water/nodes/{node_id}/telemetry` | ESP32 Node | Gateway | 1 | No | Primary sensor data payload |
| `water/nodes/{node_id}/status` | ESP32 Node | Gateway | 1 | Yes | Node heartbeat, online/offline status |
| `water/alerts/{node_id}` | Gateway | Dashboard | 2 | No | Processed alert with severity |
| `water/commands/{node_id}/sleep` | Gateway | ESP32 Node | 1 | No | Instruct node to sleep for N seconds |
| `water/commands/{node_id}/reboot` | Gateway | ESP32 Node | 2 | No | Instruct node to reboot immediately |

---

## QoS Level Rationale

| QoS | Used For | Rationale |
|-----|----------|-----------|
| **QoS 0** (at most once) | Debug/test messages | No overhead; acceptable data loss |
| **QoS 1** (at least once) | Telemetry, status | Guarantees delivery; duplicate readings handled via timestamp dedup |
| **QoS 2** (exactly once) | Reboot commands, critical alerts | Must execute exactly once; overhead acceptable for infrequent messages |

---

## Payload Schemas

### `water/nodes/{node_id}/telemetry`

**Published by:** ESP32 Node every 30 seconds  
**Direction:** Node → Broker → Gateway

```json
{
  "node_id":    "node_A",
  "timestamp":  86400,
  "firmware":   "1.3.2",
  "boot_count": 2880,
  "sensors": {
    "flow_rate":   12.47,
    "pressure":    312.5,
    "turbidity":   18.3,
    "temperature": 19.8
  },
  "system": {
    "battery_mv":  3821,
    "wifi_rssi":   -62,
    "uptime_s":    86400,
    "heap_free":   142336
  }
}
```

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `node_id` | string | — | — | Unique node identifier |
| `timestamp` | integer | seconds | 0–∞ | Seconds since first boot (proxy for time without NTP) |
| `firmware` | string | — | semver | Firmware version string |
| `boot_count` | integer | — | 0–∞ | Number of deep-sleep wake cycles |
| `sensors.flow_rate` | float | L/min | 0–50 | Volumetric flow rate from YF-S201 |
| `sensors.pressure` | float | kPa | 0–700 | Pipe pressure from MPX5700AP |
| `sensors.turbidity` | float | NTU | 0–3000 | Water turbidity from SEN0189 |
| `sensors.temperature` | float | °C | -10–80 | Water temperature from DS18B20 |
| `system.battery_mv` | integer | mV | 3000–4200 | LiPo battery voltage |
| `system.wifi_rssi` | integer | dBm | -100–0 | WiFi signal strength |
| `system.uptime_s` | integer | s | 0–∞ | Node uptime in seconds |
| `system.heap_free` | integer | bytes | 0–520000 | Free heap memory on ESP32 |

---

### `water/nodes/{node_id}/status`

**Published by:** ESP32 Node on connect; also set as Last Will & Testament (LWT)  
**Retained:** Yes (broker stores last value for new subscribers)

```json
{
  "node_id":  "node_A",
  "status":   "online",
  "firmware": "1.3.2"
}
```

**LWT payload** (automatically published by broker on abnormal disconnect):
```json
{
  "node_id": "node_A",
  "status":  "offline"
}
```

---

### `water/alerts/{node_id}`

**Published by:** Gateway after threshold or AI anomaly detection  
**QoS:** 2 (exactly once — alerts must not be lost or duplicated)

```json
{
  "node_id":    "node_B",
  "severity":   "CRITICAL",
  "alert_type": "LOW_PRESSURE",
  "message":    "Pressure 62.3 kPa below minimum 100 kPa — possible leak!",
  "score":      null,
  "timestamp":  "2025-10-14T10:45:00Z"
}
```

| `severity` | `alert_type` | Trigger Condition |
|------------|-------------|------------------|
| `WARNING`  | `LOW_FLOW` | flow_rate < threshold.flow_rate_min |
| `CRITICAL` | `LOW_PRESSURE` | pressure < threshold.pressure_min |
| `WARNING`  | `HIGH_TURBIDITY` | turbidity > threshold.turbidity_max |
| `WARNING`  | `HIGH_TEMPERATURE` | temperature > threshold.temperature_max |
| `WARNING` / `CRITICAL` | `AI_ANOMALY` | IsolationForest score < -0.10 / < -0.30 |

---

### `water/commands/{node_id}/sleep`

**Published by:** Gateway to remotely extend a node's sleep interval.  
**Payload:** Plain integer string (seconds to sleep)

```
3600
```

The node will enter deep sleep for 3600 seconds (1 hour) instead of the default 30 s.

---

### `water/commands/{node_id}/reboot`

**Published by:** Gateway to force a remote firmware restart.  
**Payload:** Any non-empty string (e.g., `"reboot"`)

```
reboot
```

---

## Wildcard Subscriptions Used by Gateway

| Subscription Pattern | Matches | Use Case |
|---------------------|---------|----------|
| `water/nodes/+/telemetry` | Any node's telemetry | Central ingestion of all readings |
| `water/nodes/+/status` | Any node's status | Tracking node online/offline state |

The `+` wildcard matches exactly one topic level (single-level wildcard).
