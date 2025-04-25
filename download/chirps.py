import os
import requests
from tqdm import tqdm
from download.utils import check_internet_connection, ensure_dir, is_valid_file, log_event
from retrying import retry

@retry(stop_max_attempt_number=3, wait_exponential_multiplier=1000)
def descargar_chirps(url, output_path, min_size_kb=50):
    """
    Descarga un archivo CHIRPS desde una URL y lo guarda en la ruta indicada.
    Muestra una barra de progreso con tamaño y velocidad de descarga.
    """
    if not check_internet_connection():
        raise Exception("Sin conexión a Internet.")

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