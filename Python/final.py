# -*- coding: utf-8 -*-
"""
DWSIM Automation and IoT Data Pipeline
Version: 3.3 (Split Payload Logic)

This script performs the following actions in a loop:
1. Reads temperature and humidity data from an ESP32 sensor via Serial port.
2. Determines Fan and Compressor status for ThingsBoard based on temperature.
3. Writes the sensor temperature to the 'Air_In' material stream in a running DWSIM simulation.
4. Waits for the simulation to update.
5. Reads the resulting 'Air_Out' temperature from the DWSIM GUI.
6. Uploads original sensor/DWSIM data to InfluxDB.
7. Uploads sensor/DWSIM data PLUS status data to ThingsBoard MQTT.
"""
import time
import sys
import serial
import json
import logging
import re
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass

# --- Import untuk otomatisasi GUI ---
import pygetwindow as gw
from pywinauto import Application, findwindows

# --- Import untuk platform data ---
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import paho.mqtt.client as mqtt

# ==============================================================================
# BAGIAN 1: PENGATURAN & KONFIGURASI GLOBAL
# ==============================================================================

# Pengaturan koneksi ESP32
SERIAL_PORT = "COM6"
BAUD_RATE = 115200

# Pengaturan otomatisasi DWSIM
DWSIM_APP_PATH = "DWSIM.exe"

# Pengaturan untuk MENULIS ke DWSIM
DWSIM_WRITE_PANEL = "Air_In (Material Stream)"
DWSIM_WRITE_CONTROL_ID = "tbTemp"

# Pengaturan untuk MEMBACA dari DWSIM
DWSIM_READ_LOCKED_INDEX = 8 

# Pengaturan interval dan logika status
UPDATE_INTERVAL = 15  # Detik
TEMPERATURE_THRESHOLD = 27.0 # Batas suhu untuk status Fan/Compressor

# Konfigurasi logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==============================================================================
# BAGIAN 2: STRUKTUR DATA & KELAS UNTUK MEMBACA DWSIM
# ==============================================================================

@dataclass
class TemperatureReading:
    """Dataclass untuk menyimpan hasil pembacaan temperatur yang sudah valid."""
    value: float
    timestamp: datetime
    source: str
    confidence_score: int
    context: str

class TemperatureCandidate:
    """Mewakili kandidat nilai temperatur yang ditemukan di GUI."""
    def __init__(self, value: str, context: str):
        self.value = value
        self.context = context
        self.score = self.calculate_score()
        
    def calculate_score(self) -> int:
        """Menghitung skor kepercayaan untuk kandidat temperatur."""
        score = 0
        context_lower = self.context.lower()
        try:
            val = float(self.value)
            if 15 <= val <= 200: score += 20
            elif -50 <= val <= 500: score += 10
            
            if 'air_out' in context_lower: score += 50
            if 'temperature' in context_lower: score += 30
            if 'stream conditions' in context_lower: score += 40
            
            if 'fraction' in context_lower: score -= 60
        except ValueError:
            score = -100
        return score

