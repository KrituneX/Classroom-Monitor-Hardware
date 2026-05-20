"""
mark24_supabase.py — Supabase fetcher & integrasi backend untuk Mark24.

Sesuai schema database baru:
  - dosen.nim_npwp_hash (SHA-256)
  - rec_session.dosen_id (FK ke dosen.id)
  - RPC validate_dosen_for_jadwal(p_nim_hash, p_jadwal_id) → int8 (dosen_id) atau NULL

Fitur:
  - Fetch jadwal harian (Senin–Minggu, termasuk Sabtu)
  - Cache lokal untuk hemat bandwidth
  - Validasi NIM/NPWP dosen via RPC
  - Lookup detail dosen (kode_dosen, nama_lengkap) setelah validasi
  - Insert/update rec_session saat rekam start/stop
  - Offline queue untuk event yang gagal kirim
"""

import os, json, hashlib, urllib.request, urllib.parse, urllib.error
from datetime import datetime, date

# ─────────────────────────────────────────
# CONFIG SUPABASE (USER WAJIB EDIT BILA GANTI PROJECT)
# ─────────────────────────────────────────
SUPABASE_URL = "https://sucfuzsxrlcyzlyagwjp.supabase.co"
SUPABASE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN1Y2Z1enN4cmxjeXpseWFnd2pwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQyOTM2MDAsImV4cCI6MjA4OTg2OTYwMH0."
                "ZusM8V0RbSJvXsq1aTH3FvMpOpHRiOc6rYGGHzYNsjI")

TABLE_JADWAL      = "jadwal_kuliah"
TABLE_DOSEN       = "dosen"
TABLE_REC_SESSION = "rec_session"
TABLE_JADWAL_OTP  = "jadwal_otp"
RPC_VALIDATE_OTP  = "validate_otp_for_jadwal"

# Filter kelas (None = ambil semua kelas hari itu)
KELAS_FILTER = None

# Device ID (auto-isi dari hostname kalau None)
DEVICE_ID = None

# ─────────────────────────────────────────
# CACHE & OFFLINE QUEUE
# ─────────────────────────────────────────
CACHE_DIR    = os.path.expanduser("~/mark24_cache")
CACHE_FILE   = os.path.join(CACHE_DIR, "jadwal_hari_ini.json")

OFFLINE_DIR  = os.path.expanduser("~/mark24_offline")
OFFLINE_FILE = os.path.join(OFFLINE_DIR, "pending_events.jsonl")

# Format hari di tabel Supabase (KAPITAL semua).
# Sabtu BUKAN libur — tetap di-fetch.
HARI_MAP = {
    0: "SENIN",
    1: "SELASA",
    2: "RABU",
    3: "KAMIS",
    4: "JUMAT",
    5: "SABTU",
    6: "MINGGU",
}

# ─────────────────────────────────────────
# HTTP HELPER
# ─────────────────────────────────────────
def _request(method, path, body=None, timeout=15):
    """HTTP request ke Supabase. Return parsed JSON atau raise."""
    url = f"{SUPABASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if method in ("POST", "PATCH"):
        req.add_header("Prefer", "return=representation")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body_raw = resp.read().decode("utf-8")
        return json.loads(body_raw) if body_raw else None

# ─────────────────────────────────────────
# FETCH JADWAL
# ─────────────────────────────────────────
def _fetch_jadwal_supabase(hari_text):
    params = {
        "hari": f"eq.{hari_text}",
        "select": "*",
        "order": "jam_mulai.asc",
    }
    if KELAS_FILTER:
        params["kelas"] = f"eq.{KELAS_FILTER}"
    query = urllib.parse.urlencode(params)
    return _request("GET", f"/rest/v1/{TABLE_JADWAL}?{query}")

def _save_cache(jadwal_list, tanggal):
    os.makedirs(CACHE_DIR, exist_ok=True)
    payload = {
        "tanggal": tanggal.isoformat(),
        "fetched_at": datetime.now().isoformat(),
        "jadwal": jadwal_list,
    }
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)
    print(f"[Supabase] Cache disimpan: {len(jadwal_list)} jadwal untuk {tanggal}")

def _load_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"[Supabase] Cache rusak: {e}")
        return None

def _is_cache_fresh(cache, target_date):
    if not cache:
        return False
    try:
        return date.fromisoformat(cache["tanggal"]) == target_date
    except Exception:
        return False

