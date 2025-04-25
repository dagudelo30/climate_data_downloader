import gzip
import shutil
import rasterio
from rasterio.mask import mask
import geopandas as gpd
from shapely.geometry import box
import zipfile
import tarfile
from pathlib import Path


def extract_zip_flat(zip_path, output_dir, max_name_length=150):
    """
    Extrae todos los archivos de un archivo .zip directamente en `output_dir`,
    ignorando cualquier estructura de carpetas interna y truncando nombres
    si exceden el límite de caracteres para rutas largas (especialmente en Windows).

    Parámetros:
    - zip_path: Ruta al archivo .zip (Path o str)
    - output_dir: Carpeta destino donde se guardarán los archivos extraídos
    - max_name_length: Máximo número de caracteres permitido en el nombre del archivo
    """

    zip_path = Path(zip_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                if member.is_dir():
                    continue  # Ignora carpetas internas

                nombre_original = Path(member.filename).name
                nombre_truncado = nombre_original[:max_name_length]
                destino = output_dir / nombre_truncado

                with zip_ref.open(member) as fuente, open(destino, 'wb') as salida:
                    shutil.copyfileobj(fuente, salida)

                print(f"✅ Extraído: {nombre_truncado}")

    except Exception as e:
        print(f"❌ Error al extraer ZIP: {e}")


def descomprimir_archivo_gz(input_path, output_path):
    """
    Descomprime un archivo .gz a .tif.
    """
    with gzip.open(input_path, 'rb') as f_in:
        with open(output_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    print(f"Descomprimido: {output_path}")

def recortar_raster(ruta_raster, ruta_salida, ruta_shapefile=None, bbox=None):
    """
    Recorta un raster usando un shapefile o un bounding box.
    """
    if not ruta_shapefile and not bbox:
        raise ValueError("Debes proporcionar un shapefile o un bounding box para recortar.")

    with rasterio.open(ruta_raster) as src:
        if ruta_shapefile:
            shapefile = gpd.read_file(ruta_shapefile)
            geometries = shapefile.geometry.values
        elif bbox:
            xmin, ymin, xmax, ymax = bbox
            geom = box(xmin, ymin, xmax, ymax)
            geometries = [geom]

        out_image, out_transform = mask(src, geometries, crop=True)
        out_meta = src.meta.copy()

        out_meta.update({
            "driver": "GTiff",
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform
        })

        with rasterio.open(ruta_salida, "w", **out_meta) as dest:
            dest.write(out_image)

    print(f"Raster recortado guardado en: {ruta_salida}")