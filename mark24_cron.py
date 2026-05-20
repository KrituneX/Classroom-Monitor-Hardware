"""
mark24_cron.py — OTP Generator & Telegram Sender

Dijalankan via crontab setiap 5 menit.
Tugasnya:
  1. Cek jadwal kuliah yang mulai tepat 1 jam ke depan (toleransi ±5 menit)
  2. Generate OTP 6 digit untuk setiap dosen di jadwal tersebut
  3. Simpan OTP ke tabel jadwal_otp di Supabase
  4. Kirim OTP ke dosen via Telegram bot

Setup crontab (jalankan: crontab -e):
  */5 * * * * /home/dafi/mark24/venv/bin/python /home/dafi/mark24/mark24_cron.py >> /home/dafi/mark24/mark24_cron.log 2>&1

Artinya: cek setiap 5 menit. Kalau ada jadwal yang mulai 55–65 menit lagi, kirim OTP.
Contoh: jadwal jam 09:30 → OTP dikirim antara jam 08:25–08:35 (pertama kali cron
masuk window itu, OTP langsung dikirim dan tidak dikirim lagi dalam 1 jam berikutnya).

──────────────────────────────────────────────────────────
MODE DEBUG / MANUAL:
  # Kirim OTP untuk jadwal_id tertentu sekarang (tanpa cek waktu):
  python3 mark24_cron.py --send <jadwal_id>

  # Kirim OTP ke semua jadwal hari ini sekarang (force, tanpa cek waktu):
  python3 mark24_cron.py --send-all

  # Lihat jadwal hari ini + status OTP masing-masing:
  python3 mark24_cron.py --status

  # Test kirim Telegram ke 1 dosen (cek apakah bot & chat_id benar):
  python3 mark24_cron.py --test-telegram <kode_dosen>
──────────────────────────────────────────────────────────
"""

import os, sys, random, json, re, urllib.request, urllib.error, urllib.parse, argparse
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mark24_supabase import (
    fetch_jadwal_hari_ini,
    parse_jam,
    SUPABASE_URL,
    SUPABASE_KEY,
    _request,
)

# ─────────────────────────────────────────
# CONFIG — USER WAJIB EDIT
# ─────────────────────────────────────────

# Token bot Telegram.
# Cara dapat: DM @BotFather → /newbot → ikuti instruksi → copy token
TELEGRAM_BOT_TOKEN = "GANTI_DENGAN_TOKEN_BOT_KAMU"

# OTP dikirim berapa menit sebelum kelas mulai
SEND_BEFORE_MINUTES = 60    # default: 60 menit sebelum mulai

# Toleransi window pengiriman (±menit dari SEND_BEFORE_MINUTES)
# Karena cron jalan tiap 5 menit, toleransi 5 menit sudah cukup
TOLERANCE_MINUTES = 5

# Panjang OTP (digit)
OTP_LENGTH = 6

# Nama sistem di pesan Telegram
NAMA_SISTEM = "Mark24"

# ─────────────────────────────────────────
# GENERATE OTP
# ─────────────────────────────────────────
def generate_otp(length=OTP_LENGTH):
    return "".join([str(random.randint(0, 9)) for _ in range(length)])

# ─────────────────────────────────────────
# CEK APAKAH OTP SUDAH PERNAH DIKIRIM (anti-dobel)
# ─────────────────────────────────────────
def otp_sudah_dikirim(jadwal_id, dosen_id):
    """
    Return True kalau sudah ada OTP aktif (belum expired & belum dipakai)
    untuk kombinasi jadwal_id + dosen_id.
    Mencegah OTP ganda kalau cron jalan 2x dalam window yang sama.
    """
    try:
        params = urllib.parse.urlencode({
            "jadwal_id": f"eq.{jadwal_id}",
            "dosen_id":  f"eq.{dosen_id}",
            "is_used":   "eq.false",
            "valid_until": f"gt.{datetime.now().isoformat()}",
            "select": "id",
        })
        rows = _request("GET", f"/rest/v1/jadwal_otp?{params}", timeout=10)
        return bool(rows and len(rows) > 0)
    except Exception as e:
        print(f"  [!] Gagal cek OTP existing: {e}")
        return False

