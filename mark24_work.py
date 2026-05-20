"""
mark24_work.py — End-of-Day Worker

Perubahan dari Mark23:
  - Kalau ANC gagal: file audio asli tetap di-upload (skip clean)
  - Kalau upload gagal: file dipindah ke ~/mark24_saved/ (tidak loop, tidak hapus)
  - File yang di-saved bisa di-upload manual nanti
"""

import os, time, pickle, glob, signal, json, shutil
import numpy as np
from scipy import signal as scipy_signal
import soundfile as sf
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
QUEUE_DIR       = os.path.expanduser("~/mark24_queue")
SAVED_DIR       = os.path.expanduser("~/mark24_saved")  # file yang gagal upload disimpan di sini
GDRIVE_ID       = "1wO1XfC7lUeGhCy9hLvfo4tYn-I1rVD0b"
MAX_RETRY       = 3

ANC_MODE        = "numpy"
ANC_CHUNK_SEC   = 30
ANC_CHUNK_SLEEP = 0.03

# ─────────────────────────────────────────
# SIGNAL
# ─────────────────────────────────────────
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    print(f"\n[*] Signal {signum} — graceful shutdown...")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ─────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────
def progress_bar(label, current, total, bar_width=28):
    pct    = int(current / total * 100) if total > 0 else 100
    filled = int(bar_width * pct / 100)
    bar    = "█" * filled + "░" * (bar_width - filled)
    print(f"\r  [{bar}] {pct:>3}%  {label}", end="", flush=True)
    if pct >= 100:
        print()

def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return None

def check_temp():
    temp = get_cpu_temp()
    if temp is None:
        return
    icon = "OK" if temp < 60 else ("HANGAT" if temp < 75 else "PANAS!")
    print(f"  [CPU] {temp:.1f}C [{icon}]")
    if temp >= 75:
        print("  [!] Jeda 15 detik untuk dinginkan CPU...")
        time.sleep(15)
    elif temp >= 65:
        time.sleep(5)

def save_to_saved_dir(file_path, reason):
    """Pindahkan file ke ~/mark24_saved/ — tidak hapus, untuk recovery manual."""
    if not os.path.exists(file_path):
        return
    os.makedirs(SAVED_DIR, exist_ok=True)
    fname = os.path.basename(file_path)
    target = os.path.join(SAVED_DIR, fname)
    # Kalau target sudah ada, tambah suffix
    if os.path.exists(target):
        ts = int(time.time())
        target = os.path.join(SAVED_DIR, f"{ts}_{fname}")
    try:
        shutil.move(file_path, target)
        print(f"  [SAVED] {fname} → ~/mark24_saved/ (reason: {reason})")
    except Exception as e:
        print(f"  [!] Gagal pindah ke saved: {e}")

# ─────────────────────────────────────────
# ANC
# ─────────────────────────────────────────
def _hp_filter_scipy(chunk_p, chunk_n, sos, zi_p, zi_n):
    out_p, zi_p = scipy_signal.sosfilt(sos, chunk_p, zi=zi_p)
    out_n, zi_n = scipy_signal.sosfilt(sos, chunk_n, zi=zi_n)
    return out_p - out_n, zi_p, zi_n

def _hp_filter_numpy(chunk_p, chunk_n, alpha, yp_prev, yn_prev, xp_prev, xn_prev):
    n      = len(chunk_p)
    out_p  = np.empty(n, dtype=np.float32)
    out_n  = np.empty(n, dtype=np.float32)
    xp_full = np.concatenate(([xp_prev], chunk_p))
    xn_full = np.concatenate(([xn_prev], chunk_n))
    for i in range(n):
        yp_prev  = xp_full[i+1] - xp_full[i] + alpha * yp_prev
        yn_prev  = xn_full[i+1] - xn_full[i] + alpha * yn_prev
        out_p[i] = yp_prev
        out_n[i] = yn_prev
    return out_p - out_n, yp_prev, yn_prev, chunk_p[-1], chunk_n[-1]

