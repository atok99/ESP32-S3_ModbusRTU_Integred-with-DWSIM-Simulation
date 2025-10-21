import pyautogui
import pygetwindow as gw
import time
from pywinauto import Application, findwindows
import re
from datetime import datetime
import threading
import json
import logging
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from abc import ABC, abstractmethod

# --- Import untuk InfluxDB & ThingsBoard MQTT ---
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import paho.mqtt.client as mqtt

# Konfigurasi logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class TemperatureReading:
    """Kelas data untuk hasil pembacaan suhu"""
    value: float
    timestamp: datetime
    source: str
    confidence_score: int
    context: str

class TemperatureCandidate:
    """Mewakili kandidat pembacaan suhu yang potensial"""
    def __init__(self, value: str, context: str, control=None):
        self.value = value
        self.context = context
        self.control = control
        self.score = 0
        
    def calculate_score(self) -> int:
        """Menghitung skor kepercayaan untuk kandidat suhu ini"""
        score = 0
        context_lower = self.context.lower()
        
        try:
            val = float(self.value)
            
            if 0.0 <= val <= 1.0: score -= 50
            if val == 0.0: score -= 30
            if 15 <= val <= 200: score += 20
            elif -50 <= val <= 500: score += 10

            context_scores = {
                'air_out': 40, 'temperature': 30, 'stream conditions': 50,
                'material stream': 20, 'input data': 15, '°c': 15,
                'celsius': 15, 'temp': 15
            }
            for keyword, bonus in context_scores.items():
                if keyword in context_lower:
                    score += bonus

            fraction_keywords = ['fraction', 'mole fraction', 'vapor phase mole']
            if any(keyword in context_lower for keyword in fraction_keywords):
                score -= 40

            strong_fraction_keywords = ['vapor phase mole fraction', 'liquid phase mole fraction']
            if any(keyword in context_lower for keyword in strong_fraction_keywords):
                score -= 60
                
        except ValueError:
            score = -100
            
        self.score = score
        return score

class DataCollectionService:
    def __init__(self):
        self.app = None
        self.main_window = None
        self.connection_established = False
        
    def connect_to_dwsim(self) -> bool:
        if self.connection_established and self.app and self.main_window:
            return True

        logger.info("🔗 Menghubungkan ke DWSIM...")
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if 'dwsim' in proc.info['name'].lower() and '.exe' in proc.info['name'].lower():
                        logger.info(f"Proses DWSIM ditemukan: {proc.info['name']} (PID: {proc.info['pid']})")
                        self.app = Application().connect(process=proc.info['pid'])
                        self.main_window = self.app.top_window()
                        window_title = self.main_window.window_text()
                        if 'DWSIM' in window_title:
                            logger.info(f"✅ Terhubung: {window_title}")
                            self.connection_established = True
                            return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                    continue
        except ImportError:
            logger.info("psutil tidak tersedia, menggunakan metode alternatif...")

        dwsim_windows = gw.getWindowsWithTitle('DWSIM')
        if dwsim_windows:
            try:
                self.app = Application().connect(title_re=".*DWSIM.*")
                self.main_window = self.app.top_window()
                logger.info(f"✅ Terhubung via judul window: {self.main_window.window_text()}")
                self.connection_established = True
                return True
            except Exception as e:
                logger.error(f"Koneksi gagal: {e}")
        
        logger.error("Tidak dapat terhubung dengan DWSIM")
        return False

    def collect_temperature_candidates(self) -> List[TemperatureCandidate]:
        if not self.connection_established:
            if not self.connect_to_dwsim():
                return []

        try:
            if self.main_window:
                self.main_window.set_focus()
                time.sleep(0.5)

            all_controls = self.main_window.descendants()
            candidates = []
            for control in all_controls:
                try:
                    text = control.window_text().strip()
                    if self._is_reasonable_temperature(text):
                        context = " | ".join([p.window_text().strip() for p in [control.parent(), control] if p and p.window_text()])
                        if not self._is_likely_fraction(text, context):
                            candidates.append(TemperatureCandidate(text, context, control))
                except Exception:
                    continue
            return candidates
        except Exception as e:
            logger.error(f"Error saat mengumpulkan kandidat suhu: {e}")
            self.connection_established = False
            self.app = None
            self.main_window = None
            return []

    def _is_reasonable_temperature(self, text: str) -> bool:
        try:
            value = float(text)
            return -100 <= value <= 1000
        except (ValueError, TypeError):
            return False

    def _is_likely_fraction(self, value: str, context: str) -> bool:
        try:
            val = float(value)
            context_lower = context.lower()
            if 0.0 <= val <= 1.0 and any(k in context_lower for k in ['fraction', 'mole']):
                return True
        except (ValueError, TypeError):
            pass
        return False

