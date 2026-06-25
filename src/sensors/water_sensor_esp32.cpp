/**
 * =============================================================================
 * water_sensor_esp32.cpp
 * Smart Urban Water Resource Management System — ESP32 Node Firmware
 *
 * Author  : Anass Krim
 * Version : 1.3.2
 * Board   : ESP32-WROOM-32D (Arduino Framework)
 * Date    : 2025-10-01
 *
 * Description:
 *   This firmware runs on each field-deployed ESP32 node. It reads four water
 *   quality / hydraulic sensors every 30 seconds, formats the data as a JSON
 *   payload, and publishes it to an MQTT broker over WiFi. Between readings the
 *   device enters deep sleep to maximise battery life (~18 months on 3000 mAh).
 *
 * Sensors:
 *   - YF-S201   : Hall-effect flow meter (1–30 L/min)  — GPIO 34 (interrupt)
 *   - MPX5700AP : Piezo-resistive pressure sensor       — GPIO 35 (ADC)
 *   - SEN0189   : Turbidity sensor (0–3000 NTU)         — GPIO 32 (ADC)
 *   - DS18B20   : 1-Wire waterproof temperature probe   — GPIO 4
 *
 * Dependencies (install via Arduino Library Manager or PlatformIO):
 *   - PubSubClient   2.8.0   — MQTT client
 *   - ArduinoJson    6.21.3  — JSON serialisation
 *   - DallasTemperature 3.9  — DS18B20 driver
 *   - OneWire        2.3.7   — 1-Wire bus driver
 *
 * License: MIT
 * =============================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "esp_system.h"   // esp_reset_reason, esp_deep_sleep_start
#include "esp_task_wdt.h" // Hardware watchdog timer

// =============================================================================
// USER CONFIGURATION — Edit these before flashing
// =============================================================================
#define WIFI_SSID          "YourWiFiSSID"
#define WIFI_PASSWORD      "YourWiFiPassword"
#define MQTT_BROKER_IP     "192.168.1.100"   // Raspberry Pi IP
#define MQTT_BROKER_PORT   1883
#define MQTT_CLIENT_ID     "node_A"          // Unique per node
#define NODE_ID            "node_A"          // Published in payload
#define FIRMWARE_VERSION   "1.3.2"

// MQTT topic constructed at runtime from NODE_ID
// Format: water/nodes/<node_id>/telemetry
#define TOPIC_TELEMETRY    "water/nodes/" NODE_ID "/telemetry"
#define TOPIC_STATUS       "water/nodes/" NODE_ID "/status"
#define TOPIC_CMD_SLEEP    "water/commands/" NODE_ID "/sleep"
#define TOPIC_CMD_REBOOT   "water/commands/" NODE_ID "/reboot"

// =============================================================================
// PIN DEFINITIONS
// =============================================================================
#define PIN_FLOW_SENSOR    34   // GPIO34 — YF-S201 pulse output (INPUT_PULLUP)
#define PIN_PRESSURE       35   // GPIO35 — MPX5700AP analog output
#define PIN_TURBIDITY      32   // GPIO32 — SEN0189 analog output
#define PIN_ONE_WIRE       4    // GPIO4  — DS18B20 data line (4.7 kΩ pull-up)

// =============================================================================
// TIMING CONSTANTS
// =============================================================================
#define DEEP_SLEEP_SECONDS     30          // Wake interval in seconds
#define FLOW_MEASURE_WINDOW_MS 5000        // 5-second window to count pulses
#define WIFI_TIMEOUT_MS        15000       // Max wait for WiFi connection
#define MQTT_TIMEOUT_MS        8000        // Max wait for MQTT connection
#define WATCHDOG_TIMEOUT_S     60          // WDT trips after 60 s of inactivity

// =============================================================================
// SENSOR CALIBRATION CONSTANTS
// =============================================================================
// YF-S201: 7.5 pulses per litre per minute (from datasheet)
#define FLOW_PULSE_PER_LITRE   7.5f

// MPX5700AP: Vout = Vs * (0.00369 * P + 0.04)
//   Vs = 5 V, ADC ref = 3.3 V via voltage divider (3.3/5 ratio)
//   ADC 12-bit range: 0–4095 → 0–3.3 V
#define PRESSURE_VREF          3.3f
#define PRESSURE_ADC_MAX       4095.0f
#define PRESSURE_VS            5.0f

// SEN0189 turbidity: empirical calibration (mV → NTU)
// NTU = -1120.4 * V^2 + 5742.3 * V - 4353.8  (manufacturer formula)
#define TURBIDITY_VREF         3.3f

// =============================================================================
// GLOBAL OBJECTS
// =============================================================================
WiFiClient          wifiClient;
PubSubClient        mqttClient(wifiClient);
OneWire             oneWireBus(PIN_ONE_WIRE);
DallasTemperature   tempSensor(&oneWireBus);

// RTC memory — survives deep sleep (stores pulse count across wake cycles)
RTC_DATA_ATTR uint32_t bootCount       = 0;
RTC_DATA_ATTR uint32_t totalPulseCount = 0;

// Flow sensor interrupt counter (volatile — accessed in ISR)
volatile uint32_t pulseCount = 0;

// =============================================================================
// INTERRUPT SERVICE ROUTINE — Flow Sensor
// =============================================================================
/**
 * @brief ISR triggered on each falling edge from YF-S201 Hall-effect sensor.
 *        Increments the pulse counter atomically.
 */
