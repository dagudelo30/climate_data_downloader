import os
import zipfile
from tqdm import tqdm
from pathlib import Path
from dotenv import load_dotenv
import cdsapi
from download.utils import log_event, ensure_dir

# Cargar .env
load_dotenv()

def ensure_cdsapi_config():
    """
    Crea o reemplaza el archivo ~/.cdsapirc con la API Key obtenida desde .env
    """
    cds_key = os.getenv("CDSAPI_KEY")
    cds_file = Path.home() / ".cdsapirc"

    if not cds_key:
        raise ValueError("❌ No se encontró 'CDSAPI_KEY' en el archivo .env")

    cds_file.write_text(
        f"url: https://cds.climate.copernicus.eu/api\nkey: {cds_key}\n"
    )
    print(f"✅ Archivo .cdsapirc creado o reemplazado en: {cds_file}")

def download_as_gz(request_name, request_config, output_path):
    """
    Descarga un archivo desde CDS API en formato .gz (NetCDF comprimido)
    """
    client = cdsapi.Client()

    ensure_dir(Path(output_path).parent)

    try:
        variable = request_config.get("variable", "[variable no especificada]")
        log_event(f"Iniciando descarga: {variable} -> {output_path}", level="INFO")

        client.retrieve(request_name, request_config).download(str(output_path))

        log_event(f"Descarga exitosa: {output_path}")
    except Exception as e:
        log_event(f"Error durante la descarga: {e}", level="ERROR")
        raise

def download_variable(product, variable, statistic, year, month, area, output_path):
    """
    Descarga variables con estadística como .gz
    """
    days = [f"{day:02d}" for day in range(1, 32)]
    request = {
        "variable": variable,
        "statistic": statistic,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": days,
        "version": "1_1",
        "area": area,
        "format": "zip"
    }
    download_as_gz(product, request, output_path)

def download_simple_variable(product, variable, year, month, area, output_path):
    """
    Descarga variables simples como .gz
    """
    days = [f"{day:02d}" for day in range(1, 32)]
    request = {
        "variable": variable,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": days,
        "version": "1_1",
        "area": area,
        "format": "zip"
    }
    download_as_gz(product, request, output_path)

def download_humidity(product, year, month, area, output_path):
    """
    Descarga humedad relativa como archivo .gz
    """
    ensure_cdsapi_config()
    client = cdsapi.Client()
    ensure_dir(Path(output_path).parent)

    days = [f"{day:02d}" for day in range(1, 32)]
    request = {
        "variable": "2m_relative_humidity",
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": days,
        "version": "1_1",
        "area": area,
        "time": ["18_00"],
        "format": "zip"
    }

    try:
        log_event(f"Iniciando descarga de humedad relativa: {year}-{month:02d} -> {output_path}")
        client.retrieve(product, request).download(str(output_path))
        log_event(f"Humedad relativa guardada en: {output_path}")
    except Exception as e:
        log_event(f"Error al descargar humedad relativa: {e}", level="ERROR")
        raise
