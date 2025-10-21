use anyhow::{anyhow, Context, Result};
use dotenvy::dotenv;
use reqwest::blocking::Client;
use serde_json::Value;
use std::env;
use std::io::{BufRead, BufReader};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use regex::Regex;
use once_cell::sync::Lazy;

use chrono::{DateTime, Utc};
use csv;
use rumqttc::{MqttOptions, Client as MqttClient, QoS};

// ========================= Regex input serial =========================
static RH_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\bRH\b\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*%").unwrap()
});
static T_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\bT\b\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*°?\s*C").unwrap()
});

struct Pending {
    rh: Option<f64>,
    t: Option<f64>,
}

// ========================= Waktu & helper LP =========================
fn now_nanos() -> i128 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::from_secs(0));
    (now.as_secs() as i128) * 1_000_000_000i128 + (now.subsec_nanos() as i128)
}

fn escape_measurement(s: &str) -> String {
    s.replace(',', r"\,").replace(' ', r"\ ").replace('=', r"\=")
}
fn escape_tag_key_or_value(s: &str) -> String {
    s.replace(',', r"\,").replace(' ', r"\ ").replace('=', r"\=")
}
fn escape_field_key(s: &str) -> String {
    s.replace(',', r"\,").replace(' ', r"\ ").replace('=', r"\=")
}
fn quote_string_field(s: &str) -> String {
    let escaped = s.replace('\\', r"\\").replace('"', r#"\""#);
    format!("\"{}\"", escaped)
}

fn parse_json_fields(s: &str) -> Option<Vec<(String, String)>> {
    let v: Value = serde_json::from_str(s).ok()?;
    let obj = v.as_object()?;
    let mut fields = Vec::new();
    for (k, val) in obj {
        if let Some(n) = val.as_f64() {
            fields.push((escape_field_key(k), format!("{}", n)));
        } else if let Some(b) = val.as_bool() {
            fields.push((escape_field_key(k), format!("{}", b)));
        } else if let Some(st) = val.as_str() {
            fields.push((escape_field_key(k), quote_string_field(st)));
        }
    }
    if fields.is_empty() { None } else { Some(fields) }
}

fn parse_kv_fields(s: &str) -> Option<Vec<(String, String)>> {
    let sep: &[_] = &[',', ' '];
    let parts: Vec<&str> = s.split(sep).filter(|t| !t.is_empty()).collect();
    if parts.is_empty() { return None; }
    let mut got = Vec::new();
    for p in parts {
        if let Some(eq) = p.find('=') {
            let key = &p[..eq];
            let val = &p[eq + 1..];
            if key.is_empty() { continue; }
            let key_esc = escape_field_key(key.trim());
            if let Ok(n) = val.trim().parse::<f64>() {
                got.push((key_esc, format!("{}", n)));
            } else if val.eq_ignore_ascii_case("true") || val.eq_ignore_ascii_case("false") {
                got.push((key_esc, val.to_ascii_lowercase()));
            } else {
                got.push((key_esc, quote_string_field(val.trim())));
            }
        }
    }
    if got.is_empty() { None } else { Some(got) }
}

fn parse_single_number(s: &str) -> Option<Vec<(String, String)>> {
    let trimmed = s.trim();
    if trimmed.is_empty() { return None; }
    if let Ok(n) = trimmed.parse::<f64>() {
        Some(vec![(escape_field_key("value"), format!("{}", n))])
    } else { None }
}

fn line_to_influx(
    measurement: &str,
    default_tag_source: &str,
    raw: &str,
    include_raw_on_fail: bool,
) -> Option<String> {
    let fields_opt = parse_json_fields(raw)
        .or_else(|| parse_kv_fields(raw))
        .or_else(|| parse_single_number(raw));

    let ts = now_nanos();
    let meas = escape_measurement(measurement);
    let tag = escape_tag_key_or_value(default_tag_source);

    if let Some(fields) = fields_opt {
        let fields_join = fields
            .iter()
            .map(|(k, v)| format!("{}={}", k, v))
            .collect::<Vec<_>>()
            .join(",");
        Some(format!("{},source={} {} {}", meas, tag, fields_join, ts))
    } else if include_raw_on_fail {
        let fields_join = format!("raw={}", quote_string_field(raw.trim()));
        Some(format!("{},source={} {} {}", meas, tag, fields_join, ts))
    } else {
        None
    }
}

// ========================= Konfigurasi =========================
struct Config {
    influx_url: String,
    influx_token: String,
    influx_org: String,
    influx_bucket: String,
    measurement: String,
    tag_source: String,
    serial_port: String,
    baudrate: u32,
    include_raw_on_fail: bool,

    tb_host: String,
    tb_port: u16,
    tb_token: String,
    tb_client_id: String,
    tb_use_tls: bool,
}

impl Config {
    fn from_env() -> Result<Self> {
        let influx_url = env::var("INFLUX_URL").context("INFLUX_URL not set")?;
        let influx_token = env::var("INFLUX_TOKEN").context("INFLUX_TOKEN not set")?;
        let influx_org = env::var("INFLUX_ORG").context("INFLUX_ORG not set")?;
        let influx_bucket = env::var("INFLUX_BUCKET").context("INFLUX_BUCKET not set")?;

        Ok(Self {
            influx_url,
            influx_token,
            influx_org,
            influx_bucket,
            measurement: env::var("MEASUREMENT").unwrap_or_else(|_| "sensor".into()),
            tag_source: env::var("TAG_SOURCE").unwrap_or_else(|_| "COM15".into()),
            serial_port: env::var("SERIAL_PORT").unwrap_or_else(|_| "COM15".into()),
            baudrate: env::var("BAUDRATE").ok().and_then(|s| s.parse::<u32>().ok()).unwrap_or(115200),
            include_raw_on_fail: env::var("INCLUDE_RAW_ON_FAIL").map(|v| v == "1" || v.eq_ignore_ascii_case("true")).unwrap_or(true),

            tb_host: env::var("TB_HOST").context("TB_HOST not set")?,
            tb_port: env::var("TB_PORT").ok().and_then(|s| s.parse::<u16>().ok()).unwrap_or(1883),
            tb_token: env::var("TB_TOKEN").context("TB_TOKEN not set")?,
            tb_client_id: env::var("TB_CLIENT_ID").unwrap_or_else(|_| "influx-bridge".into()),
            tb_use_tls: env::var("TB_USE_TLS").map(|v| v == "1" || v.eq_ignore_ascii_case("true")).unwrap_or(false),
        })
    }
}

// ========================= Serial & HTTP =========================
fn open_serial(port: &str, baud: u32) -> Result<Box<dyn serialport::SerialPort>> {
    serialport::new(port, baud)
        .timeout(std::time::Duration::from_secs(2))
        .open()
        .with_context(|| format!("Gagal membuka serial {} @{}", port, baud))
}

fn build_write_url(cfg: &Config) -> String {
    format!(
        "{}/api/v2/write?org={}&bucket={}&precision=ns",
        cfg.influx_url.trim_end_matches('/'),
        urlencoding::encode(&cfg.influx_org),
        urlencoding::encode(&cfg.influx_bucket)
    )
}

fn post_line(client: &Client, cfg: &Config, url: &str, line: &str) -> Result<()> {
    let resp = client
        .post(url)
        .bearer_auth(&cfg.influx_token)
        .header("Content-Type", "text/plain; charset=utf-8")
        .body(line.to_string())
        .send()
        .context("HTTP error saat kirim ke InfluxDB")?;

    if !resp.status().is_success() {
        let code = resp.status();
        let text = resp.text().unwrap_or_default();
        return Err(anyhow!("InfluxDB write failed: {} => {}", code, text));
    }
    Ok(())
}

// ========================= Parser RH/T =========================
fn update_pending_from_line(p: &mut Pending, line: &str) -> Option<(f64, f64)> {
    let mut updated = false;

    if let Some(c) = RH_RE.captures(line) {
        if let Some(m) = c.get(1) {
            if let Ok(v) = m.as_str().parse::<f64>() {
                p.rh = Some(v);
                updated = true;
            }
        }
    }
    if let Some(c) = T_RE.captures(line) {
        if let Some(m) = c.get(1) {
            if let Ok(v) = m.as_str().parse::<f64>() {
                p.t = Some(v);
                updated = true;
            }
        }
    }
    if updated {
        if let (Some(rh), Some(t)) = (p.rh, p.t) {
            return Some((rh, t));
        }
    }
    None
}

// ========================= Query Influx terbaru =========================
#[derive(Debug)]
struct Latest {
    temperature: f64,
    humidity: f64,
    ts_ms: i64,
}

fn query_latest_influx(client: &Client, cfg: &Config) -> Result<Latest> {
    let flux = format!(
        r#"
from(bucket: "{bucket}")
  |> range(start: -7d)
  |> filter(fn: (r) => r["_measurement"] == "{measurement}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time","temperature","humidity"])
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
"#,
        bucket = cfg.influx_bucket,
        measurement = cfg.measurement
    );

    let url = format!(
        "{}/api/v2/query?org={}",
        cfg.influx_url.trim_end_matches('/'),
        urlencoding::encode(&cfg.influx_org)
    );

    let resp = client
        .post(&url)
        .bearer_auth(&cfg.influx_token)
        .header("Accept", "text/csv")
        .header("Content-Type", "application/vnd.flux")
        .body(flux)
        .send()
        .context("HTTP error query InfluxDB")?;

    if !resp.status().is_success() {
        let code = resp.status();
        let text = resp.text().unwrap_or_default();
        return Err(anyhow!("Influx query failed: {} => {}", code, text));
    }

    let text = resp.text().unwrap_or_default();
    let mut rdr = csv::ReaderBuilder::new()
        .has_headers(true)
        .comment(Some(b'#'))
        .from_reader(text.as_bytes());

    let headers = rdr.headers()?.clone();
    let i_time = headers.iter().position(|h| h == "_time")
        .ok_or_else(|| anyhow!("Kolom _time tidak ada"))?;
    let i_temp = headers.iter().position(|h| h == "temperature")
        .ok_or_else(|| anyhow!("Kolom temperature tidak ada"))?;
    let i_hum = headers.iter().position(|h| h == "humidity")
        .ok_or_else(|| anyhow!("Kolom humidity tidak ada"))?;

    for rec in rdr.records() {
        let rec = rec?;
        let t_str = rec.get(i_time).unwrap_or("");
        let temp_str = rec.get(i_temp).unwrap_or("");
        let hum_str = rec.get(i_hum).unwrap_or("");
        if t_str.is_empty() || temp_str.is_empty() || hum_str.is_empty() {
            continue;
        }
        let t_parsed: DateTime<Utc> = t_str.parse().context("Parse _time RFC3339 gagal")?;
        let temp = temp_str.parse::<f64>().context("Parse temperature gagal")?;
        let hum = hum_str.parse::<f64>().context("Parse humidity gagal")?;
        return Ok(Latest { temperature: temp, humidity: hum, ts_ms: t_parsed.timestamp_millis() });
    }

    Err(anyhow!("Tidak ada baris data pada hasil query Influx"))
}

