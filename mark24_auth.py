#!/usr/bin/env python3
# ============================================
# MARK21 AUTHENTICATION SETUP
# ============================================
# Script untuk setup OAuth token untuk Google Drive
# Jalankan SEKALI saja untuk mendapatkan token.pickle
#
# Cara pakai:
#   python3 mark21_auth.py
#
# Script ini akan:
# 1. Buka browser untuk login Google
# 2. Approve akses untuk MARK21
# 3. Simpan token ke ~/token.pickle
# ============================================

import os.path
import pickle
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Scopes yang dibutuhkan untuk Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive']

def authenticate_google_drive():
    """
    Authenticate dengan Google Drive API
    
    Proses:
    1. Cek jika token.pickle sudah ada (dari login sebelumnya)
    2. Jika belum ada, buka browser untuk login
    3. Simpan token ke ~/token.pickle
    """
    
    creds = None
    token_path = os.path.expanduser("~/token.pickle")
    
    print("[*] MARK21 Google Drive Authentication")
    print("=" * 60)
    
    # 1. Cek apakah sudah ada token dari login sebelumnya
    if os.path.exists(token_path):
        print(f"[*] Found existing token: {token_path}")
        try:
            with open(token_path, 'rb') as token_file:
                creds = pickle.load(token_file)
                print("[✓] Token loaded successfully")
                
                # Cek apakah token masih valid
                if creds and creds.expired and creds.refresh_token:
                    print("[*] Token expired, refreshing...")
                    creds.refresh(Request())
                    print("[✓] Token refreshed")
                    
                    # Simpan token yang sudah di-refresh
                    with open(token_path, 'wb') as token_file:
                        pickle.dump(creds, token_file)
                        print(f"[✓] Refreshed token saved to {token_path}")
                
                return creds
        except Exception as e:
            print(f"[!] Error loading token: {e}")
            print("[*] Will create new token...")
    
    # 2. Jika belum ada token, minta login
    if not creds or not creds.valid:
        print("[*] Starting authentication flow...")
        print("[*] Browser akan terbuka untuk login Google")
        print("")
        
        # File credentials JSON dengan nama: client_scrt.json
        credentials_file = os.path.expanduser("~/client_scrt.json")
        
        if not os.path.exists(credentials_file):
            print("[!] ========== ERROR ==========")
            print(f"[!] Credentials file not found: {credentials_file}")
            print("[!] ")
            print("[!] File harus bernama: client_scrt.json")
            print("[!] ")
            print("[!] Langkah:")
            print("[!] 1. Download client_secret.json dari Google Cloud Console")
            print("[!] 2. Rename menjadi: client_scrt.json")
            print("[!] 3. Copy ke home directory: ~/client_scrt.json")
            print("[!] 4. Jalankan script ini lagi")
            print("[!] =============================")
            return None
        
        # Start OAuth flow
        try:
            print("[*] Opening browser for Google login...")
            print("[*] Jika browser tidak membuka otomatis:")
            print("[*] Copy-paste URL dari terminal ke browser")
            print("")
            
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file,
                SCOPES
            )
            
            creds = flow.run_local_server(port=0)
            
            print("")
            print("[✓] Authentication successful!")
            
            # Simpan token untuk penggunaan berikutnya
            with open(token_path, 'wb') as token_file:
                pickle.dump(creds, token_file)
                print(f"[✓] Token saved to: {token_path}")
            
            return creds
            
        except Exception as e:
            print(f"[!] Authentication failed: {e}")
            return None
    
    return creds


def verify_access(creds):
    """
    Verify bahwa kita punya akses ke Google Drive
    """
    try:
        service = build('drive', 'v3', credentials=creds)
        results = service.files().list(pageSize=1).execute()
        
        print("[*] Testing access...")
        print("[✓] Successfully connected to Google Drive!")
        
        # Coba list a few files
        files = results.get('files', [])
        if files:
            print(f"[✓] Found {len(files)} file(s) in your Drive")
        else:
            print("[*] No files found in Drive (empty)")
        
        return True
        
    except Exception as e:
        print(f"[!] Error testing access: {e}")
        return False


def main():
    """
    Main authentication flow
    """
    print()
    
    # 1. Authenticate
    creds = authenticate_google_drive()
    
    if not creds:
        print("[!] Authentication failed")
        return False
    
    # 2. Verify
    if not verify_access(creds):
        print("[!] Access verification failed")
        return False
    
    print()
    print("=" * 60)
    print("[✓] MARK21 Authentication Setup Complete!")
    print("=" * 60)
    print()
    print("[*] You can now run MARK21:")
    print("    python3 ~/mark21_rec.py   # Recorder")
    print("    python3 ~/mark21_work.py  # Worker")
    print()
    
    return True


if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