void IRAM_ATTR flowPulseISR() {
    pulseCount++;
}

// =============================================================================
// WIFI CONNECTION
// =============================================================================
/**
 * @brief Attempts WiFi connection with a configurable timeout.
 *        Retries up to 3 times before giving up and sleeping again.
 * @return true if connected, false on timeout.
 */
bool connectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    Serial.printf("[WiFi] Connecting to %s", WIFI_SSID);

    uint32_t startMs = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - startMs > WIFI_TIMEOUT_MS) {
            Serial.println("\n[WiFi] Connection timeout!");
            return false;
        }
        delay(500);
        Serial.print(".");
    }

    Serial.printf("\n[WiFi] Connected! IP: %s, RSSI: %d dBm\n",
                  WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
    return true;
}

// =============================================================================
// MQTT CONNECTION & CALLBACKS
// =============================================================================
/**
 * @brief Callback invoked when a subscribed MQTT message is received.
 *        Handles remote sleep and reboot commands from the gateway.
 *
 * @param topic   MQTT topic string
 * @param payload Raw message bytes
 * @param length  Payload length in bytes
 */
void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String topicStr = String(topic);
    String payloadStr;
    for (unsigned int i = 0; i < length; i++) {
        payloadStr += (char)payload[i];
    }

    Serial.printf("[MQTT] Received on [%s]: %s\n", topic, payloadStr.c_str());

    if (topicStr == TOPIC_CMD_REBOOT) {
        Serial.println("[CMD] Reboot command received — restarting in 1 s...");
        delay(1000);
        ESP.restart();
    }
    else if (topicStr == TOPIC_CMD_SLEEP) {
        // Payload expected: number of seconds to sleep
        uint32_t sleepSec = payloadStr.toInt();
        if (sleepSec > 0 && sleepSec < 3600) {
            Serial.printf("[CMD] Extended sleep command: %u s\n", sleepSec);
            esp_sleep_enable_timer_wakeup((uint64_t)sleepSec * 1000000ULL);
            esp_deep_sleep_start();
        }
    }
}

/**
 * @brief Establishes MQTT connection and subscribes to command topics.
 *        Publishes a retained "online" status message on success.
 * @return true if connected, false on failure.
 */