class DWSIMReaderService:
    """Service untuk menghubungkan dan membaca data dari antarmuka DWSIM."""
    def __init__(self):
        self.app = None
        self.main_window = None
        self.connection_established = False
        self.connect_to_dwsim()

    def connect_to_dwsim(self):
        logger.info(" READER: Menghubungkan ke proses DWSIM...")
        try:
            self.app = Application(backend="uia").connect(path=DWSIM_APP_PATH, timeout=10)
            self.main_window = self.app.top_window()
            logger.info(f" READER: Berhasil terhubung ke jendela: {self.main_window.window_text()}")
            self.connection_established = True
        except (findwindows.ProcessNotFoundError, findwindows.WindowNotFoundError) as e:
            logger.error(f" READER: Gagal terhubung ke DWSIM. Pastikan aplikasi berjalan. Error: {e}")
            self.connection_established = False

    def collect_temperature_candidates(self) -> List[TemperatureCandidate]:
        if not self.connection_established: return []
        
        candidates = []
        try:
            self.main_window.set_focus()
            for control in self.main_window.descendants():
                try:
                    text = control.window_text().strip()
                    if re.match(r'^-?\d+\.?\d*$', text):
                        parent = control.parent()
                        context = text
                        if parent and (parent_text := parent.window_text().strip()) and len(parent_text) < 100:
                            context = f"{parent_text} | {context}"
                        candidates.append(TemperatureCandidate(text, context))
                except Exception:
                    continue
        except Exception as e:
            logger.error(f" READER: Error saat mengumpulkan kandidat: {e}")
            self.connection_established = False 
        return candidates

    @staticmethod
    def analyze_candidates(candidates: List[TemperatureCandidate]) -> Optional[TemperatureReading]:
        if not candidates: return None

        if len(candidates) >= DWSIM_READ_LOCKED_INDEX:
            locked_candidate = candidates[DWSIM_READ_LOCKED_INDEX - 1] 
            try:
                val = float(locked_candidate.value)
                logger.info(f" READER: 🔒 Mengunci temperatur dari index #{DWSIM_READ_LOCKED_INDEX}: {val}°C")
                return TemperatureReading(value=val, timestamp=datetime.utcnow(), source="DWSIM_Air_Out",
                                          confidence_score=100, context=locked_candidate.context)
            except ValueError:
                logger.warning(" READER: Kandidat di locked index bukan angka yang valid.")

        candidates.sort(key=lambda x: x.score, reverse=True)
        if candidates and candidates[0].score > 0:
            best_candidate = candidates[0]
            val = float(best_candidate.value)
            logger.info(f" READER: ✅ Memilih temperatur via skor tertinggi (fallback): {val}°C")
            return TemperatureReading(value=val, timestamp=datetime.utcnow(), source="DWSIM_Air_Out",
                                      confidence_score=best_candidate.score, context=best_candidate.context)
        return None

# ==============================================================================
# BAGIAN 3: KELAS UNTUK KONEKSI & UPLOAD DATA
# ==============================================================================

