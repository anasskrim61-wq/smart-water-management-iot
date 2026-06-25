# Hardware Bill of Materials
**Smart Urban Water Resource Management System**  
*Author: Anass Krim | Revision: 1.2 | Date: 2025-10-01*

---

## BOM Summary

| Category | Item Count | Total Cost (USD) | Total Cost (EUR) |
|----------|-----------|-----------------|-----------------|
| Microcontrollers | 4 nodes | $22.00 | €20.40 |
| Flow Sensors | 4 units | $12.80 | €11.80 |
| Pressure Sensors | 4 units | $35.60 | €32.80 |
| Turbidity Sensors | 4 units | $38.00 | €35.00 |
| Temperature Sensors | 4 units | $11.20 | €10.40 |
| Gateway (SBC) | 1 unit | $55.00 | €50.70 |
| Power System | 4 nodes | $50.00 | €46.08 |
| Passive Components | Assorted | $2.28 | €2.10 |
| Mechanical / Enclosures | 4 units | $24.00 | €22.12 |
| Storage / PSU | 2 items | $14.00 | €12.90 |
| **Grand Total** | | **≈ $264.88** | **≈ €244.30** |

---

## Detailed Component List

### Microcontrollers

| Ref | Component | Manufacturer | Part Number | Qty | Unit (USD) | Total (USD) | Specs |
|-----|-----------|-------------|------------|-----|-----------|------------|-------|
| U1–U4 | ESP32 SoC Module | Espressif | ESP32-WROOM-32D | 4 | $5.50 | $22.00 | Dual-core Xtensa LX6 @ 240 MHz, 4 MB Flash, 520 KB SRAM, WiFi 802.11 b/g/n, BT 4.2, 38 GPIOs |

### Hydraulic / Water Quality Sensors

| Ref | Component | Manufacturer | Part Number | Qty | Unit (USD) | Total (USD) | Specs |
|-----|-----------|-------------|------------|-----|-----------|------------|-------|
| FS1–FS4 | Hall Effect Flow Sensor | Generic | YF-S201 | 4 | $3.20 | $12.80 | Range: 1–30 L/min, Pressure: 2.0 MPa max, Output: 5 V pulse @ 7.5 Hz/(L/min), G1/2 thread |
| PS1–PS4 | Piezo-resistive Pressure Sensor | NXP / Freescale | MPX5700AP | 4 | $8.90 | $35.60 | Range: 0–700 kPa, Output: 0.2–4.7 V, Supply: 5 V DC, Error: ±2.5%, Response: 1 ms |
| TS1–TS4 | Turbidity Sensor Module | DFRobot | SEN0189 | 4 | $9.50 | $38.00 | Range: 0–3000 NTU, Supply: 5 V, Output: 0–4.5 V analog, IP65 probe |
| TH1–TH4 | Waterproof Temp Probe | Dallas/Maxim | DS18B20 | 4 | $2.80 | $11.20 | Range: -55 to +125 °C, Accuracy: ±0.5 °C (−10 to 85 °C), 1-Wire bus, IP67 stainless probe, 3 m cable |

### Gateway / Edge Server

| Ref | Component | Manufacturer | Part Number | Qty | Unit (USD) | Total (USD) | Specs |
|-----|-----------|-------------|------------|-----|-----------|------------|-------|
| GW1 | Single-Board Computer | Raspberry Pi Foundation | Raspberry Pi 4B 4 GB | 1 | $55.00 | $55.00 | Quad-core Cortex-A72 @ 1.8 GHz, 4 GB LPDDR4, Gigabit Ethernet, 4× USB 3.0, WiFi 802.11ac |

### Power System