# ─────────────────────────────────────────
# SIMPAN OTP KE SUPABASE
# ─────────────────────────────────────────
def simpan_otp(jadwal_id, dosen_id, kode_otp, valid_until_dt):
    """
    Hapus OTP lama yang belum dipakai, lalu insert OTP baru.
    Return True kalau sukses.
    """
    # Hapus OTP lama untuk jadwal+dosen ini
    try:
        params = urllib.parse.urlencode({
            "jadwal_id": f"eq.{jadwal_id}",
            "dosen_id":  f"eq.{dosen_id}",
            "is_used":   "eq.false",
        })
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/jadwal_otp?{params}",
            method="DELETE"
        )
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
    except Exception:
        pass

    # Insert OTP baru
    payload = {
        "jadwal_id":   jadwal_id,
        "dosen_id":    dosen_id,
        "kode_otp":    kode_otp,
        "valid_until": valid_until_dt.isoformat(),
    }
    try:
        _request("POST", "/rest/v1/jadwal_otp", body=payload, timeout=10)
        print(f"  [DB] OTP '{kode_otp}' disimpan (jadwal_id={jadwal_id}, dosen_id={dosen_id})")
        return True
    except Exception as e:
        print(f"  [!] Gagal simpan OTP: {e}")
        return False

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
def kirim_telegram(chat_id, pesan):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       pesan,
        "parse_mode": "HTML",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                print(f"  [TG] ✓ Terkirim ke chat_id={chat_id}")
                return True
            else:
                print(f"  [TG] ✗ Gagal: {body.get('description')}")
                return False
    except urllib.error.HTTPError as e:
        print(f"  [TG] HTTP {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        print(f"  [TG] Error: {e}")
        return False

def buat_pesan_otp(dosen_nama, mata_kuliah, kelas, jam_mulai, jam_selesai, otp, valid_until):
    valid_str = valid_until.strftime("%H:%M")
    return (
        f"🔐 <b>Kode OTP {NAMA_SISTEM}</b>\n\n"
        f"Halo, <b>{dosen_nama}</b>!\n\n"
        f"📚 <b>Mata Kuliah:</b> {mata_kuliah}\n"
        f"🏫 <b>Kelas:</b> {kelas}\n"
        f"🕐 <b>Jam:</b> {jam_mulai}–{jam_selesai}\n\n"
        f"Kode OTP kamu:\n"
        f"<code>{otp}</code>\n\n"
        f"⏰ Berlaku sampai <b>{valid_str}</b>\n"
        f"🔒 Kode ini hanya bisa dipakai <b>1 kali</b>."
    )

# ─────────────────────────────────────────
# AMBIL DATA DOSEN
# ─────────────────────────────────────────
def get_dosen_by_kode(kode_dosen):
    try:
        params = urllib.parse.urlencode({
            "kode_dosen": f"eq.{kode_dosen}",
            "select": "id,kode_dosen,nama_lengkap,telegram_chat_id,aktif",
        })
        rows = _request("GET", f"/rest/v1/dosen?{params}", timeout=10)
        if rows and isinstance(rows, list) and rows:
            return rows[0]
    except Exception as e:
        print(f"  [!] Gagal ambil dosen '{kode_dosen}': {e}")
    return None

def get_semua_dosen_jadwal(jadwal):
    """Parse dosen_utama + daftar_dosen, return list dict dosen."""
    dosen_utama = (jadwal.get("dosen_utama") or "").strip()
    daftar_raw  = (jadwal.get("daftar_dosen") or "").strip()

    kode_list = []
    if dosen_utama:
        kode_list.append(dosen_utama)
    if daftar_raw:
        for k in re.split(r"[,;/|]\s*|\s{2,}", daftar_raw):
            k = k.strip()
            if k and k.lower() not in [x.lower() for x in kode_list]:
                kode_list.append(k)

    result = []
    for kode in kode_list:
        d = get_dosen_by_kode(kode)
        if d:
            result.append(d)
        else:
            print(f"  [!] Dosen '{kode}' tidak ditemukan di tabel dosen.")
    return result

# ─────────────────────────────────────────
# PROSES 1 JADWAL → GENERATE + KIRIM OTP
# ─────────────────────────────────────────
def proses_jadwal(jadwal, force=False):
    """
    Generate & kirim OTP untuk 1 jadwal.
    force=True: skip cek apakah OTP sudah dikirim (untuk debug/manual).
    """
    jadwal_id   = jadwal.get("id")
    mata_kuliah = jadwal.get("mata_kuliah", "?")
    kelas       = jadwal.get("kelas", "?")
    jam_mulai   = jadwal.get("jam_mulai", "?")
    jam_selesai = jadwal.get("jam_selesai", "?")

    t_selesai = parse_jam(jam_selesai)
    valid_until = (
        datetime.combine(date.today(), t_selesai)
        if t_selesai
        else datetime.now() + timedelta(hours=3)
    )

    print(f"\n  ── {mata_kuliah} | {kelas} | {jam_mulai}–{jam_selesai} (id={jadwal_id})")

    dosen_list = get_semua_dosen_jadwal(jadwal)
    if not dosen_list:
        print("  [!] Tidak ada dosen yang terdaftar. Skip.")
        return

    for dosen in dosen_list:
        dosen_id   = dosen.get("id")
        dosen_nama = dosen.get("nama_lengkap", dosen.get("kode_dosen"))
        chat_id    = dosen.get("telegram_chat_id")

        print(f"  Dosen: {dosen.get('kode_dosen')} — {dosen_nama}")

        if not chat_id:
            print(f"  [!] telegram_chat_id kosong. Dosen perlu DM bot terlebih dahulu.")
            print(f"      Cara: minta dosen DM bot → ketik /start → kamu ambil chat_id via getUpdates.")
            continue

        # Anti-dobel: skip kalau OTP sudah aktif (kecuali force)
        if not force and otp_sudah_dikirim(jadwal_id, dosen_id):
            print(f"  [~] OTP sudah pernah dikirim dan masih aktif. Skip (gunakan --send untuk force).")
            continue

        otp = generate_otp()

        if simpan_otp(jadwal_id, dosen_id, otp, valid_until):
            pesan = buat_pesan_otp(dosen_nama, mata_kuliah, kelas,
                                   jam_mulai, jam_selesai, otp, valid_until)
            kirim_telegram(chat_id, pesan)

# ─────────────────────────────────────────
# MODE OTOMATIS (dipanggil crontab)
# ─────────────────────────────────────────
def mode_otomatis():
    """Cek jadwal yang mulai SEND_BEFORE_MINUTES menit lagi (±TOLERANCE_MINUTES)."""
    now = datetime.now()
    jadwal_list = fetch_jadwal_hari_ini()
    if not jadwal_list:
        print("[*] Tidak ada jadwal hari ini.")
        return

    # Hitung window waktu target
    batas_bawah = now + timedelta(minutes=SEND_BEFORE_MINUTES - TOLERANCE_MINUTES)
    batas_atas  = now + timedelta(minutes=SEND_BEFORE_MINUTES + TOLERANCE_MINUTES)

    target = []
    for j in jadwal_list:
        t = parse_jam(j.get("jam_mulai"))
        if not t:
            continue
        dt = datetime.combine(date.today(), t)
        if batas_bawah <= dt <= batas_atas:
            target.append(j)

    if not target:
        print(f"[*] Tidak ada jadwal yang mulai {SEND_BEFORE_MINUTES}±{TOLERANCE_MINUTES} menit lagi.")
        return

    print(f"[*] {len(target)} jadwal masuk window pengiriman OTP:")
    for j in target:
        proses_jadwal(j, force=False)

    print("\n[*] Selesai.")

# ─────────────────────────────────────────
# MODE MANUAL / DEBUG
# ─────────────────────────────────────────
def mode_send(jadwal_id_target):
    """Kirim OTP untuk jadwal_id tertentu sekarang (force)."""
    jadwal_list = fetch_jadwal_hari_ini()
    for j in jadwal_list:
        if str(j.get("id")) == str(jadwal_id_target):
            print(f"[*] Force send OTP untuk jadwal_id={jadwal_id_target}")
            proses_jadwal(j, force=True)
            return
    print(f"[!] Jadwal id={jadwal_id_target} tidak ditemukan di jadwal hari ini.")
    print(f"    Jalankan --status untuk lihat jadwal_id yang tersedia.")

def mode_send_all():
    """Kirim OTP ke semua jadwal hari ini (force)."""
    jadwal_list = fetch_jadwal_hari_ini()
    if not jadwal_list:
        print("[*] Tidak ada jadwal hari ini.")
        return
    print(f"[*] Force send OTP ke {len(jadwal_list)} jadwal:")
    for j in jadwal_list:
        proses_jadwal(j, force=True)
    print("\n[*] Selesai.")

def mode_status():
    """Tampilkan jadwal hari ini + status OTP masing-masing."""
    jadwal_list = fetch_jadwal_hari_ini()
    if not jadwal_list:
        print("[*] Tidak ada jadwal hari ini.")
        return

    now = datetime.now()
    print(f"\n{'ID':>5}  {'Jam':>12}  {'Matkul':<20}  {'Kelas':<12}  {'Dosen':<10}  OTP Status")
    print("-" * 85)

    for j in jadwal_list:
        jadwal_id = j.get("id")
        t_mulai = parse_jam(j.get("jam_mulai"))
        dt_mulai = datetime.combine(date.today(), t_mulai) if t_mulai else None
        selisih = ""
        if dt_mulai:
            delta_min = int((dt_mulai - now).total_seconds() / 60)
            if delta_min > 0:
                selisih = f"(mulai {delta_min}m lagi)"
            elif delta_min > -120:
                selisih = f"(sudah mulai {abs(delta_min)}m lalu)"
            else:
                selisih = "(selesai)"

        jam_str = f"{j.get('jam_mulai','?')[:5]}–{j.get('jam_selesai','?')[:5]}"

        # Cek status OTP di DB
        try:
            params = urllib.parse.urlencode({
                "jadwal_id": f"eq.{jadwal_id}",
                "select":    "kode_otp,is_used,valid_until,dosen_id",
                "order":     "created_at.desc",
                "limit":     "5",
            })
            otps = _request("GET", f"/rest/v1/jadwal_otp?{params}", timeout=10)
        except Exception:
            otps = []

        if not otps:
            otp_status = "Belum dikirim"
        else:
            aktif  = [o for o in otps if not o.get("is_used") and o.get("valid_until", "") > now.isoformat()]
            terpakai = [o for o in otps if o.get("is_used")]
            if aktif:
                otp_status = f"✓ Aktif: {aktif[0].get('kode_otp')} (exp {aktif[0].get('valid_until','')[:16]})"
            elif terpakai:
                otp_status = f"✓ Dipakai: {terpakai[0].get('kode_otp')}"
            else:
                otp_status = "Expired"

        matkul = (j.get("mata_kuliah") or "")[:20]
        kelas  = (j.get("kelas") or "")[:12]
        dosen  = (j.get("dosen_utama") or "")[:10]

        print(f"{jadwal_id:>5}  {jam_str:>12}  {matkul:<20}  {kelas:<12}  {dosen:<10}  {otp_status}")
        if selisih:
            print(f"       {selisih}")

    print()

def mode_test_telegram(kode_dosen):
    """Kirim pesan test ke 1 dosen (verifikasi bot & chat_id benar)."""
    print(f"[*] Test Telegram untuk dosen '{kode_dosen}'...")
    dosen = get_dosen_by_kode(kode_dosen)
    if not dosen:
        print(f"[!] Dosen '{kode_dosen}' tidak ditemukan di tabel dosen.")
        return
    chat_id = dosen.get("telegram_chat_id")
    if not chat_id:
        print(f"[!] telegram_chat_id kosong untuk '{kode_dosen}'.")
        print(f"    Update: UPDATE public.dosen SET telegram_chat_id='<id>' WHERE kode_dosen='{kode_dosen}';")
        return

    pesan = (
        f"✅ <b>Test Mark24 OTP System</b>\n\n"
        f"Halo <b>{dosen.get('nama_lengkap', kode_dosen)}</b>!\n\n"
        f"Koneksi Telegram berhasil.\n"
        f"Bot sudah bisa kirim OTP ke kamu.\n\n"
        f"<i>Pesan ini dikirim saat testing.</i>"
    )
    kirim_telegram(chat_id, pesan)

# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
def main():
    now = datetime.now()
    print("=" * 55)
    print(f"  mark24_cron.py — {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    if TELEGRAM_BOT_TOKEN == "GANTI_DENGAN_TOKEN_BOT_KAMU":
        print("[!] TELEGRAM_BOT_TOKEN belum diset! Edit mark24_cron.py baris ~31.")
        return

    parser = argparse.ArgumentParser(
        description="Mark24 OTP Sender",
        add_help=True,
    )
    parser.add_argument(
        "--send", metavar="JADWAL_ID", type=int,
        help="Kirim OTP untuk jadwal_id tertentu sekarang (force, untuk debug)"
    )
    parser.add_argument(
        "--send-all", action="store_true",
        help="Kirim OTP ke semua jadwal hari ini sekarang (force, untuk debug)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Tampilkan jadwal hari ini + status OTP masing-masing"
    )
    parser.add_argument(
        "--test-telegram", metavar="KODE_DOSEN",
        help="Kirim pesan test ke 1 dosen (verifikasi bot & chat_id)"
    )

    args = parser.parse_args()

    if args.send:
        mode_send(args.send)
    elif args.send_all:
        mode_send_all()
    elif args.status:
        mode_status()
    elif args.test_telegram:
        mode_test_telegram(args.test_telegram)
    else:
        # Mode otomatis (dipanggil crontab)
        mode_otomatis()


if __name__ == "__main__":
    main()

