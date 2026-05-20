"""
mark24_camscan.py — IP Camera Auto-Detect

Mencari IP camera RTSP di subnet lokal saat IP berubah
(pindah WiFi atau kamera dapat IP baru dari DHCP).

Strategi:
  1. Coba IP terakhir yang tersimpan (dari config/cache) — kalau responsif, pakai itu.
  2. Kalau tidak responsif: deteksi subnet lokal Pi.
  3. Scan paralel semua IP di subnet untuk port 554 (RTSP).
  4. Untuk IP yang buka port 554, probe RTSP dengan kredensial.
  5. Cache hasil yang valid untuk dipakai run berikutnya.

Cara pakai dari mark24_rec.py:
    from mark24_camscan import resolve_camera_ip

    ip = resolve_camera_ip(
        last_known_ip="192.168.18.251",
        rtsp_user="admin",
        rtsp_pass="L2302A94",
    )
    if ip:
        rtsp_url = f"rtsp://admin:L2302A94@{ip}:554/cam/realmonitor?channel=1&subtype=0"
"""

import os, socket, subprocess, json, ipaddress, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
RTSP_PORT          = 554
SCAN_TIMEOUT_SEC   = 0.5    # timeout per IP saat scan port
RTSP_PROBE_TIMEOUT = 5      # timeout ffprobe RTSP
MAX_PARALLEL_SCAN  = 50     # paralel thread saat scan subnet
CACHE_FILE         = os.path.expanduser("~/mark24_cache/last_camera_ip.json")

# ─────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────
def _save_cache(ip):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump({"ip": ip, "saved_at": time.time()}, f)
    except Exception as e:
        print(f"[CamScan] Gagal simpan cache: {e}")

def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f).get("ip")
    except Exception:
        return None

# ─────────────────────────────────────────
# DETEKSI SUBNET LOKAL
# ─────────────────────────────────────────
def _get_local_subnet():
    """
    Deteksi subnet aktif Pi. Return ipaddress.IPv4Network atau None.

    Strategi:
      1. Cari IP Pi via socket trick (connect ke 8.8.8.8 tidak benar-benar kirim packet)
      2. Asumsikan netmask /24 (paling umum di home/office WiFi)
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()

        # Asumsikan /24 (192.168.x.0/24, 10.0.x.0/24, dll)
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return net, local_ip
    except Exception as e:
        print(f"[CamScan] Gagal deteksi subnet: {e}")
        return None, None

# ─────────────────────────────────────────
# PROBE PORT & RTSP
# ─────────────────────────────────────────
def _is_port_open(ip, port=RTSP_PORT, timeout=SCAN_TIMEOUT_SEC):
    """TCP connect probe ke ip:port. Return True kalau respond."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((str(ip), port))
        s.close()
        return result == 0
    except Exception:
        return False

def _probe_rtsp(ip, user, password, timeout=RTSP_PROBE_TIMEOUT):
    """
    Coba akses RTSP dengan kredensial. Return True kalau berhasil.
    Pakai ffprobe (cepat, tidak butuh download stream).
    """
    rtsp_url = f"rtsp://{user}:{password}@{ip}:{RTSP_PORT}/cam/realmonitor?channel=1&subtype=0"
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-rtsp_transport", "tcp",
                "-timeout", str(timeout * 1_000_000),  # microsecond
                "-i", rtsp_url,
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1",
            ],
            timeout=timeout + 2,
            capture_output=True,
        )
        # Sukses kalau exit code 0 dan ada stream
        if result.returncode == 0 and result.stdout:
            return True
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        print("[CamScan] ffprobe tidak ditemukan. Install dengan: apt install ffmpeg")
    except Exception as e:
        print(f"[CamScan] Probe error pada {ip}: {e}")
    return False