class DataPlatformService:
    """Mengelola koneksi dan pengiriman data ke InfluxDB & ThingsBoard."""
    def __init__(self):
        # Konfigurasi InfluxDB
        self.influx_url = "http://localhost:8086"
        self.influx_token = "1vcaeUZHbZdZsE5tU5-SDRemqEgp2AqM-aLp1cbH294zmOrDbNi0eKhZ8TvZPiA9Vc9dTv6pxjHK_TP5IdRk5Q=="
        self.influx_org = "ITS"
        self.influx_bucket = "SKT"
        self._connect_to_influx()

        # Konfigurasi ThingsBoard MQTT
        self.thingsboard_mqtt_host = "demo.thingsboard.io" 
        self.thingsboard_mqtt_port = 1883
        self.thingsboard_token = "blfdyzip029n3o6x8f8i"
        self.thingsboard_topic = "v1/devices/me/telemetry"
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._connect_to_thingsboard()

    def _connect_to_influx(self):
        try:
            logger.info("☁️  Menghubungkan ke InfluxDB...")
            self.influx_client = InfluxDBClient(url=self.influx_url, token=self.influx_token, org=self.influx_org)
            self.write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)
            logger.info("✅ Koneksi InfluxDB berhasil.")
        except Exception as e:
            logger.error(f"❌ Gagal terhubung ke InfluxDB: {e}")
            self.write_api = None

    def _connect_to_thingsboard(self):
        try:
            logger.info("🛰️  Menghubungkan ke broker ThingsBoard MQTT...")
            self.mqtt_client.username_pw_set(self.thingsboard_token)
            self.mqtt_client.connect(self.thingsboard_mqtt_host, self.thingsboard_mqtt_port, 60)
            self.mqtt_client.loop_start()
            logger.info("✅ Koneksi ThingsBoard MQTT berhasil.")
        except Exception as e:
            logger.error(f"🔥 Gagal terhubung ke ThingsBoard MQTT: {e}")

    def upload_data(self, suhu_in: float, rh: float, suhu_out: Optional[float]):
        """Mengunggah data telemetri ke semua platform dengan struktur yang benar."""
        current_timestamp = int(time.time() * 1000)

        # Kirim ke InfluxDB (Tanpa status Fan & Compressor)
        if self.write_api:
            try:
                records = [
                    Point("process_metrics").tag("stream", "Air_In").field("value", suhu_in),
                    Point("process_metrics").tag("stream", "Temperature").field("value", suhu_in),
                    Point("process_metrics").tag("stream", "Humidity").field("value", rh)
                ]
                if suhu_out is not None:
                    records.append(Point("process_metrics").tag("stream", "Air_Out").field("value", suhu_out))
                
                self.write_api.write(bucket=self.influx_bucket, org=self.influx_org, record=records)
                logger.info(f"🚀 INFLUXDB UPLOAD: AirIn={suhu_in}°C, RH={rh}%, AirOut={suhu_out}°C")
            except Exception as e:
                logger.error(f"🔥 Gagal menulis ke InfluxDB: {e}")

        # Kirim ke ThingsBoard (Dengan tambahan logika status Fan & Compressor)
        if self.mqtt_client.is_connected():
            try:
                # 1. Tentukan status berdasarkan suhu
                if suhu_in > TEMPERATURE_THRESHOLD:
                    fan_status = 1 
                    compressor_status = 1
                else:
                    fan_status = 0
                    compressor_status = 0

                # 2. Siapkan payload untuk ThingsBoard
                payload_values = {
                    "Air_In": suhu_in,
                    "Temperature": suhu_in,
                    "Humidity": rh,
                    "Fan_Status": fan_status,
                    "Compressor_Status": compressor_status
                }
                if suhu_out is not None:
                    payload_values["Air_Out"] = suhu_out
                
                payload = {"ts": current_timestamp, "values": payload_values}
                
                # 3. Kirim payload
                self.mqtt_client.publish(topic=self.thingsboard_topic, payload=json.dumps(payload))
                logger.info(f"🛰️  THINGSBOARD UPLOAD: {json.dumps(payload_values)}")
            except Exception as e:
                logger.error(f"🔥 Gagal mempublikasikan ke ThingsBoard: {e}")

    def stop(self):
        if self.mqtt_client.is_connected():
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            logger.info("Koneksi MQTT ditutup.")
        if hasattr(self, 'influx_client') and self.influx_client:
            self.influx_client.close()
            logger.info("Koneksi InfluxDB ditutup.")

# ==============================================================================
# BAGIAN 4: FUNGSI OTOMATISASI UNTUK MENULIS KE DWSIM
# ==============================================================================

def kirim_suhu_ke_dwsim(app, suhu: float):
    """Mengirimkan nilai suhu ke field input di DWSIM."""
    try:
        main_window = app.top_window()
        air_in_panel = main_window.child_window(title=DWSIM_WRITE_PANEL, control_type="Pane")
        air_in_panel.set_focus()
        
        temp_edit_control = air_in_panel.child_window(auto_id=DWSIM_WRITE_CONTROL_ID, control_type="Edit")
        nilai_suhu_str = f"{suhu:.2f}"
        
        temp_edit_control.set_edit_text(nilai_suhu_str)
        temp_edit_control.type_keys('{ENTER}')

        logger.info(f"✅ DWSIM WRITE: Suhu 'Air_In' diubah menjadi {nilai_suhu_str}°C.")
        return True
    except (findwindows.ElementNotFoundError, findwindows.WindowNotFoundError):
        logger.warning("❌ DWSIM WRITE: Panel atau jendela 'Air_In' tidak ditemukan.")
        return False
    except Exception as e:
        logger.error(f"❌ DWSIM WRITE: Terjadi error tak terduga saat update: {e}")
        return False

