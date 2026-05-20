"""
mark24_rec.py — Schedule-Aware Recorder (Mark24)

Fitur:
  - Sabtu tetap di-fetch (bukan libur)
  - Shutdown via long-press button (5 detik) ATAU kode keypad *0000#
  - Auto-detect IP camera saat ganti WiFi (via mark24_camscan.py)
  - Integrasi Supabase: validasi NIM/NPWP via RPC + tracking rec_session (dosen_id FK)
  - Worker dipanggil di akhir hari; kalau gagal upload, file disimpan (tidak loop)
  - Standby SEPANJANG window jadwal — tidak skip kalau dosen telat masuk
"""

import os, sys, time, subprocess, signal as os_signal, threading, json
from datetime import datetime, date, time as dtime, timedelta

# ─────────────────────────────────────────
# DEPENDENCIES
# ─────────────────────────────────────────
import RPi.GPIO as GPIO
import sounddevice as sd
import soundfile as sf
from RPLCD.i2c import CharLCD

from mark24_supabase import (
    fetch_jadwal_hari_ini,
    parse_jam,
    validate_otp,
    rec_session_start,
    rec_session_stop,
    sync_offline_queue,
)
from mark24_camscan import resolve_camera_ip

# ─────────────────────────────────────────
# PINOUT
# ─────────────────────────────────────────
PIN_BUTTON   = 17
PIN_LED_H    = 27
PIN_LED_M    = 22

ROWS = [5, 6, 13, 26]
COLS = [21, 25, 16, 12]

KEYS = [
    ['1', '2', '3', 'A'],
    ['4', '5', '6', 'B'],
    ['7', '8', '9', 'C'],
    ['*', '0', '#', 'D'],
]

# ─────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────
DOUBLE_CLICK_MAX_MS = 500
LONG_PRESS_SEC      = 5      # tahan button 5 detik untuk shutdown
SHUTDOWN_CODE       = "*0000#"  # kode keypad untuk shutdown (saat standby)

FS                  = 48000
CHANNELS            = 2
DEVICE_ID_AUDIO     = 1

# IP camera default. Mark24 akan auto-detect kalau IP ini tidak responsive.
IP_CAM_DEFAULT      = "192.168.18.251"
SAFETY_CODE         = "L2302A94"
RTSP_USER           = "admin"

QUEUE_DIR           = os.path.expanduser("~/mark24_queue")
SAVED_DIR           = os.path.expanduser("~/mark24_saved")
STATE_DIR           = os.path.expanduser("~/mark24_state")
WORK_SCRIPT         = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "mark24_work.py")

DAILY_REFETCH_HOUR  = 5
DAILY_REFETCH_MIN   = 0

# Berapa lama sebelum jam_mulai window standby terbuka
PRE_STANDBY_MIN     = 5

LCD_ADDRESS = 0x27

# ─────────────────────────────────────────
# STATE FILE — anti-dobel per jadwal per hari
# ─────────────────────────────────────────
def _state_path():
    return os.path.join(STATE_DIR, f"state_{date.today().isoformat()}.json")

def state_load():
    os.makedirs(STATE_DIR, exist_ok=True)
    p = _state_path()
    if not os.path.exists(p):
        return {"completed_ids": []}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {"completed_ids": []}

def state_save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    p   = _state_path()
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)

def state_mark_done(jadwal_id):
    s = state_load()
    if jadwal_id not in s["completed_ids"]:
        s["completed_ids"].append(jadwal_id)
        state_save(s)

def state_is_done(jadwal_id):
    return jadwal_id in state_load()["completed_ids"]

# ─────────────────────────────────────────
# LCD INIT
# ─────────────────────────────────────────
def _init_lcd():
    for charmap in ('A02', 'A00'):
        try:
            obj = CharLCD(
                i2c_expander='PCF8574',
                address=LCD_ADDRESS,
                port=1, cols=16, rows=2, dotsize=8,
                charmap=charmap, auto_linebreaks=False,
            )
            obj.clear()
            return obj
        except Exception as e:
            print(f"[!] LCD init gagal (charmap={charmap}): {e}")
    return None