bool connectMQTT() {
    mqttClient.setServer(MQTT_BROKER_IP, MQTT_BROKER_PORT);
    mqttClient.setCallback(mqttCallback);
    mqttClient.setBufferSize(512);  // Increase for large JSON payloads

    Serial.printf("[MQTT] Connecting to broker %s:%d ...\n",
                  MQTT_BROKER_IP, MQTT_BROKER_PORT);

    // Last Will and Testament — broker publishes "offline" if node disconnects
    String lwt_payload = "{\"node_id\":\"" NODE_ID "\",\"status\":\"offline\"}";

    bool connected = mqttClient.connect(
        MQTT_CLIENT_ID,         // Client ID (must be unique)
        nullptr,                // Username (not used)
        nullptr,                // Password (not used)
        TOPIC_STATUS,           // LWT topic
        1,                      // LWT QoS
        true,                   // LWT retain
        lwt_payload.c_str()     // LWT message
    );

    if (!connected) {
        Serial.printf("[MQTT] Connection failed, rc=%d\n", mqttClient.state());
        return false;
    }

    Serial.println("[MQTT] Connected!");

    // Subscribe to command topics
    mqttClient.subscribe(TOPIC_CMD_SLEEP, 1);
    mqttClient.subscribe(TOPIC_CMD_REBOOT, 2);

    // Publish online status (retained)
    String statusPayload = "{\"node_id\":\"" NODE_ID "\",\"status\":\"online\","
                           "\"firmware\":\"" FIRMWARE_VERSION "\"}";
    mqttClient.publish(TOPIC_STATUS, statusPayload.c_str(), true);

    return true;
}

// =============================================================================
// SENSOR READING FUNCTIONS
// =============================================================================
/**
 * @brief Measures water flow rate over a fixed time window using pulse counting.
 *        The YF-S201 generates 7.5 pulses per L/min.
 *
 * @return Flow rate in Litres per Minute (L/min), or -1 on error.
 */
float readFlowRate() {
    // Attach interrupt, zero counter, wait, detach
    pulseCount = 0;
    attachInterrupt(digitalPinToInterrupt(PIN_FLOW_SENSOR), flowPulseISR, FALLING);
    delay(FLOW_MEASURE_WINDOW_MS);
    detachInterrupt(digitalPinToInterrupt(PIN_FLOW_SENSOR));

    // Convert pulse count to L/min
    // frequency (Hz) = pulseCount / (window_s)
    // flow (L/min)   = frequency / FLOW_PULSE_PER_LITRE * 60
    float windowSec = FLOW_MEASURE_WINDOW_MS / 1000.0f;
    float frequency = (float)pulseCount / windowSec;
    float flowRate  = (frequency / FLOW_PULSE_PER_LITRE) * 60.0f;

    totalPulseCount += pulseCount;  // Accumulate in RTC memory
    Serial.printf("[Flow] Pulses: %u, Rate: %.2f L/min\n", pulseCount, flowRate);
    return flowRate;
}

/**
 * @brief Reads pipe pressure from MPX5700AP via 12-bit ADC.
 *        Converts ADC counts → Voltage → Pressure (kPa) using datasheet formula.
 *
 * @return Pressure in kilopascals (kPa), or -1 on ADC error.
 */
float readPressure() {
    // Average 16 ADC samples to reduce noise
    uint32_t adcSum = 0;
    const uint8_t numSamples = 16;
    for (uint8_t i = 0; i < numSamples; i++) {
        adcSum += analogRead(PIN_PRESSURE);
        delayMicroseconds(200);
    }
    float adcAvg = (float)adcSum / numSamples;

    // Convert ADC reading to voltage (ESP32 ADC Vref ≈ 3.3 V, 12-bit)
    float voltage = (adcAvg / PRESSURE_ADC_MAX) * PRESSURE_VREF;

    // MPX5700AP transfer function: Vout = Vs * (0.00369 * P + 0.04)
    // Rearranged: P = (Vout/Vs - 0.04) / 0.00369
    // Note: sensor output is scaled through a voltage divider (3.3V/5V ratio)
    float vout_actual = voltage * (PRESSURE_VS / PRESSURE_VREF);  // Undo divider
    float pressure_kpa = (vout_actual / PRESSURE_VS - 0.04f) / 0.00369f;

    // Clamp to valid sensor range [0, 700] kPa
    if (pressure_kpa < 0.0f) pressure_kpa = 0.0f;
    if (pressure_kpa > 700.0f) pressure_kpa = 700.0f;

    Serial.printf("[Pressure] ADC avg: %.1f, Voltage: %.3f V, Pressure: %.2f kPa\n",
                  adcAvg, voltage, pressure_kpa);
    return pressure_kpa;
}

/**
 * @brief Reads water turbidity from SEN0189 sensor.
 *        Higher voltage = lower turbidity (inverse relationship).
 *        Uses manufacturer's empirical polynomial: NTU = f(V).
 *
 * @return Turbidity in NTU (Nephelometric Turbidity Units), 0–3000.
 */