# ==============================================================================
# BAGIAN 5: FUNGSI UTAMA & LOOP EKSEKUSI
# ==============================================================================

def main():
    """Fungsi utama untuk menjalankan seluruh proses."""
    suhu_terakhir = None
    kelembapan_terakhir = None
    
    ser, data_platform, dwsim_reader = None, None, None

    try:
        logger.info(" WRITER: Menghubungkan ke DWSIM untuk menulis data...")
        dwsim_writer_app = Application(backend="uia").connect(path=DWSIM_APP_PATH, timeout=10)
        logger.info(" WRITER: Berhasil terhubung ke DWSIM.")

        dwsim_reader = DWSIMReaderService()
        if not dwsim_reader.connection_established:
            raise ConnectionError("Gagal membuat koneksi pembaca DWSIM.")

        logger.info(f"📡 Menghubungkan ke ESP32 di port {SERIAL_PORT}...")
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
        logger.info("✅ Berhasil terhubung ke ESP32.")
        
        data_platform = DataPlatformService()
        
        print("\n" + "="*50)
        print("🟢 Sistem terintegrasi berjalan.")
        print("   Tekan Ctrl+C untuk berhenti.")
        print("="*50 + "\n")
        
        time.sleep(2)
        ser.flushInput()

        waktu_update_berikutnya = time.time()
        
        while True:
            if ser.in_waiting > 0:
                try:
                    clean_line = ser.readline().decode('utf-8').strip()
                    if clean_line.startswith("RH:") and ",T:" in clean_line:
                        parts = clean_line.split(',')
                        kelembapan_terakhir = float(parts[0].split(':')[1])
                        suhu_terakhir = float(parts[1].split(':')[1])
                        print(f"-> DATA DITERIMA | 💧 RH: {kelembapan_terakhir:.1f}% | 🌡️ Suhu: {suhu_terakhir:.1f}°C")
                except (UnicodeDecodeError, IndexError, ValueError):
                    pass 
            
            if time.time() >= waktu_update_berikutnya:
                print("\n--- WAKTUNYA UPDATE ---")
                if suhu_terakhir is not None and kelembapan_terakhir is not None:
                    kirim_suhu_ke_dwsim(dwsim_writer_app, suhu_terakhir)
                    
                    logger.info("...Menunggu DWSIM mengkalkulasi (2 detik)...")
                    time.sleep(2)
                    
                    logger.info("...Membaca nilai Air_Out dari DWSIM...")
                    candidates = dwsim_reader.collect_temperature_candidates()
                    reading_obj = DWSIMReaderService.analyze_candidates(candidates)
                    air_out_suhu = reading_obj.value if reading_obj else None
                    
                    data_platform.upload_data(suhu_in=suhu_terakhir, rh=kelembapan_terakhir, suhu_out=air_out_suhu)
                else:
                    logger.warning("🟡 Belum ada data valid dari sensor untuk dikirim.")
                
                waktu_update_berikutnya = time.time() + UPDATE_INTERVAL
                print("-" * 23 + f"\nMenunggu {UPDATE_INTERVAL} detik...\n")
            
            time.sleep(0.1)

    except findwindows.ProcessNotFoundError:
        logger.critical("\n❌ GAGAL: Proses DWSIM.exe tidak ditemukan.")
    except serial.SerialException:
        logger.critical(f"\n❌ GAGAL: Tidak dapat membuka port serial '{SERIAL_PORT}'.")
    except ConnectionError as e:
        logger.critical(f"\n❌ GAGAL: {e}")
    except KeyboardInterrupt:
        print("\n\n🛑 Program dihentikan oleh pengguna.")
    except Exception as e:
        logger.critical(f"\n❌ TERJADI ERROR KRITIS: {e}", exc_info=True)
    finally:
        if ser and ser.is_open:
            ser.close()
            logger.info("Koneksi serial ditutup.")
        if data_platform:
            data_platform.stop()

if __name__ == "__main__":
    main()