# ─────────────────────────────────────────
# SCAN SUBNET
# ─────────────────────────────────────────
def _scan_subnet_for_open_port(network, exclude_ip=None):
    """
    Scan paralel subnet untuk IP yang buka port RTSP.
    Return list IP yang buka port 554.
    """
    ips = [ip for ip in network.hosts() if str(ip) != exclude_ip]
    open_ips = []

    print(f"[CamScan] Scanning {len(ips)} IP di {network} untuk port {RTSP_PORT}...")
    start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SCAN) as executor:
        futures = {executor.submit(_is_port_open, ip): ip for ip in ips}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                if future.result():
                    open_ips.append(str(ip))
                    print(f"  [+] Port {RTSP_PORT} terbuka: {ip}")
            except Exception:
                pass

    elapsed = time.time() - start
    print(f"[CamScan] Scan selesai dalam {elapsed:.1f}s. {len(open_ips)} kandidat.")
    return open_ips

# ─────────────────────────────────────────
# MAIN FUNCTION
# ─────────────────────────────────────────
def resolve_camera_ip(last_known_ip, rtsp_user, rtsp_pass, allow_scan=True):
    """
    Cari IP camera yang valid.

    Args:
        last_known_ip: IP yang dipakai terakhir (dari config)
        rtsp_user, rtsp_pass: kredensial kamera
        allow_scan: kalau False, hanya coba IP last_known + cached (tidak scan subnet)

    Return: IP string yang valid, atau None kalau tidak ketemu.
    """
    # 1. Coba IP last-known
    candidates = []
    if last_known_ip:
        candidates.append(last_known_ip)

    # 2. Coba IP dari cache (kalau berbeda dari last_known)
    cached_ip = _load_cache()
    if cached_ip and cached_ip not in candidates:
        candidates.append(cached_ip)

    for ip in candidates:
        print(f"[CamScan] Coba IP {ip}...")
        if _is_port_open(ip):
            if _probe_rtsp(ip, rtsp_user, rtsp_pass):
                print(f"[CamScan] ✓ IP {ip} responsive!")
                _save_cache(ip)
                return ip
            else:
                print(f"[CamScan] Port {ip}:554 terbuka tapi RTSP auth gagal.")
        else:
            print(f"[CamScan] Port {ip}:{RTSP_PORT} tidak respond.")

    if not allow_scan:
        return None

    # 3. Scan subnet lokal
    network, local_ip = _get_local_subnet()
    if not network:
        print("[CamScan] Tidak bisa deteksi subnet, scan dibatalkan.")
        return None

    print(f"[CamScan] Pi di {local_ip}, scan subnet {network}...")
    open_ips = _scan_subnet_for_open_port(network, exclude_ip=local_ip)

    # Filter: skip IP yang sudah dicoba di step 1-2
    open_ips = [ip for ip in open_ips if ip not in candidates]

    if not open_ips:
        print("[CamScan] Tidak ada IP dengan port RTSP terbuka.")
        return None

    # 4. Probe RTSP tiap kandidat
    for ip in open_ips:
        print(f"[CamScan] Probe RTSP di {ip}...")
        if _probe_rtsp(ip, rtsp_user, rtsp_pass):
            print(f"[CamScan] ✓ Kamera ditemukan di {ip}!")
            _save_cache(ip)
            return ip
        else:
            print(f"[CamScan] {ip}: bukan kamera kita (auth gagal).")

    print("[CamScan] Tidak ada kamera yang cocok dengan kredensial.")
    return None


# ─────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────
if __name__ == "__main__":
    import sys
    print("=" * 50)
    print("  Test IP Camera Scanner")
    print("=" * 50)

    last = sys.argv[1] if len(sys.argv) > 1 else "192.168.18.251"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    pwd  = sys.argv[3] if len(sys.argv) > 3 else "L2302A94"

    ip = resolve_camera_ip(last, user, pwd)
    if ip:
        print(f"\n✓ KAMERA DITEMUKAN: {ip}")
        print(f"  RTSP URL: rtsp://{user}:{pwd}@{ip}:554/cam/realmonitor?channel=1&subtype=0")
    else:
        print("\n✗ KAMERA TIDAK DITEMUKAN.")
        print("  Cek:")
        print("    - Kamera nyala dan terkoneksi ke WiFi/LAN")
        print("    - Pi dan kamera di subnet yang sama")
        print("    - Kredensial RTSP benar")