class TemperatureAnalysisService:
    LOCKED_INDEX = 8 

    @staticmethod
    def analyze_candidates(candidates: List[TemperatureCandidate]) -> Optional[TemperatureReading]:
        if not candidates:
            return None

        if len(candidates) >= TemperatureAnalysisService.LOCKED_INDEX:
            locked_candidate = candidates[TemperatureAnalysisService.LOCKED_INDEX - 1]
            try:
                val = float(locked_candidate.value)
                logger.info(f"🔒 Suhu ter-lock: {val}°C dari konteks: {locked_candidate.context}")
                return TemperatureReading(
                    value=val,
                    timestamp=datetime.utcnow(), 
                    source="Heat_Exchanger_Outlet",
                    confidence_score=locked_candidate.calculate_score(), 
                    context=locked_candidate.context
                )
            except ValueError:
                pass
        
        for candidate in candidates:
            candidate.calculate_score()
        candidates.sort(key=lambda x: x.score, reverse=True)

        if candidates and candidates[0].score > 0:
            best_candidate = candidates[0]
            try:
                val = float(best_candidate.value)
                logger.info(f"✅ Dipilih (fallback): {val}°C dengan skor {best_candidate.score}")
                return TemperatureReading(
                    value=val,
                    timestamp=datetime.utcnow(),
                    source="Heat_Exchanger_Outlet",
                    confidence_score=best_candidate.score,
                    context=best_candidate.context
                )
            except ValueError:
                pass
        return None

class DataStorageService:
    def __init__(self):
        self.readings: List[TemperatureReading] = []
        
        # --- Konfigurasi InfluxDB ---
        self.influx_url = "http://localhost:8086"
        self.influx_token = "1vcaeUZHbZdZsE5tU5-SDRemqEgp2AqM-aLp1cbH294zmOrDbNi0eKhZ8TvZPiA9Vc9dTv6pxjHK_TP5IdRk5Q=="
        self.influx_org = "ITS"
        self.influx_bucket = "SKT"
        self.write_api = None
        
        # --- Konfigurasi ThingsBoard MQTT ---
        self.thingsboard_mqtt_host = "demo.thingsboard.io" # UBAH JIKA PERLU (misal: "mqtt.thingsboard.cloud")
        self.thingsboard_mqtt_port = 1883
        self.thingsboard_token = "blfdyzip029n3o6x8f8i"
        self.mqtt_client = None

        # Inisialisasi koneksi InfluxDB
        try:
            logger.info("☁️  Menghubungkan ke InfluxDB...")
            client = InfluxDBClient(url=self.influx_url, token=self.influx_token, org=self.influx_org)
            self.write_api = client.write_api(write_options=SYNCHRONOUS)
            logger.info("✅ Koneksi InfluxDB berhasil.")
        except Exception as e:
            logger.error(f"❌ Gagal terhubung ke InfluxDB: {e}")

        # Inisialisasi dan koneksi ke ThingsBoard via MQTT
        try:
            logger.info("⚡️ Menghubungkan ke ThingsBoard via MQTT...")
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.username_pw_set(self.thingsboard_token)
            self.mqtt_client.connect(self.thingsboard_mqtt_host, self.thingsboard_mqtt_port, 60)
            self.mqtt_client.loop_start()
            logger.info("✅ Koneksi ThingsBoard MQTT berhasil.")

            attributes = {"Nama": "Kelompok 3", "title": "Atok Nopal"}
            self.mqtt_client.publish('v1/devices/me/attributes', json.dumps(attributes))
            logger.info(f"📠 Atribut device terkirim ke ThingsBoard: {json.dumps(attributes)}")
        except Exception as e:
            logger.error(f"❌ Gagal terhubung atau mengirim atribut ke ThingsBoard MQTT: {e}")
            self.mqtt_client = None

    def _upload_to_influx(self, reading: TemperatureReading):
        if not self.write_api: return
        point = Point("process_metrics").tag("stream", "Air_Out").field("value", reading.value).time(reading.timestamp)
        try:
            self.write_api.write(bucket=self.influx_bucket, org=self.influx_org, record=point)
            logger.info(f"🚀 Berhasil diunggah ke InfluxDB: Air_Out value={reading.value:.2f}")
        except Exception as e:
            logger.error(f"🔥 Gagal menulis ke InfluxDB: {e}")

    def _upload_to_thingsboard_mqtt(self, reading: TemperatureReading):
        if not self.mqtt_client or not self.mqtt_client.is_connected():
            logger.warning("Klien ThingsBoard MQTT tidak terhubung. Upload dilewati.")
            return

        payload = {"Air_Out": reading.value}
        
        try:
            topic = 'v1/devices/me/telemetry'
            result = self.mqtt_client.publish(topic, json.dumps(payload))
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                 logger.info(f"🛰️  Berhasil diunggah ke ThingsBoard MQTT: {json.dumps(payload)}")
            else:
                 logger.error(f"🔥 Gagal mengunggah ke ThingsBoard MQTT, kode error: {result.rc}")
        except Exception as e:
            logger.error(f"🔥 Terjadi exception saat mengunggah ke ThingsBoard MQTT: {e}")

    def store_reading(self, reading: TemperatureReading):
        self.readings.append(reading)
        if len(self.readings) > 100:
            self.readings = self.readings[-100:]
        
        self._upload_to_influx(reading)
        self._upload_to_thingsboard_mqtt(reading)

