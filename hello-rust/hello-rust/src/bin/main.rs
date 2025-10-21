#![no_std]
#![no_main]

use panic_halt as _;
use esp_hal::{
    Config,
    uart::{Uart, Config as UartConfig},
    DriverMode,
    gpio::{Output, Level, OutputConfig},
};
use esp_println::println;
use esp_hal::time::{Instant, Duration};

esp_bootloader_esp_idf::esp_app_desc!();

// --- Konfigurasi ---
const BAUD: u32 = 9_600; // Gunakan 9600 yang sudah terbukti bekerja untuk sensor
const SID:  u8  = 1;

#[esp_hal::main]
fn main() -> ! {
    let p = esp_hal::init(Config::default());

    // --- Inisialisasi UART ---
    let uart_config = UartConfig::default().with_baudrate(BAUD);
    let mut uart = Uart::new(p.UART1, uart_config)
        .expect("UART1 init failed")
        .with_tx(p.GPIO17)
        .with_rx(p.GPIO18);

    // --- Inisialisasi relay di pin 11 & 15 ---
    let mut relay1 = Output::new(p.GPIO11, Level::Low, OutputConfig::default());
    let mut relay2 = Output::new(p.GPIO15, Level::Low, OutputConfig::default());

    loop {
        let mut rh_val: Option<f32> = None;
        let mut temp_val: Option<f32> = None;
        
        let mut req = [0u8; 8];
        req[0] = SID;
        req[1] = 0x04;

        // 1. MEMBACA KELEMBAPAN (RH)
        req[2..4].copy_from_slice(&0x0001u16.to_be_bytes());
        req[4..6].copy_from_slice(&1u16.to_be_bytes());
        let crc = crc16(&req[..6]);
        req[6..8].copy_from_slice(&crc.to_le_bytes());
        
        let _ = uart.write(&req);
        let _ = uart.flush();
        let (n_rh, rx_buffer) = read_response(&mut uart);
        
        if n_rh >= 7 && (rx_buffer[1] & 0x80) == 0 && rx_buffer[2] == 2 && check_crc(&rx_buffer[..n_rh]) {
            let raw_rh = u16::from_be_bytes([rx_buffer[3], rx_buffer[4]]);
            rh_val = Some(raw_rh as f32 / 10.0);
        }
        
        sleep(Duration::from_millis(100));

        // 2. MEMBACA SUHU
        req[2..4].copy_from_slice(&0x0002u16.to_be_bytes());
        let crc2 = crc16(&req[..6]);
        req[6..8].copy_from_slice(&crc2.to_le_bytes());

        let _ = uart.write(&req);
        let _ = uart.flush();
        let (n_temp, rx_buffer2) = read_response(&mut uart);

        if n_temp >= 7 && (rx_buffer2[1] & 0x80) == 0 && rx_buffer2[2] == 2 && check_crc(&rx_buffer2[..n_temp]) {
            let raw_t = u16::from_be_bytes([rx_buffer2[3], rx_buffer2[4]]);
            temp_val = Some(raw_t as f32 / 10.0);
        }

        // 3. CETAK HASIL + KONTROL RELAY
        match (rh_val, temp_val) {
            (Some(rh), Some(temp)) => {
                println!("RH:{:.1},T:{:.1}", rh, temp);

                if temp > 27.0 {
                    relay1.set_high();
                    relay2.set_high();
                    println!("Relay ON (Temp {:.1} > 27)", temp);
                } else {
                    relay1.set_low();
                    relay2.set_low();
                    println!("Relay OFF (Temp {:.1} <= 27)", temp);
                }
            },
            _ => {
                // Tidak mencetak apa-apa jika gagal agar tidak mengganggu Python
            }
        }
        sleep(Duration::from_millis(2000));
    }
}

// Fungsi helper sama seperti sebelumnya
fn read_response(uart: &mut Uart<'_, impl DriverMode>) -> (usize, [u8; 32]) {
    let mut rx_buffer = [0u8; 32];
    if let Ok(bytes_read) = uart.read(&mut rx_buffer) {
        (bytes_read, rx_buffer)
    } else {
        (0, rx_buffer)
    }
}
fn crc16(data: &[u8]) -> u16 { 
    let mut crc = 0xFFFFu16; 
    for &b in data { 
        crc ^= b as u16; 
        for _ in 0..8 { 
            crc = if (crc & 1) != 0 { 
                (crc >> 1) ^ 0xA001 
            } else { 
                crc >> 1 
            }; 
        } 
    } 
    crc 
}
fn check_crc(frame: &[u8]) -> bool { 
    if frame.len() < 3 { return false; } 
    let crc_index = frame.len() - 2; 
    let received_crc = u16::from_le_bytes([frame[crc_index], frame[crc_index + 1]]); 
    let calculated_crc = crc16(&frame[..crc_index]); 
    received_crc == calculated_crc 
}
#[inline(always)]
fn sleep(dur: Duration) { 
    let start = Instant::now(); 
    while start.elapsed() < dur {} 
}