lcd = _init_lcd()

def lcd_show(line1="", line2=""):
    if lcd is None:
        print(f"[LCD] {line1} | {line2}")
        return
    try:
        lcd.clear()
        lcd.cursor_pos = (0, 0); lcd.write_string(line1[:16].ljust(16))
        lcd.cursor_pos = (1, 0); lcd.write_string(line2[:16].ljust(16))
    except Exception as e:
        print(f"[!] LCD write error: {e}")

# ─────────────────────────────────────────
# GPIO INIT
# ─────────────────────────────────────────
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(PIN_LED_H, GPIO.OUT); GPIO.output(PIN_LED_H, GPIO.LOW)
GPIO.setup(PIN_LED_M, GPIO.OUT); GPIO.output(PIN_LED_M, GPIO.LOW)
for r in ROWS:
    GPIO.setup(r, GPIO.OUT); GPIO.output(r, GPIO.HIGH)
for c in COLS:
    GPIO.setup(c, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ─────────────────────────────────────────
# LED
# ─────────────────────────────────────────
def led_hijau(durasi=None):
    GPIO.output(PIN_LED_H, GPIO.HIGH); GPIO.output(PIN_LED_M, GPIO.LOW)
    if durasi:
        time.sleep(durasi); GPIO.output(PIN_LED_H, GPIO.LOW)

def led_merah(durasi=None):
    GPIO.output(PIN_LED_M, GPIO.HIGH); GPIO.output(PIN_LED_H, GPIO.LOW)
    if durasi:
        time.sleep(durasi); GPIO.output(PIN_LED_M, GPIO.LOW)

def led_off():
    GPIO.output(PIN_LED_H, GPIO.LOW); GPIO.output(PIN_LED_M, GPIO.LOW)

def led_blink_both(times=3, interval=0.2):
    for _ in range(times):
        GPIO.output(PIN_LED_H, GPIO.HIGH); GPIO.output(PIN_LED_M, GPIO.HIGH)
        time.sleep(interval)
        GPIO.output(PIN_LED_H, GPIO.LOW); GPIO.output(PIN_LED_M, GPIO.LOW)
        time.sleep(interval)

# ─────────────────────────────────────────
# KEYPAD
# ─────────────────────────────────────────
def scan_keypad():
    for r_idx, row_pin in enumerate(ROWS):
        GPIO.output(row_pin, GPIO.LOW)
        for c_idx, col_pin in enumerate(COLS):
            if GPIO.input(col_pin) == GPIO.LOW:
                while GPIO.input(col_pin) == GPIO.LOW:
                    time.sleep(0.01)
                GPIO.output(row_pin, GPIO.HIGH)
                return KEYS[r_idx][c_idx]
        GPIO.output(row_pin, GPIO.HIGH)
    return None

def read_keypad_with_shutdown(prompt_line1, deadline_ts=None):
    """
    Baca input keypad, return string atau "SHUTDOWN" atau None (timeout).
    """
    raw = ""   # untuk deteksi shutdown
    buf = ""   # buffer normal digit
    lcd_show(prompt_line1, "")
    while True:
        if deadline_ts and time.time() >= deadline_ts:
            return None

        key = scan_keypad()
        if key is None:
            time.sleep(0.05)
            continue

        raw += key
        if SHUTDOWN_CODE in raw:
            return "SHUTDOWN"
        if len(raw) > 20:
            raw = raw[-20:]

        if key == '#':
            return buf
        elif key == '*':
            buf = buf[:-1]
        elif key in ('A', 'B', 'C', 'D'):
            pass
        else:
            buf += key

        lcd_show(prompt_line1, ('*' * len(buf))[-16:])

# ─────────────────────────────────────────
# BUTTON: SINGLE / DOUBLE / LONG PRESS
# ─────────────────────────────────────────
def wait_button_single(deadline_ts=None):
    """Tunggu button ditekan + dilepas. Return False jika timeout."""
    while GPIO.input(PIN_BUTTON) == GPIO.HIGH:
        if deadline_ts and time.time() >= deadline_ts:
            return False
        time.sleep(0.02)
    while GPIO.input(PIN_BUTTON) == GPIO.LOW:
        time.sleep(0.02)
    return True

def check_double_or_long_click():
    """
    Saat dipanggil dan button sedang LOW:
    - "long": ditekan > LONG_PRESS_SEC
    - "double": dilepas lalu ditekan lagi dalam DOUBLE_CLICK_MAX_MS
    - "single": cuma 1 kali klik biasa
    - "none": button sedang HIGH (tidak ditekan)
    """
    if GPIO.input(PIN_BUTTON) == GPIO.HIGH:
        return "none"

    press_start = time.time()
    while GPIO.input(PIN_BUTTON) == GPIO.LOW:
        if time.time() - press_start >= LONG_PRESS_SEC:
            while GPIO.input(PIN_BUTTON) == GPIO.LOW:
                time.sleep(0.05)
            return "long"
        time.sleep(0.01)

    deadline = time.time() + DOUBLE_CLICK_MAX_MS / 1000.0
    while time.time() < deadline:
        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            while GPIO.input(PIN_BUTTON) == GPIO.LOW:
                time.sleep(0.01)
            return "double"
        time.sleep(0.01)

    return "single"

# ─────────────────────────────────────────
# SANITIZE FILENAME
# ─────────────────────────────────────────
def sanitize_for_filename(s, max_len=30):
    import re
    if not s:
        return "NA"
    s = str(s).strip()
    gelar_patterns = [
        r"^Dr\.?\s+", r"^Prof\.?\s+", r"^Ir\.?\s+", r"^H\.?\s+",
        r",?\s*S\.?Kom\.?$", r",?\s*S\.?T\.?$", r",?\s*S\.?Si\.?$",
        r",?\s*M\.?Kom\.?$", r",?\s*M\.?T\.?$", r",?\s*M\.?Si\.?$",
        r",?\s*M\.?Sc\.?$", r",?\s*Ph\.?D\.?$", r",?\s*M\.?Eng\.?$",
        r",?\s*MBA$", r",?\s*M\.?M\.?$",
    ]
    for pat in gelar_patterns:
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"[\\/:*?\"<>|]", "", s)
    s = re.sub(r"[\s,]+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "NA"

def format_jam_mulai(jam_str):
    if not jam_str:
        return "NA"
    try:
        parts = str(jam_str).split(":")
        h = int(parts[0]); m = int(parts[1]) if len(parts) > 1 else 0
        return f"{h:02d}-{m:02d}"
    except Exception:
        return "NA"

def build_kode_dosen(jadwal):
    """Gabungkan dosen_utama + daftar_dosen jadi 'RLC-DEF-GHI'."""
    import re
    utama = (jadwal.get("dosen_utama") or "").strip()
    daftar = (jadwal.get("daftar_dosen") or "").strip()

    parts_from_daftar = []
    if daftar:
        tokens = re.split(r"[,;/|]\s*|\s{2,}", daftar)
        for t in tokens:
            t = t.strip()
            if t:
                parts_from_daftar.append(t)

    final = []
    seen_lower = set()
    if utama:
        final.append(utama)
        seen_lower.add(utama.lower())
    for p in parts_from_daftar:
        if p.lower() not in seen_lower:
            final.append(p)
            seen_lower.add(p.lower())

    if not final:
        return "NA"

    cleaned = [sanitize_for_filename(p, 20) for p in final]
    cleaned = [c for c in cleaned if c and c != "NA"]
    return "-".join(cleaned) if cleaned else "NA"

def build_final_filename(jadwal, ts_date_obj, ext):
    """Format: YYYY-MM-DD_HH-MM_ruangan_kodematkul_dosen_kelas.ext"""
    tanggal = ts_date_obj.strftime("%Y-%m-%d")
    jam = format_jam_mulai(jadwal.get("jam_mulai"))
    ruangan = sanitize_for_filename(jadwal.get("ruangan"), 20)
    kodemk = sanitize_for_filename(jadwal.get("kode_mata_kuliah"), 20)
    dosen = build_kode_dosen(jadwal)
    kelas = sanitize_for_filename(jadwal.get("kelas"), 20)
    return f"{tanggal}_{jam}_{ruangan}_{kodemk}_{dosen}_{kelas}.{ext}"

# ─────────────────────────────────────────
# RECORDING
# ─────────────────────────────────────────
_stop_flag = threading.Event()

def record_audio(a_tmp):
    try:
        with sf.SoundFile(a_tmp, mode='x', samplerate=FS, channels=CHANNELS) as f:
            with sd.InputStream(samplerate=FS, channels=CHANNELS, device=DEVICE_ID_AUDIO) as stream:
                while not _stop_flag.is_set():
                    data, _ = stream.read(2048)
                    f.write(data)
                    time.sleep(0.001)
    except Exception as e:
        print(f"[!] Error audio: {e}")

# Cache IP camera yang aktif di runtime
_current_cam_ip = None

def get_current_rtsp_url():
    global _current_cam_ip
    if _current_cam_ip is None:
        _current_cam_ip = IP_CAM_DEFAULT
    return f"rtsp://{RTSP_USER}:{SAFETY_CODE}@{_current_cam_ip}:554/cam/realmonitor?channel=1&subtype=0"

def refresh_camera_ip():
    """Cari IP camera saat ini, update _current_cam_ip."""
    global _current_cam_ip
    print("[*] Mencari IP camera...")
    lcd_show("Cari IP camera", "Mohon tunggu...")
    new_ip = resolve_camera_ip(
        last_known_ip=_current_cam_ip or IP_CAM_DEFAULT,
        rtsp_user=RTSP_USER,
        rtsp_pass=SAFETY_CODE,
    )
    if new_ip:
        _current_cam_ip = new_ip
        print(f"[*] Camera IP: {new_ip}")
        return True
    print("[!] Camera tidak ditemukan.")
    return False

def start_recording(jadwal):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    _stop_flag.clear()

    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag   = f"id{jadwal.get('id','x')}"
    v_tmp = os.path.join(QUEUE_DIR, f"vid_{ts}_{tag}.mp4")
    a_tmp = os.path.join(QUEUE_DIR, f"aud_{ts}_{tag}.wav")

    today = date.today()
    final_basename_audio = build_final_filename(jadwal, today, "wav")
    final_basename_video = build_final_filename(jadwal, today, "mp4")
    final_basename_meta  = build_final_filename(jadwal, today, "json")
    final_basename_clean = final_basename_audio.replace(".wav", "_clean.wav")

    rtsp_url = get_current_rtsp_url()
    v_proc = subprocess.Popen([
        'ffmpeg', '-y',
        '-rtsp_transport', 'tcp',
        '-i', rtsp_url,
        '-c', 'copy',
        v_tmp
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    a_thread = threading.Thread(target=record_audio, args=(a_tmp,), daemon=True)
    a_thread.start()

    meta_path = os.path.join(QUEUE_DIR, f"aud_{ts}_{tag}.meta.json")
    try:
        with open(meta_path, "w") as f:
            json.dump({
                "started_at": datetime.now().isoformat(),
                "jadwal": jadwal,
                "audio": os.path.basename(a_tmp),
                "video": os.path.basename(v_tmp),
                "final_basename_audio": final_basename_audio,
                "final_basename_video": final_basename_video,
                "final_basename_clean": final_basename_clean,
                "final_basename_meta":  final_basename_meta,
            }, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Gagal tulis metadata: {e}")

    print(f"[*] Rekam mulai: {ts} | {jadwal.get('mata_kuliah')} ({jadwal.get('kelas')})")
    print(f"[*] Nama upload : {final_basename_audio}")
    return v_proc, a_thread, v_tmp, a_tmp, ts, final_basename_audio, final_basename_video

def stop_recording(v_proc, a_thread, v_tmp, a_tmp):
    print("[*] Menghentikan rekaman...")
    _stop_flag.set()
    a_thread.join(timeout=5)
    try:
        v_proc.send_signal(os_signal.SIGINT)
        v_proc.wait(timeout=10)
    except Exception as e:
        print(f"[!] Error stop ffmpeg: {e}")
        try: v_proc.kill()
        except Exception: pass
    try:
        open(a_tmp + ".done", "w").close()
        open(v_tmp + ".done", "w").close()
    except Exception as e:
        print(f"[!] Error buat .done: {e}")
    print("[*] Rekaman selesai.")

# ─────────────────────────────────────────
# SCHEDULING
# ─────────────────────────────────────────
def jadwal_to_datetime(jadwal, today=None):
    if today is None:
        today = date.today()
    t_mulai   = parse_jam(jadwal.get("jam_mulai"))
    t_selesai = parse_jam(jadwal.get("jam_selesai"))
    if not t_mulai or not t_selesai:
        return None, None
    return datetime.combine(today, t_mulai), datetime.combine(today, t_selesai)

def find_next_active_jadwal(jadwal_list, now=None):
    """
    Cari jadwal yang:
      - belum di-mark done
      - jam_selesai-nya belum lewat
    """
    if now is None:
        now = datetime.now()
    for j in jadwal_list:
        if state_is_done(j.get("id")):
            continue
        dt_mulai, dt_selesai = jadwal_to_datetime(j)
        if not dt_mulai:
            continue
        if now < dt_selesai:
            return j
    return None

def sleep_until(target_dt, status_line1="Standby..."):
    """Sleep sampai target_dt, refresh LCD tiap 30 detik."""
    while True:
        now = datetime.now()
        if now >= target_dt:
            return
        delta = target_dt - now
        total_sec = int(delta.total_seconds())
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        lcd_show(status_line1, f"{h:02d}h{m:02d}m lagi")
        time.sleep(min(30, max(1, total_sec)))

# ─────────────────────────────────────────
# RECORD SESSION
# ─────────────────────────────────────────
def record_session(jadwal, dosen_id):
    """
    Sesi rekam lengkap.
    Stop kondisi: double-click button ATAU jam_selesai tercapai.
    Long-press 5s: shutdown sistem.
    """
    _, dt_selesai = jadwal_to_datetime(jadwal)
    if dt_selesai is None:
        print("[!] Jadwal tidak punya jam_selesai valid, skip.")
        return False

    # Kirim event rec_session_start ke Supabase
    session_id = rec_session_start(jadwal, dosen_id)

    led_hijau()
    lcd_show("Berhasil!", "Merekam...")
    print(f"[*] Mulai rekam. Auto-stop di {dt_selesai.strftime('%H:%M:%S')}")
    time.sleep(2)
    led_off()

    (v_proc, a_thread, v_tmp, a_tmp, ts,
     final_audio_name, final_video_name) = start_recording(jadwal)

    auto_stop = False
    shutdown_requested = False

    while True:
        now = datetime.now()

        if now >= dt_selesai:
            print("[*] Auto-stop: waktu jam_selesai tercapai.")
            auto_stop = True
            break

        sisa = dt_selesai - now
        sisa_min = int(sisa.total_seconds() // 60)
        lcd_show("Rekam (2x=stop)", f"Sisa {sisa_min:>3}m")

        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            click_type = check_double_or_long_click()
            if click_type == "double":
                print("[*] Manual stop (double-click).")
                break
            elif click_type == "long":
                print("[!] Long-press detected → shutdown setelah stop rekam.")
                shutdown_requested = True
                break
            elif click_type == "single":
                led_merah()
                lcd_show("Stop GAGAL!", "Ulangi klik 2x")
                print("[!] Single-click; bukan stop.")
                time.sleep(3)
                led_off()
                continue

        time.sleep(0.1)

    led_off()
    lcd_show("Stop berhasil!" if not auto_stop else "Selesai jadwal!",
             "Menyimpan...")
    stop_recording(v_proc, a_thread, v_tmp, a_tmp)

    # Kirim event rec_session_stop ke Supabase
    stop_reason = "manual_button" if not auto_stop else "auto_schedule"
    if shutdown_requested:
        stop_reason = "manual_admin"
    rec_session_stop(session_id, stop_reason, final_audio_name, final_video_name)

    led_hijau(durasi=1)
    led_off()

    state_mark_done(jadwal.get("id"))

    return shutdown_requested

# ─────────────────────────────────────────
# STANDBY WINDOW
# ─────────────────────────────────────────
def wait_for_user_input(jadwal):
    """
    Tunggu dosen input OTP + konfirmasi button.
    OTP dikirim via Telegram oleh mark24_cron.py sebelum kelas.
    Window aktif: dari NOW sampai jam_selesai.

    Return:
      - dict dosen info kalau OTP valid {dosen_id, kode_dosen, nama_lengkap}
      - None kalau jam_selesai lewat tanpa OTP valid
      - "SHUTDOWN" kalau user ketik kode shutdown
    """
    _, dt_selesai = jadwal_to_datetime(jadwal)
    if not dt_selesai:
        return None

    deadline  = dt_selesai.timestamp()
    mk        = (jadwal.get("mata_kuliah") or "")[:16]
    jadwal_id = jadwal.get("id")

    while time.time() < deadline:
        led_off()
        lcd_show(mk or "Siap rekam", "Input OTP...")

        otp = read_keypad_with_shutdown("Masukkan OTP:", deadline_ts=deadline)

        if otp is None:
            print(f"[!] Window jadwal {mk} berakhir tanpa OTP valid.")
            return None
        if otp == "SHUTDOWN":
            return "SHUTDOWN"

        print(f"[*] OTP diinput: {'*' * len(otp)}")

        lcd_show("Tekan button", "utk konfirmasi")
        ok = wait_button_single(deadline_ts=deadline)
        if not ok:
            print("[!] Timeout konfirmasi.")
            return None

        # Validasi OTP via Supabase
        lcd_show("Verifikasi...", "Mohon tunggu")
        dosen_info = validate_otp(otp, jadwal_id)

        if dosen_info and dosen_info.get("dosen_id"):
            nama = dosen_info.get("nama_lengkap") or dosen_info.get("kode_dosen") or "Dosen"
            print(f"[*] OTP valid! dosen_id={dosen_info.get('dosen_id')} ({nama})")
            led_hijau(durasi=0.5)
            return dosen_info

        # OTP salah / expired / sudah dipakai
        led_merah()
        lcd_show("OTP salah!", "Coba lagi...")
        print("[!] OTP tidak valid / expired / sudah dipakai.")
        time.sleep(3)
        led_off()

    return None

# ─────────────────────────────────────────
# SHUTDOWN
# ─────────────────────────────────────────
def perform_shutdown():
    """Matikan Raspberry Pi dengan bersih."""
    print("[!] SHUTDOWN diminta. Mematikan sistem...")
    led_blink_both(times=5, interval=0.15)
    lcd_show("Mematikan", "sistem...")
    time.sleep(2)
    led_off()
    if lcd:
        try: lcd.clear()
        except Exception: pass
    GPIO.cleanup()

    try:
        subprocess.run(['sudo', '/sbin/shutdown', '-h', 'now'], check=False)
    except Exception as e:
        print(f"[!] Shutdown command gagal: {e}")
        print("[!] Pakai 'sudo shutdown -h now' manual atau cabut listrik.")
    sys.exit(0)

# ─────────────────────────────────────────
# WORKER (END OF DAY)
# ─────────────────────────────────────────
def run_worker_end_of_day():
    lcd_show("Semua selesai", "Mengunggah...")
    print("[*] Menjalankan mark24_work.py...")
    try:
        subprocess.run(['python3', WORK_SCRIPT], check=False)
        print("[*] Worker selesai.")
    except Exception as e:
        print(f"[!] Error worker: {e}")
    lcd_show("Upload selesai", "Sampai besok!")

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    print("=" * 50)
    print("  MARK24 — Schedule-Aware Recorder")
    print("=" * 50)

    # Sync offline queue (event yang gagal kirim di sesi sebelumnya)
    try:
        sync_offline_queue()
    except Exception as e:
        print(f"[!] sync_offline_queue error: {e}")

    # Resolve IP camera saat startup
    refresh_camera_ip()

    worker_already_run_for = None

    try:
        while True:
            jadwal_list = fetch_jadwal_hari_ini()
            today = date.today()
            print(f"[*] {len(jadwal_list)} jadwal untuk {today}")

            # Sync queue offline lagi
            try: sync_offline_queue()
            except Exception: pass

            if not jadwal_list:
                lcd_show("Tidak ada", "kuliah hari ini")
                besok_pagi = datetime.combine(
                    today + timedelta(days=1),
                    dtime(DAILY_REFETCH_HOUR, DAILY_REFETCH_MIN)
                )
                sleep_until(besok_pagi, "Tunggu besok")
                continue

            while True:
                jadwal = find_next_active_jadwal(jadwal_list)
                if jadwal is None:
                    print("[*] Semua jadwal hari ini sudah diproses/lewat.")
                    break

                dt_mulai, dt_selesai = jadwal_to_datetime(jadwal)
                now = datetime.now()

                # Sleep sampai window standby terbuka
                standby_at = dt_mulai - timedelta(minutes=PRE_STANDBY_MIN)
                if now < standby_at:
                    print(f"[*] Tunggu standby {jadwal.get('mata_kuliah')} "
                          f"di {standby_at.strftime('%H:%M')}")
                    label = (jadwal.get("mata_kuliah") or "Kuliah")[:16]
                    sleep_until(standby_at, label)
                    continue

                # Window standby AKTIF
                print(f"\n[*] STANDBY: {jadwal.get('mata_kuliah')} "
                      f"({dt_mulai.strftime('%H:%M')}–{dt_selesai.strftime('%H:%M')})")

                result = wait_for_user_input(jadwal)

                if result == "SHUTDOWN":
                    perform_shutdown()
                    return

                if result is None:
                    # Window berakhir tanpa input valid
                    state_mark_done(jadwal.get("id"))
                    led_merah(durasi=2)
                    lcd_show("Jadwal expired", "Tdk ada input")
                    print(f"[!] Jadwal {jadwal.get('mata_kuliah')} expired (tidak ada input valid).")
                    time.sleep(2)
                    continue

                # Verify camera responsif sebelum rekam
                if not refresh_camera_ip():
                    led_merah(durasi=3)
                    lcd_show("Camera offline!", "Skip rekam.")
                    print("[!] Camera tidak ditemukan, skip jadwal.")
                    state_mark_done(jadwal.get("id"))
                    time.sleep(3)
                    continue

                # Rekam!
                dosen_id = result.get("dosen_id") if isinstance(result, dict) else None
                shutdown_after = record_session(jadwal, dosen_id)

                if shutdown_after:
                    perform_shutdown()
                    return

            # Akhir hari: jalankan worker (sekali saja per hari)
            if worker_already_run_for != today:
                if os.path.isdir(QUEUE_DIR) and any(
                    f.startswith("aud_") and f.endswith(".wav")
                    for f in os.listdir(QUEUE_DIR)
                ):
                    run_worker_end_of_day()
                worker_already_run_for = today

            besok_pagi = datetime.combine(
                today + timedelta(days=1),
                dtime(DAILY_REFETCH_HOUR, DAILY_REFETCH_MIN)
            )
            print(f"[*] Selesai hari ini. Tunggu sampai {besok_pagi}")
            sleep_until(besok_pagi, "Sampai besok")

    except KeyboardInterrupt:
        print("\n[*] Dihentikan oleh user.")

    finally:
        _stop_flag.set()
        led_off()
        lcd_show("Sistem dimatikan", "")
        time.sleep(1)
        if lcd:
            try: lcd.clear()
            except Exception: pass
        GPIO.cleanup()
        print("[*] Cleanup selesai.")


if __name__ == "__main__":
    main()
