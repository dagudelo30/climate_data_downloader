import os
import io
import stat
import tempfile
import xarray as xr
import geopandas as gpd
import rioxarray as rxr
from datetime import datetime, date, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import time

# Permiso solo lectura en Drive
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Carpetas en Google Drive
FOLDER_ID_PAST = "1gWoZ2bK2u5osJ8Iw-dvguZ56Kmz2QWrL"      # hasta 2020
FOLDER_ID_CURRENT = "1aehP6YDNOO73ab3tvTZet2Sh5uPdG9I_"   # desde 2021


def get_drive_service(client_secret_path: str):
    token_path = "token.json"
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def force_writable(path):
    try:
        if os.path.exists(path):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def atomic_replace(src_tmp, dst_final):
    force_writable(dst_final)
    os.replace(src_tmp, dst_final)


def fecha_a_nombre(fecha: date) -> str:
    doy = fecha.timetuple().tm_yday
    return f"{fecha.year}{doy:03d}.nc"


def clip_nc_to_shape(in_nc_path, out_nc_path, shp_gdf, var_name_preferred="precipitation"):
    ds = xr.open_dataset(in_nc_path, engine="h5netcdf")
    var = var_name_preferred if var_name_preferred in ds.data_vars else list(ds.data_vars)[0]
    da = ds[var]

    # Detectar dims
    x_dim = y_dim = None
    for d in da.dims:
        dl = d.lower()
        if dl in ("lon", "longitude", "x"): x_dim = d
        if dl in ("lat", "latitude", "y"):  y_dim = d
    if x_dim is None or y_dim is None:
        ds.close()
        raise ValueError(f"No encuentro dims espaciales en {da.dims}.")

    if x_dim != "lon" or y_dim != "lat":
        da = da.rename({x_dim: "lon", y_dim: "lat"})

    if float(da.lon.max()) > 180:
        da = da.assign_coords(lon=((da.lon + 180) % 360) - 180).sortby("lon")

    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=False)
    if not da.rio.crs:
        da = da.rio.write_crs("EPSG:4326")

    da_clip = da.rio.clip(shp_gdf.geometry, shp_gdf.crs, all_touched=True)
    ds_clip = da_clip.to_dataset(name=var)

    tmp_out = out_nc_path + ".writing.tmp"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)
    ds_clip.to_netcdf(tmp_out, engine="h5netcdf", mode="w")
    ds.close()
    atomic_replace(tmp_out, out_nc_path)


def buscar_archivo(service, folder_id, filename):
    """
    Busca un archivo por nombre dentro de una carpeta en Drive.
    Devuelve el file_id si existe, sino None.
    """
    resp = service.files().list(
        q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
        spaces="drive",
        fields="files(id, name)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def download_mswep_range(service, shp_gdf, dest_dir,
                         fecha_inicio: date, fecha_fin: date,
                         overwrite=False):
    """
    Descarga y recorta MSWEP en un rango de fechas.
    Busca archivo por archivo directamente en Drive (más liviano).
    """
    os.makedirs(dest_dir, exist_ok=True)

    fecha = fecha_inicio
    delta = timedelta(days=1)

    print(f"📅 Descargando datos desde {fecha_inicio} hasta {fecha_fin}...")
    while fecha <= fecha_fin:
        anio = fecha.year
        fecha_str = fecha.strftime("%Y-%m-%d")
        expected_name = fecha_a_nombre(fecha)
        folder_id = FOLDER_ID_PAST if anio <= 2020 else FOLDER_ID_CURRENT

        print(f"\n🔎 Buscando archivo para {fecha_str} -> {expected_name} ...", end=" ")
        file_id = buscar_archivo(service, folder_id, expected_name)
        if not file_id:
            print("❌ No encontrado.")
            fecha += delta
            continue
        print("✅ Encontrado.")

        final_path = os.path.join(dest_dir, expected_name)
        if os.path.exists(final_path) and not overwrite:
            print(f"⏭️  Ya existe en disco, se omite: {final_path}")
            fecha += delta
            continue

        tmp_download = None
        try:
            # Crear archivo temporal para descarga
            with tempfile.NamedTemporaryFile(dir=dest_dir, suffix=".download.tmp", delete=False) as tmpf:
                tmp_download = tmpf.name
            request = service.files().get_media(fileId=file_id)

            print("⬇️  Iniciando descarga...")
            with io.FileIO(tmp_download, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        print(f"   Progreso: {int(status.progress() * 100)}%", end="\r")
            print("   ✅ Descarga completa.")

            # Recortar y guardar
            print("✂️  Recortando con shapefile...")
            clip_nc_to_shape(tmp_download, final_path, shp_gdf)
            print(f"   💾 Guardado en: {final_path}")

        except Exception as e:
            print(f"   ❌ Error procesando {expected_name}: {e}")
        finally:
            if tmp_download and os.path.exists(tmp_download):
                for intento in range(3):
                    try:
                        os.remove(tmp_download)
                        print(f"🗑️  Temporal eliminado: {tmp_download}")
                        break
                    except PermissionError:
                        print(f"⚠️  Archivo en uso, reintentando en 0.5s (intento {intento+1})...")
                        time.sleep(0.5)
                else:
                    try:
                        os.replace(tmp_download, tmp_download + ".to_delete")
                        print(f"⚠️  No se pudo borrar, renombrado como: {tmp_download}.to_delete")
                    except Exception as e2:
                        print(f"❌ No se pudo manejar temporal {tmp_download}: {e2}")

        fecha += delta
    print("\n🏁 Proceso terminado.")
