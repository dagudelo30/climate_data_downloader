import os
import ee
import geemap
from download.utils import ensure_dir, log_event


def inicializar_gee(project_id=None):
    """
    Inicializa Google Earth Engine con un proyecto registrado.
    """
    from pathlib import Path
    cred_path = Path.home() / ".config" / "earthengine" / "credentials"

    try:
        if not cred_path.exists():
            print(f"🔐 Autenticando con el proyecto: {project_id}")
            ee.Authenticate(cloud_project=project_id)
        ee.Initialize(project=project_id)
        print("✅ Google Earth Engine inicializado correctamente.")
    except Exception as e:
        print(f"❌ Error al inicializar Earth Engine: {e}")


def calcular_ndvi(image):
    """Calcula NDVI a partir de una imagen MODIS."""
    nir = image.select("sur_refl_b02")
    red = image.select("sur_refl_b01")
    ndvi = nir.subtract(red).divide(nir.add(red)).rename("NDVI")
    return ndvi.copyProperties(image, ["system:time_start"])

def calcular_ndwi(image):
    """
    Calcula NDWI a partir de una imagen MODIS.
    NDWI = (NIR - SWIR) / (NIR + SWIR)
    """
    nir = image.select("sur_refl_b02")   # Banda 2 (NIR)
    swir = image.select("sur_refl_b06")  # Banda 6 (SWIR)
    ndwi = nir.subtract(swir).divide(nir.add(swir)).rename("NDWI")
    return ndwi.copyProperties(image, ["system:time_start"])


def exportar_imagen_local(image, region, output_folder, nombre_base, scale=500):
    """
    Exporta una imagen recortada al área de interés como TIFF.

    Args:
        image (ee.Image): Imagen a exportar
        region (ee.FeatureCollection): Región de interés
        output_folder (str): Carpeta de salida
        nombre_base (str): Prefijo del archivo de salida
        scale (int): Resolución en metros (default 500)
    """
    date = ee.Date(image.get("system:time_start")).format("YYYY-MM-dd").getInfo()
    filename = os.path.join(output_folder, f"{nombre_base}_{date}.tif")

    geemap.ee_export_image(
        image.clip(region),
        filename=filename,
        scale=scale,
        region=region.geometry(),
        file_per_band=False
    )
    print(f"✅ Descargado: {filename}")
    log_event(f"Imagen exportada: {filename}")


def descargar_ndvi_modis(fecha_inicio, fecha_fin, shapefile_path, output_folder):
    """
    Descarga imágenes NDVI diarias desde MODIS y las guarda como TIFF.

    Args:
        fecha_inicio (str): Fecha de inicio "YYYY-MM-DD"
        fecha_fin (str): Fecha final "YYYY-MM-DD"
        shapefile_path (str): Ruta al shapefile del área de interés
        output_folder (str): Carpeta de salida
    """
    ensure_dir(output_folder)
    region = geemap.shp_to_ee(shapefile_path)
    fecha_fin_ajustada = ee.Date(fecha_fin).advance(1, "day")

    modis = (ee.ImageCollection("MODIS/061/MOD09GA")
             .filterBounds(region)
             .filterDate(fecha_inicio, fecha_fin_ajustada))

    image_count = modis.size().getInfo()
    print(f"📦 Imágenes encontradas: {image_count}")

    if image_count == 0:
        raise ValueError("⚠ No se encontraron imágenes para el rango de fechas y la región seleccionada.")

    ndvi_collection = modis.map(calcular_ndvi)
    image_list = ndvi_collection.toList(ndvi_collection.size())

    for i in range(image_count):
        img = ee.Image(image_list.get(i))
        exportar_imagen_local(img, region, output_folder, nombre_base="NDVI_MODIS")

    print("✅ Descarga de NDVI diario completada.")


def descargar_ndwi_modis(fecha_inicio, fecha_fin, shapefile_path, output_folder):
    """
    Descarga imágenes NDWI diarias desde MODIS y las guarda como TIFF.
    """
    ensure_dir(output_folder)
    region = geemap.shp_to_ee(shapefile_path)
    fecha_fin_ajustada = ee.Date(fecha_fin).advance(1, "day")

    modis = (ee.ImageCollection("MODIS/061/MOD09GA")
             .filterBounds(region)
             .filterDate(fecha_inicio, fecha_fin_ajustada))

    image_count = modis.size().getInfo()
    print(f"📦 Imágenes encontradas: {image_count}")

    if image_count == 0:
        raise ValueError("⚠ No se encontraron imágenes para el rango de fechas y la región seleccionada.")

    ndwi_collection = modis.map(calcular_ndwi)
    image_list = ndwi_collection.toList(ndwi_collection.size())

    for i in range(image_count):
        img = ee.Image(image_list.get(i))
        exportar_imagen_local(img, region, output_folder, nombre_base="NDWI_MODIS")

    print("✅ Descarga de NDWI diario completada.")
