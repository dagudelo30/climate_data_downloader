import os
import io
import requests
import socket
from datetime import datetime
from pathlib import Path



# Ruta base del proyecto (2 niveles arriba del notebook)
ROOT_DIR = Path(__file__).resolve().parent.parent if "__file__" in globals() else Path.cwd().parent

def check_internet_connection(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def is_valid_file(filepath, min_size_kb=50):
    return os.path.exists(filepath) and os.path.getsize(filepath) > min_size_kb * 1024

def log_event(msg, log_file=ROOT_DIR / "logs" / "download_log.txt", level="INFO"):
    """
    Registra un evento en un archivo de log con timestamp y nivel.
    """
    try:
        os.makedirs(log_file.parent, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        linea = f"[{timestamp}] [{level.upper()}] {msg}\n"
        with io.open(log_file, mode="a", encoding="utf-8") as f:
            f.write(linea)
    except Exception as e:
        print(f"⚠️ Error al escribir en log (utf-8): {e}")