// ========================= Publish ke ThingsBoard =========================
fn publish_to_tb(cfg: &Config, telemetry_json: &str) -> Result<()> {
    let mut mqtt_opts = MqttOptions::new(&cfg.tb_client_id, &cfg.tb_host, cfg.tb_port);
    mqtt_opts.set_credentials(&cfg.tb_token, "");

    if cfg.tb_use_tls {
        mqtt_opts.set_transport(rumqttc::Transport::Tls(rumqttc::TlsConfiguration::default()));
    }

    let (mut client, mut connection) = MqttClient::new(mqtt_opts, 10);

    // jalankan reader di thread lain (biar koneksi keepalive)
    std::thread::spawn(move || {
        for _ in connection.iter() {
            // bisa log jika ingin
        }
    });

    let topic = "v1/devices/me/telemetry";
    client.publish(topic, QoS::AtLeastOnce, false, telemetry_json.as_bytes())
        .context("MQTT publish gagal")?;

    std::thread::sleep(Duration::from_millis(150));
    Ok(())
}

// ========================= MAIN LOOP =========================
fn main() -> Result<()> {
    dotenv().ok();
    let cfg = Config::from_env()?;

    println!(
        "Membaca serial {} @{} dan menulis ke InfluxDB bucket={} org={} measurement={}",
        cfg.serial_port, cfg.baudrate, cfg.influx_bucket, cfg.influx_org, cfg.measurement
    );

    let sp = open_serial(&cfg.serial_port, cfg.baudrate)?;
    let mut reader = BufReader::new(sp);

    let http = Client::builder()
        .timeout(Duration::from_secs(8))
        .build()
        .context("Gagal membuat HTTP client")?;
    let write_url = build_write_url(&cfg);

    let mut buf = String::new();
    let mut pending = Pending { rh: None, t: None };

    loop {
        buf.clear();
        let n = reader.read_line(&mut buf)?;
        if n == 0 { continue; }
        let trimmed = buf.trim();
        if trimmed.is_empty() { continue; }

        // 1) Tangkap format "RH = x%" / "T = y °C" -> kirim ke Influx sebagai satu point
        if let Some((rh, t)) = update_pending_from_line(&mut pending, trimmed) {
            let ts = now_nanos();
            let meas = escape_measurement(&cfg.measurement);
            let tag = escape_tag_key_or_value(&cfg.tag_source);
            let lp = format!("{},source={} temperature={},humidity={} {}",
                             meas, tag, t, rh, ts);

            if let Err(e) = post_line(&http, &cfg, &write_url, &lp) {
                eprintln!("Gagal kirim RH/T ke Influx: {} | {}", e, lp);
            } else {
                println!("OK Influx (RH/T): RH={}%, T={}°C", rh, t);

                // 2) Setelah berhasil masuk ke Influx, ambil data terbaru dari Influx
                match query_latest_influx(&http, &cfg) {
                    Ok(latest) => {
                        // 3) Kirim ke ThingsBoard (payload tanpa ts; TB pakai server time)
                        let json_payload = serde_json::json!({
                            "temperature": latest.temperature,
                            "humidity": latest.humidity
                            // Jika ingin sertakan timestamp:
                            // "ts": latest.ts_ms
                        }).to_string();

                        if let Err(e) = publish_to_tb(&cfg, &json_payload) {
                            eprintln!("Gagal publish ke TB: {}", e);
                        } else {
                            println!("Published to ThingsBoard ✅  {}", json_payload);
                        }
                    }
                    Err(e) => eprintln!("Query Influx terbaru gagal: {}", e),
                }
            }

            // reset pending per pasangan
            pending.rh = None;
            pending.t = None;
            continue;
        }

        // 4) Jika bukan format RH/T, fallback ke parser generik lama
        if let Some(lp) = line_to_influx(&cfg.measurement, &cfg.tag_source, trimmed, cfg.include_raw_on_fail) {
            if let Err(e) = post_line(&http, &cfg, &write_url, &lp) {
                eprintln!("Gagal kirim (generic): {} | {}", e, lp);
            } else {
                println!("OK Influx (generic): {}", trimmed);
            }
        }
    }
}