class NotificationService:
    @staticmethod
    def notify_temperature_change(old_temp: Optional[float], new_temp: float):
        if old_temp is not None:
            print(f"🌡️  Suhu Air_Out: {new_temp}°C (Berubah dari {old_temp}°C)")
        else:
            print(f"🌡️  Suhu Air_Out: {new_temp}°C")

    @staticmethod
    def notify_error(message: str):
        print(f"❌ {message}")

    @staticmethod
    def notify_status(message: str):
        print(f"📊 {message}")

class DWSIMTemperatureMonitor:
    def __init__(self):
        self.running = True
        self.debug_mode = True
        self.data_collection = DataCollectionService()
        self.temperature_analysis = TemperatureAnalysisService()
        self.data_storage = DataStorageService()
        self.notification = NotificationService()

    def monitor_loop(self):
        print("🔄 Memulai pemantauan berkelanjutan (setiap 15 detik)")
        print("Tekan Ctrl+C untuk berhenti\n")
        
        iteration = 0
        last_temperature = None
        try:
            while self.running:
                iteration += 1
                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                self.notification.notify_status(f"Pembacaan #{iteration} pada {timestamp_str}")
                print("-" * 50)

                if iteration > 2:
                    self.debug_mode = False

                temperature_reading = self.temperature_analysis.analyze_candidates(
                    self.data_collection.collect_temperature_candidates()
                )

                if temperature_reading:
                    self.data_storage.store_reading(temperature_reading)
                    current_temp = temperature_reading.value
                    if current_temp != last_temperature:
                        self.notification.notify_temperature_change(last_temperature, current_temp)
                        last_temperature = current_temp
                    else:
                        print(f"🌡️  Suhu Air_Out: {current_temp}°C (tidak berubah)")
                else:
                    self.notification.notify_error("Tidak dapat membaca suhu")

                print("-" * 50)
                print(f"⏰ Pembacaan berikutnya dalam 15 detik...\n")

                for _ in range(15):
                    if not self.running: break
                    time.sleep(1)

        except KeyboardInterrupt:
            print("\n🛑 Pemantauan dihentikan oleh pengguna.")
        except Exception as e:
            logger.error(f"Terjadi error pada loop pemantauan: {e}")
        finally:
            self.running = False
            if self.data_storage.mqtt_client:
                self.data_storage.mqtt_client.loop_stop()
                self.data_storage.mqtt_client.disconnect()
                logger.info("🔌 Koneksi ThingsBoard MQTT ditutup.")

    def start_monitoring(self):
        print("DWSIM Temperature Monitor v8.1 - MQTT Edition")
        print("=" * 70)
        print("📡 Protokol: MQTT untuk ThingsBoard, HTTP untuk InfluxDB")
        print(f"🔧 Atribut Device: Nama='Kelompok 3', title='Atok Nopal'")
        print("⏱️  Interval Update: 15 detik")
        print("=" * 70)
        print()

        if self.data_collection.connect_to_dwsim():
            self.monitor_loop()
        else:
            self.notification.notify_error("Tidak dapat terhubung ke DWSIM. Pastikan aplikasi berjalan.")

def main():
    monitor = DWSIMTemperatureMonitor()
    monitor.start_monitoring()

if __name__ == "__main__":
    main()