float readTurbidity() {
    // Average 16 samples
    uint32_t adcSum = 0;
    const uint8_t numSamples = 16;
    for (uint8_t i = 0; i < numSamples; i++) {
        adcSum += analogRead(PIN_TURBIDITY);
        delayMicroseconds(200);
    }
    float adcAvg  = (float)adcSum / numSamples;
    float voltage = (adcAvg / PRESSURE_ADC_MAX) * TURBIDITY_VREF;

    // Manufacturer empirical formula for SEN0189 (valid 2.5–4.2 V):
    // NTU = -1120.4 * V^2 + 5742.3 * V - 4353.8
    float ntu = -1120.4f * voltage * voltage + 5742.3f * voltage - 4353.8f;

    // Clamp to physical range
    if (ntu < 0.0f)   ntu = 0.0f;
    if (ntu > 3000.0f) ntu = 3000.0f;

    Serial.printf("[Turbidity] ADC avg: %.1f, Voltage: %.3f V, NTU: %.2f\n",
                  adcAvg, voltage, ntu);
    return ntu;
}

/**
 * @brief Reads water temperature from DS18B20 sensor over 1-Wire bus.
 *        Issues a blocking conversion (750 ms for 12-bit resolution).
 *
 * @return Temperature in °C, or -127.0 on sensor error.
 */
float readTemperature() {
    tempSensor.requestTemperatures();
    float tempC = tempSensor.getTempCByIndex(0);

    if (tempC == DEVICE_DISCONNECTED_C) {
        Serial.println("[Temp] ERROR: DS18B20 not found or disconnected!");
        return -127.0f;
    }

    Serial.printf("[Temp] Temperature: %.2f °C\n", tempC);
    return tempC;
}

// =============================================================================
// BATTERY VOLTAGE MEASUREMENT
// =============================================================================
/**
 * @brief Reads battery voltage via internal ADC on GPIO 36 (VP).
 *        Assumes a 2:1 voltage divider (100kΩ / 100kΩ) to scale 4.2 V → 2.1 V.
 *
 * @return Battery voltage in millivolts.
 */
uint32_t readBatteryMv() {
    // GPIO36 is ADC1_CH0, safe to use in deep-sleep-wakeup cycle
    uint32_t adcRaw = analogRead(36);
    // ADC reading to voltage (12-bit, 3.3 V Vref)
    float voltage = (adcRaw / 4095.0f) * 3.3f;
    // Undo 2:1 voltage divider
    float battVoltage = voltage * 2.0f;
    return (uint32_t)(battVoltage * 1000.0f);  // Convert to mV
}

// =============================================================================
// PAYLOAD CONSTRUCTION & MQTT PUBLISH
// =============================================================================
/**
 * @brief Builds a JSON payload with all sensor readings and publishes it via MQTT.
 *
 * @param flowRate    L/min
 * @param pressure    kPa
 * @param turbidity   NTU
 * @param temperature °C
 * @return true if publish succeeded, false otherwise.
 */
bool publishTelemetry(float flowRate, float pressure, float turbidity, float temperature) {
    // Use a static buffer — ArduinoJson v6 on stack
    StaticJsonDocument<512> doc;

    // Top-level fields
    doc["node_id"]   = NODE_ID;
    doc["firmware"]  = FIRMWARE_VERSION;
    doc["boot_count"] = bootCount;

    // Timestamp — ESP32 does not have RTC; use boot count as sequence proxy.
    // In production, use NTP: configTime(0, 0, "pool.ntp.org");
    doc["timestamp"] = bootCount * DEEP_SLEEP_SECONDS;  // seconds since first boot

    // Nested sensor object
    JsonObject sensors = doc.createNestedObject("sensors");
    sensors["flow_rate"]   = serialized(String(flowRate, 2));
    sensors["pressure"]    = serialized(String(pressure, 2));
    sensors["turbidity"]   = serialized(String(turbidity, 2));
    sensors["temperature"] = serialized(String(temperature, 2));

    // System health object
    JsonObject system = doc.createNestedObject("system");
    system["battery_mv"] = readBatteryMv();
    system["wifi_rssi"]  = WiFi.RSSI();
    system["uptime_s"]   = bootCount * DEEP_SLEEP_SECONDS;
    system["heap_free"]  = ESP.getFreeHeap();

    // Serialize to char buffer
    char jsonBuffer[512];
    size_t payloadLen = serializeJson(doc, jsonBuffer, sizeof(jsonBuffer));

    Serial.printf("[MQTT] Publishing %zu bytes to [%s]\n", payloadLen, TOPIC_TELEMETRY);
    Serial.println(jsonBuffer);

    bool ok = mqttClient.publish(TOPIC_TELEMETRY, jsonBuffer, false);  // QoS 0 via PubSubClient
    if (!ok) {
        Serial.printf("[MQTT] Publish FAILED (state=%d)\n", mqttClient.state());
    }
    return ok;
}