def fetch_jadwal_hari_ini(force=False):
    """Ambil jadwal hari ini. Sabtu juga dicek karena ada matkul kadang."""
    today = date.today()
    hari  = HARI_MAP[today.weekday()]
    cache = _load_cache()

    if not force and _is_cache_fresh(cache, today):
        print(f"[Supabase] Pakai cache untuk {today} ({hari})")
        return cache["jadwal"]

    print(f"[Supabase] Fetch jadwal untuk {today} ({hari})...")
    try:
        jadwal = _fetch_jadwal_supabase(hari)
        _save_cache(jadwal, today)
        return jadwal
    except urllib.error.HTTPError as e:
        print(f"[Supabase] HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        print(f"[Supabase] Network error: {e.reason}")
    except Exception as e:
        print(f"[Supabase] Error: {e}")

    # Failsafe: pakai cache walau beda tanggal (kalau hari sama nama)
    if cache:
        print(f"[Supabase] Fetch gagal, pakai cache lama dari {cache.get('tanggal')}")
        try:
            if HARI_MAP[date.fromisoformat(cache["tanggal"]).weekday()] == hari:
                return cache["jadwal"]
        except Exception:
            pass

    print("[Supabase] Tidak ada data jadwal yang bisa dipakai.")
    return []

def parse_jam(jam_str):
    from datetime import time as dtime
    if not jam_str:
        return None
    try:
        parts = jam_str.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return dtime(h, m, s)
    except Exception:
        return None

# ─────────────────────────────────────────
# VALIDASI DOSEN
# ─────────────────────────────────────────
def hash_nim(nim_str):
    """SHA-256 hex dari NIM/NPWP. Bytes-safe."""
    if nim_str is None:
        return ""
    return hashlib.sha256(str(nim_str).encode("utf-8")).hexdigest()

def _fetch_dosen_detail(dosen_id):
    """Ambil detail dosen by id. Return dict (kode_dosen, nama_lengkap) atau None."""
    try:
        params = urllib.parse.urlencode({
            "id": f"eq.{dosen_id}",
            "select": "id,kode_dosen,nama_lengkap,email,aktif",
        })
        rows = _request("GET", f"/rest/v1/{TABLE_DOSEN}?{params}", timeout=10)
        if rows and isinstance(rows, list) and rows:
            return rows[0]
    except urllib.error.HTTPError as e:
        # 403 (RLS blocked) atau permission lain — kita masih punya dosen_id
        print(f"[Supabase] _fetch_dosen_detail HTTP {e.code} (dosen_id tetap valid)")
    except Exception as e:
        print(f"[Supabase] _fetch_dosen_detail error: {e}")
    return None

def validate_otp(otp_input, jadwal_id):
    """
    Validasi OTP yang diketik dosen di keypad untuk jadwal_id tertentu.

    Memanggil RPC validate_otp_for_jadwal yang:
      1. Cari OTP di tabel jadwal_otp: cocok, belum dipakai, belum expired
      2. Kalau ketemu: tandai is_used=true (one-time use), return dosen_id
      3. Kalau tidak: return NULL

    Return:
      dict {
        "dosen_id": int,
        "kode_dosen": str | None,
        "nama_lengkap": str | None,
      }
      atau None kalau OTP tidak valid / koneksi gagal.
    """
    if not otp_input or not otp_input.strip():
        return None

    payload = {
        "p_kode_otp": otp_input.strip(),
        "p_jadwal_id": jadwal_id,
    }

    try:
        result = _request("POST", f"/rest/v1/rpc/{RPC_VALIDATE_OTP}",
                          body=payload, timeout=10)
    except Exception as e:
        print(f"[Supabase] validate_otp error: {e}")
        return None

    # RPC return int (dosen_id) atau None
    if result is None:
        return None

    try:
        dosen_id = int(result)
    except (TypeError, ValueError):
        print(f"[Supabase] RPC return unexpected: {result}")
        return None

    # Fetch detail dosen (optional — kalau gagal, masih lanjut)
    detail = _fetch_dosen_detail(dosen_id)
    if detail:
        return {
            "dosen_id":     dosen_id,
            "kode_dosen":   detail.get("kode_dosen"),
            "nama_lengkap": detail.get("nama_lengkap"),
        }
    # Fallback kalau RLS blokir SELECT ke tabel dosen
    return {
        "dosen_id":     dosen_id,
        "kode_dosen":   None,
        "nama_lengkap": None,
    }

# ─────────────────────────────────────────
# REC_SESSION
# ─────────────────────────────────────────
def _get_device_id():
    global DEVICE_ID
    if DEVICE_ID:
        return DEVICE_ID
    try:
        import socket
        DEVICE_ID = socket.gethostname()
    except Exception:
        DEVICE_ID = "unknown"
    return DEVICE_ID

def _append_offline(event):
    os.makedirs(OFFLINE_DIR, exist_ok=True)
    with open(OFFLINE_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")
    print(f"[Supabase] Event disimpan ke offline queue: {event.get('action')}")

def rec_session_start(jadwal, dosen_id):
    """
    Insert row baru ke rec_session saat rekam dimulai.
    Trigger di DB otomatis mengisi: keterlambatan, durasi (NULL), kehadiran.

    Args:
        jadwal: dict jadwal dari Supabase
        dosen_id: int dosen.id (hasil dari validate_dosen)

    Return: session_id (int) kalau sukses, None kalau gagal.
    """
    now = datetime.now()
    payload = {
        "jadwal_id":          jadwal.get("id"),
        "dosen_id":           dosen_id,
        "tanggal":            now.date().isoformat(),
        "jam_jadwal_mulai":   jadwal.get("jam_mulai"),
        "jam_jadwal_selesai": jadwal.get("jam_selesai"),
        "started_at":         now.isoformat(),
        "device_id":          _get_device_id(),
        # keterlambatan, durasi, kehadiran diisi otomatis oleh trigger DB
    }

    try:
        result = _request("POST", f"/rest/v1/{TABLE_REC_SESSION}",
                          body=payload, timeout=10)
        if result and isinstance(result, list) and result:
            row        = result[0]
            session_id = row.get("id")
            kehadiran  = row.get("kehadiran", "-")
            telat      = row.get("keterlambatan", "-")
            print(f"[Supabase] rec_session #{session_id} dibuat "
                  f"(dosen_id={dosen_id}, kehadiran={kehadiran}, "
                  f"keterlambatan={telat}).")
            return session_id
    except Exception as e:
        print(f"[Supabase] rec_session_start error: {e}")
        _append_offline({
            "action": "start",
            "payload": payload,
            "queued_at": datetime.now().isoformat(),
        })
    return None

def rec_session_stop(session_id, stop_reason, audio_filename=None, video_filename=None):
    """Update rec_session saat rekam berhenti."""
    payload = {
        "stopped_at":  datetime.now().isoformat(),
        "stop_reason": stop_reason,
    }
    if audio_filename:
        payload["audio_filename"] = audio_filename
    if video_filename:
        payload["video_filename"] = video_filename

    if session_id is None:
        _append_offline({
            "action": "stop_orphan",
            "payload": payload,
            "queued_at": datetime.now().isoformat(),
        })
        return False

    try:
        params = urllib.parse.urlencode({"id": f"eq.{session_id}"})
        _request("PATCH", f"/rest/v1/{TABLE_REC_SESSION}?{params}",
                 body=payload, timeout=10)
        print(f"[Supabase] rec_session #{session_id} di-stop ({stop_reason}).")
        return True
    except Exception as e:
        print(f"[Supabase] rec_session_stop error: {e}")
        _append_offline({
            "action": "stop",
            "session_id": session_id,
            "payload": payload,
            "queued_at": datetime.now().isoformat(),
        })
    return False

def sync_offline_queue():
    """Coba kirim ulang event yang sebelumnya gagal."""
    if not os.path.exists(OFFLINE_FILE):
        return

    try:
        with open(OFFLINE_FILE) as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception:
        return

    if not lines:
        return

    print(f"[Supabase] Sync {len(lines)} event offline...")
    remaining = []
    for line in lines:
        try:
            ev = json.loads(line)
            action = ev.get("action")
            payload = ev.get("payload", {})

            if action == "start":
                _request("POST", f"/rest/v1/{TABLE_REC_SESSION}",
                         body=payload, timeout=10)
            elif action == "stop" and ev.get("session_id"):
                params = urllib.parse.urlencode({"id": f"eq.{ev['session_id']}"})
                _request("PATCH", f"/rest/v1/{TABLE_REC_SESSION}?{params}",
                         body=payload, timeout=10)
            else:
                remaining.append(line)
                continue
        except Exception as e:
            print(f"[Supabase] Sync gagal untuk satu event: {e}")
            remaining.append(line)

    if remaining:
        with open(OFFLINE_FILE, "w") as f:
            for line in remaining:
                f.write(line + "\n")
        print(f"[Supabase] Sync selesai. {len(remaining)} event tertinggal.")
    else:
        os.remove(OFFLINE_FILE)
        print("[Supabase] Sync selesai. Semua event terkirim.")


# ─────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  Test Supabase (Mark24)")
    print("=" * 50)
    jadwal = fetch_jadwal_hari_ini(force=True)
    print(f"\nTotal jadwal hari ini: {len(jadwal)}")
    for j in jadwal:
        print(f"  - id={j.get('id')} | {j.get('jam_mulai')}–{j.get('jam_selesai')} | "
              f"{j.get('mata_kuliah')} | {j.get('kelas')} | "
              f"dosen: {j.get('dosen_utama')} ({j.get('daftar_dosen')})")