| Ref | Component | Manufacturer | Part Number | Qty | Unit (USD) | Total (USD) | Specs |
|-----|-----------|-------------|------------|-----|-----------|------------|-------|
| B1–B4 | LiPo Battery | Generic | 3.7 V 3000 mAh | 4 | $7.00 | $28.00 | Nominal: 3.7 V, Capacity: 3000 mAh, Max charge: 4.2 V, JST-PH 2-pin connector |
| SC1–SC4 | Solar Charge Controller + Panel | Generic | TP4056 + 6V 1W | 4 | $5.50 | $22.00 | Charge IC: TP4056 (1A), Panel: 6V 1W monocrystalline, includes protection circuit (DW01+) |
| PSU1 | USB-C Power Supply | Raspberry Pi | Official 27W | 1 | $8.00 | $8.00 | 5 V / 3 A, USB-C, for Raspberry Pi 4B |

### Passive & Miscellaneous Components

| Ref | Component | Value | Package | Qty | Unit (USD) | Total (USD) | Purpose |
|-----|-----------|-------|---------|-----|-----------|------------|---------|
| R1–R4 | Resistor | 4.7 kΩ ±1% | 0603 SMD | 4 | $0.05 | $0.20 | DS18B20 1-Wire pull-up to 3.3 V |
| R5–R8 | Resistor Divider | 100 kΩ ±1% | 0603 SMD | 8 | $0.05 | $0.40 | MPX5700 output voltage divider (5V→3.3V) |
| C1–C16 | Decoupling Capacitor | 100 nF, 10 V | 0402 SMD | 16 | $0.03 | $0.48 | VCC decoupling per sensor |
| C17–C20 | Bulk Capacitor | 100 µF, 10 V | Electrolytic | 4 | $0.10 | $0.40 | Power rail bulk decoupling per node |
| J1–J4 | JST-PH Connectors | 2-pin, 3-pin | Through-hole | 16 | $0.05 | $0.80 | Battery and sensor connectors |

### Mechanical & Enclosures

| Ref | Component | Specification | Qty | Unit (USD) | Total (USD) | Notes |
|-----|-----------|--------------|-----|-----------|------------|-------|
| ENC1–ENC4 | Waterproof ABS Enclosure | 120×80×55 mm, IP67 | 4 | $6.00 | $24.00 | Field deployment housing; pre-drill cable glands |
| CG1–CG16 | PG7 Cable Glands | IP68, 3–6.5 mm cable | 16 | $0.20 | $3.20 | Sensor cable entry seals |
| MT1–MT16 | M3 Standoffs | Brass, M3×10 mm | 16 | $0.05 | $0.80 | PCB mounting inside enclosure |

### Storage

| Ref | Component | Specification | Qty | Unit (USD) | Total (USD) | Notes |
|-----|-----------|--------------|-----|-----------|------------|-------|
| SD1 | MicroSD Card | 32 GB, Class 10 A1 | 1 | $6.00 | $6.00 | Raspberry Pi OS + database storage |
| SD2 | MicroSD Adapter | SPI module | 1 | $1.50 | $1.50 | Optional external SD for logging expansion |

---

## Supplier Recommendations

| Supplier | Region | Notes |
|----------|--------|-------|
| [AliExpress](https://www.aliexpress.com) | Global | Lowest prices; 3–6 week shipping |
| [Mouser Electronics](https://www.mouser.com) | Global | Authorised distributor; NXP MPX5700 |
| [DigiKey](https://www.digikey.com) | Global | Authorised distributor; passives, connectors |
| [DFRobot](https://www.dfrobot.com) | Global | SEN0189 turbidity sensor official store |
| [Botland](https://botland.store) | Europe | ESP32, sensors, components |
| [Semageek](https://www.semageek.com) | Morocco/France | Local components for prototyping |

---

## Tools Required for Assembly

| Tool | Purpose |
|------|---------|
| Soldering iron (350 °C, fine tip) | SMD and through-hole soldering |
| Hot air rework station | SMD component placement |
| Digital multimeter | Continuity, voltage, resistance verification |
| USB-to-UART programmer | ESP32 firmware upload |
| Step drill bit (16 mm, 20 mm) | Enclosure cable gland holes |
| Silicone RTV sealant | Additional IP sealing of enclosure seams |
