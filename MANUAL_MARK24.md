# Manual Mark24 — Setup, Pemakaian, & Operasional

Panduan lengkap untuk Mark24 dengan integrasi Supabase (schema baru: `dosen` + `rec_session`).

## Daftar Isi
1. [Apa yang Baru di Mark24](#1-apa-yang-baru-di-mark24)
2. [Arsitektur & File](#2-arsitektur--file)
3. [Setup Database Supabase](#3-setup-database-supabase)
4. [Deploy Mark24 ke Raspberry Pi](#4-deploy-mark24-ke-raspberry-pi)
5. [Setup Shutdown via Button & Keypad](#5-setup-shutdown-via-button--keypad)
6. [Setup Auto-Start (systemd)](#6-setup-auto-start-systemd)
7. [Cara Pemakaian Sehari-hari](#7-cara-pemakaian-sehari-hari)
8. [Operasional & Troubleshooting](#8-operasional--troubleshooting)
9. [Cheat Sheet](#9-cheat-sheet)

---

## 1. Apa yang Baru di Mark24

| Fitur | Mark23 | Mark24 |
|---|---|---|
| Hari aktif | Senin–Jumat | Senin–Sabtu (Sabtu kadang ada matkul) |
| Validasi NIM/NPWP | Hardcoded di kode (`VALID_CODES`) | Dari tabel Supabase, divalidasi RPC |
| Tracking rekaman | Tidak ada | Insert ke tabel `rec_session` (start/stop) |
| Statistik keterlambatan | Tidak ada | Otomatis (trigger DB hitung `keterlambatan_detik`) |
| IP camera | Hardcoded | Auto-scan subnet kalau IP berubah |
| Shutdown hardware | Tidak ada | Long-press button 5 detik / kode `*0000#` |
| Standby saat dosen telat | Skip kalau lewat 30 menit | **Standby sepanjang jam matkul** |
| Gagal upload | Loop terus | Simpan ke `~/mark24_saved/`, lanjut sesi berikut |
| Sync event saat offline | — | Auto-queue ke `~/mark24_offline/`, sync saat online |

---

## 2. Arsitektur & File

```
┌──────────────────────────────────────────────────────────────┐
│  SUPABASE (cloud)                                            │
│  ├─ jadwal_kuliah     (master jadwal mingguan)               │
│  ├─ dosen             (NIM/NPWP hash + kode_dosen + nama)    │
│  ├─ rec_session       (log mulai/berhenti rekam + telat)     │
│  └─ RPC validate_dosen_for_jadwal(hash, jadwal_id) → dosen_id│
└──────────────────────────────────────────────────────────────┘
                        ▲
                        │ HTTPS
                        │
┌──────────────────────────────────────────────────────────────┐
│  RASPBERRY PI                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  systemd: mark24.service                               │  │
│  │  └─ mark24_rec.py (jalan terus)                        │  │
│  │     ├─ mark24_supabase.py (fetch jadwal, validasi)     │  │
│  │     ├─ mark24_camscan.py (auto-scan IP camera)         │  │
│  │     └─ panggil mark24_work.py end-of-day               │  │
│  │                                                        │  │
│  │  mark24_work.py (ANC + upload Drive)                   │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  Folder data:                                                │
│  ├─ ~/mark24/             (kode + venv + token Drive)        │
│  ├─ ~/mark24_cache/       (cache jadwal harian)              │
│  ├─ ~/mark24_state/       (state harian, anti-dobel)         │
│  ├─ ~/mark24_queue/       (audio/video menunggu upload)      │
│  ├─ ~/mark24_saved/       (file yang gagal upload)           │
│  └─ ~/mark24_offline/     (event Supabase yang gagal kirim)  │
└──────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────┐
│  GOOGLE DRIVE — hasil rekaman final                          │
└──────────────────────────────────────────────────────────────┘
```

### File yang Disediakan

| File | Fungsi |
|---|---|
| `mark24_rec.py` | Main recorder (yang dijalankan systemd) |
| `mark24_work.py` | Worker ANC + upload (dipanggil end-of-day) |
| `mark24_supabase.py` | Module fetch jadwal + validasi dosen + tracking rec_session |
| `mark24_camscan.py` | Module auto-scan IP camera |
| `mark24_test.py` | Script diagnostik |
| `mark24.service` | systemd service file |
| `mark24-shutdown.sudoers` | Konfigurasi izinkan shutdown tanpa password |
| `migration_mark24.sql` | SQL migration untuk Supabase (versi terbaru) |
| `MANUAL_MARK24.md` | Dokumen ini |

---

## 3. Setup Database Supabase

> **Status saat ini:** kamu sudah eksekusi migration SQL. Section ini hanya untuk konteks dan verifikasi.

### A. Struktur Tabel

**Tabel `dosen`:**
| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | bigserial PK | Auto-increment |
| `kode_dosen` | text UNIQUE | "RLC" (match dengan `jadwal_kuliah.dosen_utama`) |
| `nama_lengkap` | text | Untuk display |
| `nim_npwp_hash` | text UNIQUE | SHA-256 dari NIM/NPWP |
| `email` | text nullable | |
| `aktif` | boolean | Soft delete flag |

**Tabel `rec_session`:**
| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | bigserial PK | |
| `jadwal_id` | FK → jadwal_kuliah | |
| `dosen_id` | FK → dosen | Yang trigger rekaman |
| `tanggal` | date | |
| `jam_jadwal_mulai/selesai` | time | Snapshot dari jadwal |
| `started_at` | timestamptz | Jam aktual rekam dimulai |
| `stopped_at` | timestamptz | Jam aktual rekam berhenti |
| `keterlambatan_detik` | int4 | **Diisi trigger** |
| `durasi_detik` | int4 | **Diisi trigger** |
| `stop_reason` | text | `manual_button`/`auto_schedule`/`crash`/`manual_admin` |
| `audio_filename`, `video_filename`, `device_id` | text | Audit trail |

### B. Insert Dosen ke Tabel

Di Supabase Dashboard → SQL Editor, jalankan query berikut untuk setiap dosen:

```sql
INSERT INTO public.dosen (kode_dosen, nama_lengkap, nim_npwp_hash, email)
VALUES (
    'RLC',                                            -- kode 3-huruf
    'Dr. Roberto Lopez Cesario, M.Sc.',               -- nama lengkap
    encode(digest('1103220150', 'sha256'), 'hex'),    -- hash NIM/NPWP
    'rlc@example.ac.id'                               -- email (opsional)
);
```

**Penting:**
- `kode_dosen` harus persis match dengan yang ada di kolom `dosen_utama` atau `daftar_dosen` di tabel `jadwal_kuliah`.
- NIM/NPWP **tidak disimpan plaintext** — hanya SHA-256 hash. Kalau dosen lupa NIM-nya, harus reset (insert dengan NIM baru, update `nim_npwp_hash`).
- Setelah insert semua dosen, jangan lupa kembali ke section di bawah untuk **verifikasi**.

### C. Verifikasi Setup Database

Di SQL Editor:

```sql
-- 1. Cek tabel ada
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_name IN ('dosen', 'rec_session');
-- Harus muncul 2 baris

-- 2. Cek jumlah dosen
SELECT COUNT(*) FROM public.dosen WHERE aktif = true;

-- 3. Cek function ada
SELECT routine_name FROM information_schema.routines
WHERE routine_schema = 'public' AND routine_name = 'validate_dosen_for_jadwal';

-- 4. Test function (ganti dengan jadwal_id real di tabel jadwal_kuliah)
SELECT public.validate_dosen_for_jadwal(
    encode(digest('1103220150', 'sha256'), 'hex'),  -- NIM dosen yang sudah di-insert
    1                                                 -- jadwal_id real
);
-- Hasilnya: dosen_id (integer) kalau valid, NULL kalau tidak

-- 5. Cek RLS aktif untuk tabel sensitif
SELECT tablename, rowsecurity FROM pg_tables
WHERE schemaname = 'public' AND tablename IN ('dosen', 'rec_session');
```

### D. Tempat Edit URL & API Key di Mark24

Buka file `mark24_supabase.py` di Pi, edit baris ~22-26:

```python
SUPABASE_URL = "https://sucfuzsxrlcyzlyagwjp.supabase.co"   # ← URL kamu
SUPABASE_KEY = ("eyJhbGc...")                                # ← anon key kamu
```

URL dan anon key dapat ditemukan di:
**Supabase Dashboard → Project Settings → API**

---

## 4. Deploy Mark24 ke Raspberry Pi

### A. Backup Mark23 (kalau masih ada)

```bash
# Stop service mark23 dulu
sudo systemctl stop mark23 2>/dev/null
sudo systemctl disable mark23 2>/dev/null

# Backup folder
mv ~/mark23 ~/mark23_backup_$(date +%Y%m%d) 2>/dev/null

# Jangan hapus service mark23 dulu — biarkan saja, nanti pakai mark24
```

### B. Buat Folder & Upload File

```bash
mkdir -p ~/mark24
cd ~/mark24

# Upload file (via scp dari laptop atau FileZilla):
#   mark24_rec.py
#   mark24_work.py
#   mark24_supabase.py
#   mark24_camscan.py
#   mark24_test.py
#   mark24.service
#   mark24-shutdown.sudoers
```

Dari laptop pakai scp (ubah username & host):
```bash
scp mark24_*.py mark24.service mark24-shutdown.sudoers dafi@raspberrypi3.local:~/mark24/
```

### C. Copy token Google Drive dari Mark23

```bash
cp ~/mark23_backup_*/token.pickle ~/mark24/
```

Verifikasi:
```bash
ls -la ~/mark24/token.pickle
```

### D. Setup venv & Install Library

```bash
cd ~/mark24
python3 -m venv venv
source venv/bin/activate

pip install RPi.GPIO sounddevice soundfile scipy numpy RPLCD smbus2 \
            google-api-python-client google-auth google-auth-httplib2

deactivate
```

### E. Pastikan ffmpeg + ffprobe Terinstal

`mark24_camscan.py` butuh `ffprobe` untuk validasi RTSP saat scan IP:

```bash
sudo apt update
sudo apt install -y ffmpeg

which ffprobe   # harus ada output
```

### F. Test Diagnostik (PENTING — JANGAN SKIP)

```bash
~/mark24/venv/bin/python ~/mark24/mark24_test.py
```

Output yang diharapkan:
```
[Test 1] Fetch jadwal hari ini...
[V] Berhasil! Dapat N jadwal.
  Jadwal hari ini:
  - id=X | 08:30:00–10:30:00 | FISIKA | TK-49-03 | dosen: RLC

[Test 2] Test RPC validate_dosen_for_jadwal...
  Test untuk jadwal id=X, dosen utama=RLC
  Memanggil RPC dengan NIM dummy '0000000000'...
  [V] RPC bekerja — return None untuk NIM dummy (expected)

[Test 3] Cek token Google Drive...
[V] token.pickle ditemukan

[Test 4] Scan IP camera...
[V] Camera ditemukan di: 192.168.x.x
```

**Kalau ada yang gagal**, jangan lanjut ke service. Fix dulu masalahnya. Lihat troubleshooting di section 8.

---

## 5. Setup Shutdown via Button & Keypad

Tanpa setup ini, **fitur shutdown via hardware tidak akan berfungsi** — Python tidak punya privilege untuk panggil `shutdown`.

### A. Install Sudoers File

```bash
sudo cp ~/mark24/mark24-shutdown.sudoers /etc/sudoers.d/mark24-shutdown
sudo chmod 440 /etc/sudoers.d/mark24-shutdown
sudo visudo -c -f /etc/sudoers.d/mark24-shutdown
```

Output `visudo -c` harus: `parsed OK`.

### B. Test Tanpa Password

Sebagai user `dafi` (bukan root), test:
```bash
sudo -n /sbin/shutdown -h 23:59
```

Kalau **tidak minta password** → ✅ setup OK. Cancel scheduled shutdown:
```bash
sudo shutdown -c
```

Kalau muncul `sudo: a password is required` → sudoers belum aktif. Cek lagi langkah A.

### C. Cara Shutdown Setelah Setup

**Cara 1: Long-press button (5 detik)**
- Tahan button 5 detik **saat sistem standby atau merekam**
- LED kedua berkedip 5x
- LCD "Mematikan sistem..."
- Pi shutdown setelah ~3 detik

**Cara 2: Kode keypad `*0000#`**
- Saat LCD menampilkan "Input kode..." (standby), ketik `*0000#`
- Sistem langsung shutdown

**Cara 3: SSH dari laptop**
```bash
ssh dafi@raspberrypi3.local "sudo shutdown -h now"
```

---

## 6. Setup Auto-Start (systemd)

### A. Install Service

```bash
sudo cp ~/mark24/mark24.service /etc/systemd/system/mark24.service
sudo systemctl daemon-reload
sudo systemctl enable mark24
sudo systemctl start mark24
```

### B. Verifikasi Service Jalan

```bash
sudo systemctl status mark24 --no-pager
```

Yang dicari:
- `Loaded: loaded (...; enabled; ...)` ← akan auto-start saat boot
- `Active: active (running)` ← sedang jalan
- `Main PID: XXXXX (python)` ← proses Python aktif

### C. Lihat Log

```bash
# Real-time
sudo journalctl -u mark24 -f

# Log 50 baris terakhir
sudo journalctl -u mark24 -n 50 --no-pager

# Log file
tail -f ~/mark24/mark24.log
```

### D. Test Auto-Start saat Reboot

```bash
sudo reboot
```

Tunggu ~1 menit, lalu SSH lagi:
```bash
sudo systemctl status mark24 --no-pager
```

Kalau `Active: active (running)` tanpa user intervention → ✅ auto-start sukses.

---

## 7. Cara Pemakaian Sehari-hari

### Skenario A: Hari Normal dengan 2 Jadwal

Misalnya hari Selasa, jadwal:
- 08:30–10:30 FISIKA (dosen RLC)
- 13:00–15:00 KIMIA (dosen DEF)

| Jam | LCD | Yang Terjadi |
|---|---|---|
| 00:00 | (boot) | Pi nyala, service auto-start |
| 00:01 | "FISIKA / 08h29m lagi" | Fetch jadwal, countdown |
| 08:25 | "FISIKA / Input kode..." | Window standby terbuka (5 menit sebelum 08:30) |
| 08:42 | "FISIKA / **********" | Dosen RLC telat, input NIM, tekan `#` |
| 08:42 | "Tekan button / utk konfirmasi" | Tekan button |
| 08:42 | "Verifikasi..." | Mark24 panggil RPC validate_dosen_for_jadwal |
| 08:42 | "Berhasil! / Merekam..." | rec_session insert (started_at=now), rekam mulai |
| 08:42–10:30 | "Rekam (2x=stop) / Sisa Xm" | Rekaman berlangsung |
| 10:30 | "Selesai jadwal!" | Auto-stop, rec_session update (stopped_at) |
| 10:30 | "KIMIA / 02h29m lagi" | Lanjut countdown KIMIA |
| 12:55 | "KIMIA / Input kode..." | Window KIMIA terbuka |
| ... | (siklus berulang) | |
| 15:00+ | "Semua selesai / Mengunggah..." | Worker jalan (ANC + upload semua sesi hari itu) |
| 16:00+ | "Sampai besok" | Sleep sampai besok jam 05:00 |

### Skenario B: Dosen Telat 1.5 Jam (Mark24 vs Mark23)

**Mark23 (lama):** Window standby cuma sampai `jam_selesai + 30 menit`. Kalau dosen telat 35 menit, **jadwal di-skip**.

**Mark24 (baru):** Window standby `[jam_mulai - 5 menit, jam_selesai]`. Selama dosen masuk **sebelum jam_selesai**, masih bisa rekam. Tinggal:
- Telat 5 menit → rekam 1 jam 55 menit
- Telat 1 jam → rekam 1 jam
- Telat 1 jam 50 menit → rekam 10 menit
- Telat lewat jam_selesai → tidak bisa rekam (jadwal expired)

Statistik keterlambatan otomatis terhitung di kolom `rec_session.keterlambatan_detik` (positif = telat, negatif = lebih awal).

### Skenario C: Pindah WiFi (IP Camera Berubah)

1. Kamu pindahkan setup ke ruangan lain dengan WiFi berbeda
2. IP camera berubah dari `192.168.18.251` ke `192.168.5.150` (misalnya)
3. **Saat rekaman berikutnya akan mulai**, sistem panggil `refresh_camera_ip()`:
   - Coba IP last-known → gagal
   - Scan subnet baru (mis. `192.168.5.0/24`)
   - Probe RTSP setiap IP dengan port 554 terbuka
   - Ketemu di `192.168.5.150` → simpan ke cache
4. LCD: "Cari IP camera / Mohon tunggu..." → "Berhasil! / Merekam..."
5. Selesai. Tidak perlu edit kode/restart service.

Manual scan (kalau mau test sebelum rekam):
```bash
~/mark24/venv/bin/python ~/mark24/mark24_camscan.py
```

### Skenario D: Internet Mati Saat Rekaman

1. Rekaman jalan normal di Pi
2. Internet putus di tengah → `rec_session_start` gagal kirim ke Supabase
3. Event di-queue ke `~/mark24_offline/pending_events.jsonl`
4. Rekaman tetap berjalan (lokal tidak terganggu)
5. Saat internet pulih, `sync_offline_queue()` dipanggil:
   - Setiap fetch jadwal (1x sehari + saat boot)
   - Mark24 coba kirim ulang event yang queued
6. Kalau sukses → file offline queue dibersihkan

### Skenario E: Gagal Upload Drive

1. End-of-day, worker jalan
2. ANC sukses tapi upload Drive gagal (token expired, internet putus, dll)
3. Setelah 3x retry, **file dipindah** ke `~/mark24_saved/` (BUKAN dihapus, BUKAN diloop)
4. Worker lanjut ke sesi berikutnya
5. Log: `[!] N file disimpan di ~/mark24_saved/`

Upload manual file saved:
```bash
# Pindah balik ke queue
mv ~/mark24_saved/* ~/mark24_queue/

# Jalankan worker lagi
~/mark24/venv/bin/python ~/mark24/mark24_work.py
```

---

## 8. Operasional & Troubleshooting

### Cek Statistik Keterlambatan Dosen (di Supabase)

SQL Editor:

```sql
SELECT
    d.kode_dosen,
    d.nama_lengkap,
    COUNT(*)                                          AS total_sesi,
    ROUND(AVG(rs.keterlambatan_detik) / 60.0, 1)      AS rata_telat_menit,
    MAX(rs.keterlambatan_detik / 60)                  AS telat_terlama_menit,
    SUM(CASE WHEN rs.keterlambatan_detik > 600 THEN 1 ELSE 0 END) AS telat_lebih_10min
FROM public.rec_session rs
JOIN public.dosen d ON d.id = rs.dosen_id
WHERE rs.tanggal >= CURRENT_DATE - INTERVAL '30 days'
  AND rs.stopped_at IS NOT NULL
GROUP BY d.kode_dosen, d.nama_lengkap
ORDER BY rata_telat_menit DESC;
```

### Cek Sesi yang Belum Stop (Crash Mid-Recording?)

```sql
SELECT rs.id, rs.tanggal, rs.started_at, d.kode_dosen, jk.mata_kuliah, jk.kelas
FROM public.rec_session rs
JOIN public.dosen d ON d.id = rs.dosen_id
JOIN public.jadwal_kuliah jk ON jk.id = rs.jadwal_id
WHERE rs.stopped_at IS NULL
  AND rs.started_at < now() - INTERVAL '4 hours'
ORDER BY rs.started_at DESC;
```

### Validasi Dosen Selalu Gagal

1. **Cek hash di Mark24 vs SQL** harus identik:
```bash
python3 -c "import hashlib; print(hashlib.sha256(b'1103220150').hexdigest())"
```
```sql
SELECT encode(digest('1103220150', 'sha256'), 'hex');
```
Output keduanya harus sama. Kalau beda → ada whitespace/typo di salah satu.

2. **Cek kode_dosen di tabel `dosen` match dengan `jadwal_kuliah.dosen_utama`:**
```sql
-- Cek dosen yang ada di tabel
SELECT kode_dosen FROM public.dosen WHERE aktif = true;

-- Cek dosen yang ada di jadwal hari ini
SELECT dosen_utama, daftar_dosen FROM public.jadwal_kuliah WHERE hari = 'SENIN';
```
Setiap `dosen_utama` (dan setiap kode di `daftar_dosen`) harus ada di tabel `dosen`. Kalau tidak, validasi akan gagal.

3. **Test RPC langsung:**
```sql
SELECT public.validate_dosen_for_jadwal(
    encode(digest('NIM_DOSEN_REAL', 'sha256'), 'hex'),
    JADWAL_ID_REAL
);
```
Kalau return NULL: NIM tidak terdaftar / kode dosen tidak ada di jadwal.
Kalau return angka (dosen_id): valid.

### IP Camera Tidak Ketemu

```bash
# Test scan manual
~/mark24/venv/bin/python ~/mark24/mark24_camscan.py
```

Kalau tidak ketemu:
- Pastikan kamera nyala dan terhubung WiFi
- Pi dan kamera di subnet sama: `ip route` (Pi) dan cek IP kamera di router admin
- Test RTSP manual: `ffplay rtsp://admin:L2302A94@<IP>:554/cam/realmonitor?channel=1&subtype=0`
- Cek firewall di kamera tidak blok port 554

### Force Refetch Jadwal (Sudah Update di Supabase Tengah Hari)

```bash
sudo systemctl stop mark24
rm ~/mark24_cache/jadwal_hari_ini.json
sudo systemctl start mark24
```

### Reset State Hari Ini (Mau Rekam Ulang Jadwal yang Sudah Done)

```bash
# Lihat state
cat ~/mark24_state/state_$(date +%Y-%m-%d).json

# Edit, hapus jadwal_id yang mau di-reset
nano ~/mark24_state/state_$(date +%Y-%m-%d).json

# Restart
sudo systemctl restart mark24
```

### Service Restart-Loop

```bash
sudo journalctl -u mark24 -n 100 --no-pager
```

Cari error pattern:
- `ModuleNotFoundError` → library hilang, install di venv
- `Permission denied` → ownership salah, `sudo chown -R dafi:dafi ~/mark24`
- `RTSP error` → camera tidak ada, sistem skip dengan benar
- `IndentationError` → file rusak saat upload, re-upload

### Mark23 Masih Aktif (Konflik dengan Mark24)

```bash
# Pastikan mark23 stopped & disabled
sudo systemctl stop mark23
sudo systemctl disable mark23

# Hapus service file (optional)
sudo rm /etc/systemd/system/mark23.service
sudo systemctl daemon-reload
```

---

## 9. Cheat Sheet

```bash
# === STATUS & CONTROL ===
sudo systemctl status mark24 --no-pager     # cek status
sudo systemctl start|stop|restart mark24    # control
sudo journalctl -u mark24 -f                # log realtime

# === DEBUG ===
~/mark24/venv/bin/python ~/mark24/mark24_test.py      # diagnostik lengkap
~/mark24/venv/bin/python ~/mark24/mark24_camscan.py   # scan IP cam
~/mark24/venv/bin/python ~/mark24/mark24_rec.py       # run manual (stop service dulu)

# === DATA RESET ===
rm ~/mark24_cache/jadwal_hari_ini.json                # force refetch jadwal
rm ~/mark24_state/state_$(date +%Y-%m-%d).json        # reset state hari ini
rm ~/mark24_offline/pending_events.jsonl              # bersihkan offline queue (HATI-HATI)

# === RECOVERY GAGAL UPLOAD ===
mv ~/mark24_saved/* ~/mark24_queue/
~/mark24/venv/bin/python ~/mark24/mark24_work.py

# === SHUTDOWN ===
sudo shutdown -h now                                  # via SSH
# atau tahan button 5 detik
# atau ketik *0000# di keypad saat standby

# === SUPABASE QUERY UMUM ===
# (di SQL Editor Supabase Dashboard)
# Lihat dosen aktif:
#   SELECT kode_dosen, nama_lengkap FROM public.dosen WHERE aktif = true;
#
# Lihat sesi hari ini:
#   SELECT * FROM public.rec_session WHERE tanggal = CURRENT_DATE ORDER BY started_at;
#
# Add new dosen:
#   INSERT INTO public.dosen (kode_dosen, nama_lengkap, nim_npwp_hash, email)
#   VALUES ('XYZ', 'Nama Dosen', encode(digest('NIM_BARU', 'sha256'), 'hex'), 'email');
```