def process_anc_chunked(wav_path):
    """
    Return clean_path (string) kalau sukses, None kalau gagal.
    Kalau gagal, file audio asli TIDAK dihapus — biarkan caller yang handle.
    """
    if ANC_MODE == "off":
        print("  [~] ANC off, skip")
        return None
    if os.path.getsize(wav_path) < 10_000:
        print("  [!] File terlalu kecil, skip ANC")
        return None

    clean_path = wav_path.replace(".wav", "_clean.wav")

    try:
        with sf.SoundFile(wav_path, 'r') as src:
            fs = src.samplerate
            channels = src.channels
            total_frames = len(src)

            if channels < 2:
                print("  [!] Audio bukan stereo, skip ANC")
                return None

            chunk_frames = int(fs * ANC_CHUNK_SEC)
            total_chunks = (total_frames + chunk_frames - 1) // chunk_frames
            print(f"  [*] ANC [{ANC_MODE}] chunked: {total_chunks} chunk x {ANC_CHUNK_SEC}s")

            if ANC_MODE == "scipy":
                sos  = scipy_signal.butter(2, 20, 'hp', fs=fs, output='sos')
                zi_p = scipy_signal.sosfilt_zi(sos) * 0.0
                zi_n = scipy_signal.sosfilt_zi(sos) * 0.0
            else:
                alpha   = 1.0 - (2.0 * np.pi * 20.0 / fs)
                yp_prev = yn_prev = xp_prev = xn_prev = 0.0

            with sf.SoundFile(clean_path, 'w', samplerate=fs, channels=1,
                              subtype='PCM_16') as dst:
                chunk_idx   = 0
                frames_done = 0
                while True:
                    if _shutdown:
                        print("\n  [!] Shutdown saat ANC")
                        dst.flush()
                        try: os.remove(clean_path)
                        except Exception: pass
                        return None

                    data = src.read(chunk_frames, dtype='float32', always_2d=True)
                    if len(data) == 0:
                        break

                    ch_p = data[:, 0]
                    ch_n = data[:, 1]

                    if ANC_MODE == "scipy":
                        clean_chunk, zi_p, zi_n = _hp_filter_scipy(
                            ch_p, ch_n, sos, zi_p, zi_n)
                    else:
                        clean_chunk, yp_prev, yn_prev, xp_prev, xn_prev = _hp_filter_numpy(
                            ch_p, ch_n, alpha, yp_prev, yn_prev, xp_prev, xn_prev)

                    dst.write(clean_chunk)
                    frames_done += len(data)
                    chunk_idx   += 1
                    progress_bar(f"ANC {chunk_idx}/{total_chunks}",
                                 frames_done, total_frames)
                    if ANC_CHUNK_SLEEP > 0:
                        time.sleep(ANC_CHUNK_SLEEP)

        print("  [*] Normalisasi peak...")
        _normalize_wav_inplace(clean_path)

        size_mb = os.path.getsize(clean_path) / (1024 * 1024)
        print(f"  [V] ANC selesai -> {os.path.basename(clean_path)} ({size_mb:.1f} MB)")
        return clean_path

    except MemoryError:
        print("  [!] MemoryError — kurangi ANC_CHUNK_SEC")
        try: os.remove(clean_path)
        except Exception: pass
        return None
    except Exception as e:
        print(f"  [!] Error ANC: {e}")
        try: os.remove(clean_path)
        except Exception: pass
        return None

def _normalize_wav_inplace(wav_path):
    tmp_path = wav_path + ".tmp"
    try:
        peak = 0.0
        with sf.SoundFile(wav_path, 'r') as f:
            while True:
                chunk = f.read(65536, dtype='float32')
                if len(chunk) == 0:
                    break
                local_peak = float(np.max(np.abs(chunk)))
                if local_peak > peak:
                    peak = local_peak
        if peak <= 0:
            return
        gain = 1.0 / peak
        with sf.SoundFile(wav_path, 'r') as src:
            with sf.SoundFile(tmp_path, 'w', samplerate=src.samplerate,
                              channels=src.channels, subtype='PCM_16') as dst:
                while True:
                    chunk = src.read(65536, dtype='float32')
                    if len(chunk) == 0:
                        break
                    dst.write(chunk * gain)
        os.replace(tmp_path, wav_path)
    except Exception as e:
        print(f"  [!] Normalisasi gagal: {e}")
        try: os.remove(tmp_path)
        except Exception: pass