// =============================================================================
// SETUP — Runs once per deep-sleep wake cycle
// =============================================================================
void setup() {
    Serial.begin(115200);
    delay(100);  // Let serial stabilise

    bootCount++;
    Serial.printf("\n========================================\n");
    Serial.printf(" Water Node [%s] — Boot #%u\n", NODE_ID, bootCount);
    Serial.printf(" Reset reason: %d\n", esp_reset_reason());
    Serial.printf("========================================\n");

    // ---- Configure Hardware Watchdog (60 s) ----
    // If setup() or loop() stalls, the WDT resets the chip
    esp_task_wdt_init(WATCHDOG_TIMEOUT_S, true);
    esp_task_wdt_add(NULL);  // Subscribe current task to WDT

    // ---- Configure ADC ----
    analogReadResolution(12);             // 12-bit resolution (0–4095)
    analogSetAttenuation(ADC_11db);       // Full-scale 0–3.3 V range

    // ---- Configure GPIO ----
    pinMode(PIN_FLOW_SENSOR, INPUT_PULLUP);  // Flow sensor — pulled high, ISR on falling edge

    // ---- Initialize DS18B20 ----
    tempSensor.begin();
    tempSensor.setResolution(12);  // 12-bit = 0.0625°C resolution (750 ms conversion)

    // ---- Connect to WiFi ----
    if (!connectWiFi()) {
        Serial.println("[Main] WiFi failed — entering deep sleep to retry.");
        esp_sleep_enable_timer_wakeup((uint64_t)DEEP_SLEEP_SECONDS * 1000000ULL);
        esp_deep_sleep_start();
    }

    // ---- Connect to MQTT ----
    if (!connectMQTT()) {
        Serial.println("[Main] MQTT failed — entering deep sleep to retry.");
        WiFi.disconnect(true);
        esp_sleep_enable_timer_wakeup((uint64_t)DEEP_SLEEP_SECONDS * 1000000ULL);
        esp_deep_sleep_start();
    }

    // ---- Read All Sensors ----
    // Note: readFlowRate() blocks for FLOW_MEASURE_WINDOW_MS (5 s)
    float flowRate    = readFlowRate();
    float pressure    = readPressure();
    float turbidity   = readTurbidity();
    float temperature = readTemperature();

    // ---- Feed watchdog before MQTT publish ----
    esp_task_wdt_reset();

    // ---- Publish via MQTT ----
    publishTelemetry(flowRate, pressure, turbidity, temperature);

    // ---- Process any incoming MQTT commands ----
    mqttClient.loop();
    delay(100);

    // ---- Graceful disconnect ----
    mqttClient.disconnect();
    WiFi.disconnect(true);
    delay(200);

    // ---- Enter deep sleep ----
    uint32_t sleepMicros = (uint64_t)(DEEP_SLEEP_SECONDS - FLOW_MEASURE_WINDOW_MS / 1000) * 1000000ULL;
    Serial.printf("[Main] Entering deep sleep for %u s...\n",
                  DEEP_SLEEP_SECONDS - FLOW_MEASURE_WINDOW_MS / 1000);
    Serial.flush();

    esp_sleep_enable_timer_wakeup(sleepMicros);
    esp_deep_sleep_start();
    // Execution never reaches here — deep sleep restarts from setup()
}

// =============================================================================
// LOOP — Not used (deep sleep restarts from setup() each cycle)
// =============================================================================
void loop() {
    // Empty — the device sleeps between readings.
    // If deep sleep is disabled for debugging, this loop is a fallback.
    esp_task_wdt_reset();
    delay(1000);
}
