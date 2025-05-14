import os, requests, gzip
from pathlib import Path
from tqdm import tqdm
from retrying import retry
from download.utils import check_internet_connection, ensure_dir, is_valid_file, log_event


def ensure_dir(p): Path(p).mkdir(parents=True, exist_ok=True)

def is_valid_file(path, min_kb):
    return os.path.isfile(path) and os.path.getsize(path) >= min_kb * 1024

@retry(stop_max_attempt_number=3, wait_exponential_multiplier=1000)
def descargar_chirps1(url, output_path, min_size_kb=50):
    output_path = Path(output_path)
    ensure_dir(output_path.parent)

    # --- HEAD: tamaño esperado ---
    head = requests.head(url, allow_redirects=True, timeout=10)
    head.raise_for_status()
    expected = int(head.headers.get("content-length", 0))          # 0 = servidor no lo declara

    # --- ¿ya existe y coincide byte por byte? ---
    if expected and output_path.exists() and output_path.stat().st_size == expected:
        print(f"✓ Archivo ya existe y tiene tamaño correcto: {output_path.name}")
        return

    # --- GET con stream ---
    with requests.get(url, stream=True, timeout=(10, 300)) as r:
        r.raise_for_status()
        total_size = int(r.headers.get("content-length", 0))
        with open(output_path, "wb") as f, tqdm(
            desc=f"Descargando {output_path.name}",
            total=total_size or None,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as barra:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    barra.update(len(chunk))
            f.flush(); os.fsync(f.fileno())         # fuerza escritura en disco

    # --- Validaciones post‑descarga ---
    actual = output_path.stat().st_size
    if expected and actual != expected:
        output_path.unlink(missing_ok=True)
        raise ValueError(f"Tamaño incorrecto ({actual} vs {expected})")

    if actual < min_size_kb * 1024:
        output_path.unlink(missing_ok=True)
        raise ValueError("Archivo demasiado pequeño, probablemente vacío")

    try:                                           # test rápido de integridad gzip
        with gzip.open(output_path, "rb") as gz:
            gz.read(1)
    except OSError:
        output_path.unlink(missing_ok=True)
        raise ValueError("Archivo gzip corrupto")

    print(f"✓ Descarga correcta: {output_path.name}")


@retry(stop_max_attempt_number=3, wait_exponential_multiplier=1000)
def descargar_chirps(url, output_path, min_size_kb=50):
    """
    Descarga un archivo CHIRPS desde una URL y lo guarda en la ruta indicada.
    Muestra una barra de progreso con tamaño y velocidad de descarga.
    """
    ensure_dir(os.path.dirname(output_path))

    if is_valid_file(output_path, min_size_kb):
        log_event(f"Archivo ya existe y es válido: {output_path}")
        return

    log_event(f"Iniciando descarga desde: {url}")

    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))

            with open(output_path, 'wb') as f, tqdm(
                desc=f"Descargando {os.path.basename(output_path)}",
                total=total_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
            ) as barra:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        barra.update(len(chunk))

    except Exception as e:
        log_event(f"Error al descargar {url}: {e}", level="ERROR")
        raise

    if not is_valid_file(output_path, min_size_kb):
        raise Exception(f"Archivo descargado es inválido o está corrupto: {output_path}")

    log_event(f"Descarga exitosa: {output_path}")