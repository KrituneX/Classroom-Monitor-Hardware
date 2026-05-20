"""
mark24_test.py — Test diagnostik untuk Mark24.

Cek:
  1. Koneksi Supabase + format hari
  2. Validasi dosen via RPC (dengan kode dummy)
  3. Token Google Drive
  4. IP camera (scan jaringan)
"""
import sys, os
from datetime import date
from mark24_supabase import (
    fetch_jadwal_hari_ini,
    HARI_MAP,
    validate_dosen,
    KELAS_FILTER,
)

def main():
    print("=" * 55)
    print("  MARK24 — Test Diagnostik")
    print("=" * 55)

    today    = date.today()
    hari_now = HARI_MAP[today.weekday()]
    print(f"\n[*] Hari ini: {today} ({hari_now})")
    print(f"[*] KELAS_FILTER: {KELAS_FILTER or '(semua kelas)'}\n")

    # ── Test 1: Fetch jadwal ──
    print("[Test 1] Fetch jadwal hari ini...")
    jadwal = []
    try:
        jadwal = fetch_jadwal_hari_ini(force=True)
        print(f"[V] Berhasil! Dapat {len(jadwal)} jadwal.\n")
        if jadwal:
            print("  Jadwal hari ini:")
            for j in jadwal:
                print(f"  - id={j.get('id')} | {j.get('jam_mulai')}–{j.get('jam_selesai')} | "
                      f"{j.get('mata_kuliah')} | {j.get('kelas')} | "
                      f"dosen: {j.get('dosen_utama')}")
    except Exception as e:
        print(f"[X] Gagal: {e}\n")

    # ── Test 2: Validasi dosen (kalau ada jadwal) ──
    print("\n[Test 2] Test RPC validate_dosen_for_jadwal...")
    if jadwal:
        test_jadwal_id = jadwal[0].get("id")
        test_dosen     = jadwal[0].get("dosen_utama")
        print(f"  Test untuk jadwal id={test_jadwal_id}, dosen utama={test_dosen}")
        print(f"  Memanggil RPC dengan NIM dummy '0000000000'...")
        result = validate_dosen("0000000000", test_jadwal_id)
        if result is None:
            print(f"  [V] RPC bekerja — return None untuk NIM dummy (expected)")
        else:
            print(f"  [!] RPC return: {result}")
            print(f"      (NIM 0000000000 ternyata valid? unusual)")
    else:
        print("  [SKIP] Tidak ada jadwal untuk test.")

    # ── Test 3: Token Drive ──
    print("\n[Test 3] Cek token Google Drive...")
    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.pickle')
    if os.path.exists(token_path):
        print(f"[V] token.pickle ditemukan ({os.path.getsize(token_path)} bytes)")
    else:
        print(f"[X] token.pickle TIDAK ada di {token_path}")
        print("    Copy dari Mark23 atau buat baru via auth_gdrive.py")

    # ── Test 4: IP Camera Scan ──
    print("\n[Test 4] Scan IP camera...")
    try:
        from mark24_camscan import resolve_camera_ip
        ip = resolve_camera_ip(
            last_known_ip="192.168.18.251",
            rtsp_user="admin",
            rtsp_pass="L2302A94",
        )
        if ip:
            print(f"[V] Camera ditemukan di: {ip}")
        else:
            print(f"[X] Camera tidak ditemukan.")
            print("    Cek: kamera nyala, di subnet sama, kredensial benar.")
    except Exception as e:
        print(f"[!] Camera scan error: {e}")

    print("\n" + "=" * 55)
    print("  Test selesai.")
    print("=" * 55)


if __name__ == "__main__":
    main()