# ─────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────
def load_service():
    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.pickle')
    if not os.path.exists(token_path):
        raise FileNotFoundError("token.pickle tidak ditemukan")
    with open(token_path, 'rb') as t:
        creds = pickle.load(t)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)

def upload_file(service, file_path, mime, upload_name=None):
    """Upload ke Drive. Return True kalau sukses."""
    file_size = os.path.getsize(file_path)
    local_name = os.path.basename(file_path)
    drive_name = upload_name or local_name

    for attempt in range(MAX_RETRY):
        try:
            label = drive_name if drive_name == local_name else f"{local_name} → {drive_name}"
            print(f"  [*] Upload {label} ({file_size // 1024} KB) — coba {attempt+1}/{MAX_RETRY}")
            media = MediaFileUpload(file_path, mimetype=mime, resumable=True)
            request = service.files().create(
                body={'name': drive_name, 'parents': [GDRIVE_ID]},
                media_body=media
            )
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress_bar(f"Upload {drive_name}",
                                 int(status.resumable_progress), file_size)
            progress_bar(f"Upload {drive_name}", file_size, file_size)
            print(f"  [V] Upload sukses: {drive_name}")
            return True
        except Exception as e:
            print(f"\n  [!] Gagal upload (try {attempt+1}): {e}")
            if attempt < MAX_RETRY - 1:
                for i in range(3, 0, -1):
                    print(f"\r  [~] Retry dalam {i}s...", end="", flush=True)
                    time.sleep(1)
                print()
    print(f"  [X] Upload gagal: {drive_name}")
    return False

def upload_or_save(service, file_path, mime, upload_name=None, label="file"):
    """
    Coba upload. Kalau sukses, hapus file lokal.
    Kalau gagal SETELAH MAX_RETRY, pindahkan ke SAVED_DIR (tidak hapus, tidak loop).
    Return True kalau sukses upload.
    """
    if not os.path.exists(file_path):
        return False
    if service is None:
        # Tidak ada koneksi Drive — langsung save
        save_to_saved_dir(file_path, reason="no_drive_service")
        return False
    if upload_file(service, file_path, mime, upload_name=upload_name):
        try: os.remove(file_path)
        except Exception as e: print(f"  [!] Gagal hapus: {e}")
        return True
    else:
        # Upload gagal setelah retry → save, jangan hapus, jangan loop
        save_to_saved_dir(file_path, reason=f"upload_failed_{label}")
        return False

