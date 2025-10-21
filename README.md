# Intelligent Rust-Based ESP32 System for Real-Time IoT and Process Simulation
---

This repository contains the work for the research paper: **"Development of an Intelligent Rust-Based Embedded System for SHT20 Modbus RTU and DWSIM AC Simulation Integration with Real-Time IoT Connectivity"**.

The project presents a smart, reliable embedded system built on the ESP32-S3 microcontroller and programmed entirely in **Rust**. It bridges the gap between physical industrial sensors and virtual process simulations by creating a real-time, "hardware-in-the-loop" environment. The system monitors an air conditioning (AC) process, uses live sensor data to drive a thermodynamic simulation in DWSIM, and visualizes all data on a modern IoT platform.

---

## 👨‍💻 Author
1. Rizal Khoirul Atok (2042231013)
2. Naufal Faqiih Ashshiddiq (2042231068)
3. Ahmad Radhy (Supervisor)

Teknik Instrumentasi - Institut Teknologi Sepuluh Nopember

---

## 📋 Key Features

* [cite_start]**Rust-Based Firmware**: Developed using Rust for its memory safety, high concurrency, and performance, ensuring robust and reliable industrial communication[cite: 15, 24].
* [cite_start]**Hardware-in-the-Loop Simulation**: Integrates a physical SHT20 Modbus RTU sensor with a DWSIM AC process simulation in a closed loop[cite: 11, 57]. [cite_start]Live temperature and humidity data dynamically drive the simulation[cite: 12, 132].
* **Bidirectional Control**: The system is not just for monitoring. [cite_start]It uses the output from the DWSIM simulation to control physical actuators (a fan and a pump), mimicking a real-world control system[cite: 14, 87].
* [cite_start]**Real-Time IoT Dashboard**: All sensor and simulation data is published to an MQTT broker and visualized in real-time on the ThingsBoard platform[cite: 13, 89].
* [cite_start]**Time-Series Data Storage**: Uses InfluxDB for high-performance storage and querying of all time-series data, enabling historical analysis[cite: 13, 89].
* [cite_start]**Industrial Protocol Support**: Utilizes the robust Modbus RTU protocol for communication between the ESP32-S3 and the industrial-grade SHT20 sensor[cite: 11, 85].

---

## 🏗️ System Architecture

[cite_start]The system is orchestrated by an **ESP32-S3 microcontroller**, which acts as the central hub[cite: 84]. [cite_start]It acquires data from the **SHT20 sensor** via Modbus RTU, communicates with the **DWSIM** simulation over Wi-Fi (via a Python API), and controls the physical fan and pump[cite: 85, 86, 87]. [cite_start]Simultaneously, it publishes all data to an **MQTT Broker**, which then forwards it to **InfluxDB** for storage and **ThingsBoard** for visualization[cite: 88, 89].

![alt text](https://github.com/atok99/ESP32-S3_ModbusRTU_Integred-with-DWSIM-Simulation/blob/main/SystemArchitectur.png?raw=true)

*Figure 1: System Architecture and Data Flow*

---

## 🛠️ Hardware & Wiring

### Components Required

* [cite_start]**Microcontroller**: ESP32-S3 development board[cite: 100].
* [cite_start]**Sensor**: SHT20 temperature and humidity sensor with Modbus RTU interface[cite: 101].
* [cite_start]**Converter**: RS485 to TTL converter module for Modbus communication[cite: 102].
* [cite_start]**Actuators**: A 5V DC Fan and a 5V DC Pump[cite: 103].
* [cite_start]**Control**: A 2-channel 5V Relay Module to safely control the actuators[cite: 104].
* [cite_start]**Power**: A standard 5V USB adapter[cite: 106].

### Wiring Diagram


*Figure 2: Hardware Wiring Diagram*

---

## 💻 Software, Simulation & Technology Stack

The system's logic is divided between the embedded firmware and the process simulation model.

* [cite_start]**Embedded Firmware**: Written entirely in **Rust**[cite: 115]. [cite_start]It handles polling the sensor, communicating with the DWSIM API, controlling the relays, and publishing data via MQTT[cite: 117, 118, 120, 121].
* [cite_start]**Process Simulation**: A **DWSIM** model of a Heat Exchanger simulates the AC cooling coil[cite: 124]. [cite_start]The `Air_In` temperature is updated by the SHT20 sensor, and DWSIM calculates the resulting `Air_Out` temperature[cite: 132, 134].
    
    *Figure 3: DWSIM Heat Exchanger Model*
* [cite_start]**API Bridge**: A **Python script** exposes the DWSIM simulation as a REST API, allowing the ESP32-S3 to interact with it over the network[cite: 135].
* **Backend & IoT Platform**:
    * [cite_start]**Database**: **InfluxDB** is used for time-series data storage[cite: 151].
    * [cite_start]**Visualization**: **ThingsBoard** provides the real-time dashboards, charts, and widgets[cite: 153].
    * [cite_start]**Communication**: An **MQTT Broker** handles message routing between the ESP32-S3 and the backend[cite: 154].

---

## 🚀 How It Works: A Demonstration

The system operates in a continuous, real-time loop, bridging the physical and virtual worlds.

### 1. Real-Time Data Acquisition
The system reads temperature and humidity from the SHT20 sensor. [cite_start]This live data is then immediately sent to update the DWSIM simulation's `Air_In` parameter[cite: 175].


*Figure 4: Terminal log showing SHT20 data being sent to DWSIM*

### 2. Thermodynamic Simulation
[cite_start]With the updated `Air_In` value (e.g., **32.4°C**), DWSIM runs its thermodynamic calculations and computes the cooled `Air_Out` temperature (e.g., **11.977°C**)[cite: 205, 207].


*Figure 5: DWSIM calculating the output temperature*

### 3. Data Transmission to IoT Platforms
[cite_start]The calculated `Air_Out` value is retrieved from DWSIM and uploaded via MQTT to InfluxDB and ThingsBoard, completing the data loop[cite: 214, 215].


*Figure 6: Terminal log confirming data upload to InfluxDB and ThingsBoard*

### 4. Data Aggregation and Visualization
[cite_start]All data streams are aggregated in the InfluxDB database and visualized in the ThingsBoard dashboard, providing a complete overview of both the physical and simulated parameters[cite: 242, 244].


*Figure 7: Time-series data from all sources in InfluxDB*

---

## 📊 Performance Highlights

The system demonstrated exceptional performance and reliability during a 7-day intensive testing period.

* [cite_start]**Uptime & Reliability**: **99.9%** uptime and a **99.8%** Modbus communication success rate[cite: 166, 287].
* [cite_start]**Data Integrity**: **99.7%** end-to-end data transmission success rate[cite: 276, 287].
* [cite_start]**Low Latency**: Ultra-low processing latency averaging **12 ms** on the ESP32-S3[cite: 168].
* [cite_start]**Resource Efficiency**: The Rust firmware has a remarkably small memory footprint, using only **128 KB** of Program Flash and **45 KB** of Static RAM[cite: 170].
* [cite_start]**Cost-Effective**: The entire hardware implementation costs approximately **$35 per node**, offering an affordable alternative to commercial solutions[cite: 289].

---

## Acknowledgements

[cite_start]This research was conducted autonomously without being funded by any institution[cite: 297]. [cite_start]The authors would like to thank the Department of Instrumentation Engineering, Institut Teknologi Sepuluh Nopember for providing the facilities and support for this research[cite: 298].