# ─────────────────────────────────────────
# FIND READY FILES
# ─────────────────────────────────────────
def find_ready_files():
    ready = []
    for af in sorted(glob.glob(os.path.join(QUEUE_DIR, "aud_*.wav"))):
        a_done = af + ".done"
        base   = os.path.basename(af).replace("aud_", "vid_", 1).replace(".wav", ".mp4")
        vf     = os.path.join(QUEUE_DIR, base)
        v_done = vf + ".done"
        meta   = af.replace(".wav", ".meta.json")

        if os.path.exists(a_done) and os.path.exists(vf) and os.path.exists(v_done):
            ready.append({
                "audio": af,
                "video": vf,
                "audio_done": a_done,
                "video_done": v_done,
                "meta": meta if os.path.exists(meta) else None,
            })
    return ready

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    print("=" * 55)
    print("  MARK24 WORKER — End-of-Day ANC + Upload")
    print(f"  ANC Mode  : {ANC_MODE}")
    print(f"  Chunk     : {ANC_CHUNK_SEC}s")
    print(f"  Queue Dir : {QUEUE_DIR}")
    print(f"  Saved Dir : {SAVED_DIR}")
    print("=" * 55)

    try: os.nice(5)
    except Exception: pass

    ready = find_ready_files()
    if not ready:
        print("[*] Tidak ada file siap proses. Selesai.")
        return

    print(f"[*] {len(ready)} sesi rekaman siap diproses.\n")

    # Coba load service Drive
    service = None
    try:
        service = load_service()
    except Exception as e:
        print(f"[!] Gagal load Google Drive service: {e}")
        print("[!] Semua file akan disimpan ke ~/mark24_saved/ untuk upload manual nanti.")

    for idx, item in enumerate(ready, 1):
        if _shutdown:
            break

        af   = item["audio"]
        vf   = item["video"]
        meta = item["meta"]
        fname = os.path.basename(af)

        final_audio = final_video = final_clean = final_meta = None
        j = {}
        if meta:
            try:
                with open(meta) as f:
                    m = json.load(f)
                j = m.get("jadwal", {})
                final_audio = m.get("final_basename_audio")
                final_video = m.get("final_basename_video")
                final_clean = m.get("final_basename_clean")
                final_meta  = m.get("final_basename_meta")
            except Exception:
                pass

        print(f"\n{'═'*55}")
        print(f"  Sesi {idx}/{len(ready)}: {fname}")
        if j:
            print(f"  Kuliah    : {j.get('mata_kuliah')} ({j.get('kode_mata_kuliah')})")
            print(f"  Kelas     : {j.get('kelas')} | Ruangan: {j.get('ruangan')}")
            print(f"  Dosen     : {j.get('dosen_utama')}")
            print(f"  Jadwal    : {j.get('jam_mulai')}–{j.get('jam_selesai')}")
        if final_audio:
            print(f"  Upload as : {final_audio}")
        sz_mb = os.path.getsize(af) / (1024 * 1024)
        print(f"  Ukuran WAV: {sz_mb:.1f} MB")
        print(f"{'═'*55}")

        check_temp()

        # ANC (kalau gagal, audio asli tetap ada — di-handle di step upload)
        clean_wav = process_anc_chunked(af)

        check_temp()

        # Upload: audio asli
        print("\n  [STEP 1/4] Audio asli")
        success_audio = upload_or_save(service, af, 'audio/wav',
                                       upload_name=final_audio, label="audio")
        if success_audio:
            try: os.remove(item["audio_done"])
            except Exception: pass

        # Upload: audio bersih (kalau ANC sukses)
        if clean_wav and os.path.exists(clean_wav):
            print("\n  [STEP 2/4] Audio bersih (ANC)")
            upload_or_save(service, clean_wav, 'audio/wav',
                           upload_name=final_clean, label="clean")
        else:
            print("\n  [STEP 2/4] Tidak ada audio bersih (ANC gagal/skip)")

        # Upload: video
        print("\n  [STEP 3/4] Video")
        success_video = upload_or_save(service, vf, 'video/mp4',
                                       upload_name=final_video, label="video")
        if success_video:
            try:
                if os.path.exists(item["video_done"]):
                    os.remove(item["video_done"])
            except Exception:
                pass

        # Upload: metadata
        if meta and os.path.exists(meta):
            print("\n  [STEP 4/4] Metadata jadwal")
            upload_or_save(service, meta, 'application/json',
                           upload_name=final_meta, label="meta")
        else:
            print("\n  [STEP 4/4] Tidak ada metadata")

        print(f"\n  [V] Selesai sesi: {fname}\n")

    # Cek dan laporkan file yang masih di saved
    if os.path.isdir(SAVED_DIR):
        saved_files = os.listdir(SAVED_DIR)
        if saved_files:
            print(f"\n[!] {len(saved_files)} file disimpan di {SAVED_DIR}")
            print("[!] Untuk upload manual nanti, jalankan: python3 mark24_work.py (setelah memindah file ke queue)")

    print("\n[*] Worker selesai.")


if __name__ == "__main__":
    main()
