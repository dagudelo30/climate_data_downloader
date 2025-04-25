# 🌎 Climate Data Downloader

Este repositorio contiene un conjunto de scripts y notebooks para descargar, procesar y organizar variables climáticas provenientes de diferentes fuentes: **CHIRPS**, **Copernicus CDS**, y **Google Earth Engine (GEE)**.

---

## 📦 Estructura del proyecto

```
climate_data_downloader/
├── data/                      # Carpeta que contiene todos los datos descargados y procesados.
│   ├── auxiliary/             # Información de apoyo, como shapefiles o archivos de máscara.
│   ├── raw/                   # Datos originales sin procesar desde la fuente (e.g. .gz, .nc, .tif).
│   ├── interim/               # Datos descomprimidos y/o transformados, listos para análisis posterior.
│   └── processed/             # Datos finales ya recortados al área de interés o formateados para modelado o análisis.
│
├── download/                  # Scripts principales para descarga de datos por fuente.
│   ├── chirps.py              # Contiene las funciones para realizar la Descarga de precipitación diaria CHIRPS.
│   ├── copernicus.py          # Contiene las funciones para realizar la Descarga de variables climáticas vía Copernicus CDS API.
│   ├── gee.py                 # Contiene las funciones para descargar y calcular el NDVI/NDWI desde Google Earth Engine (GEE).
│   └── utils.py               # Utilidades generales: logs, validaciones, conexión, etc.
│
├── notebooks/                 # Notebooks de demostración y ejecución del pipeline
│   ├── 00-setup.ipynb         # Notebook inicial que configura la estructura de carpetas e instala la dependencias requeridas.
│   ├── 01_download_chirps     # Notebook que realiza la descarga de datos desde CHIRPS 
│   ├── 02_download_cds        # Notebook que realiza la descarga de datos desde Copernicus CDS API. 
│   ├── 03_download_gee        # Notebook que realiza la descarga de datos desde Google Earth Engine. 
│  
├── requirements.txt           # Lista de paquetes necesarios para reproducir el entorno.
├── .env.example               # Ejemplo de archivo de variables de entorno (CDSAPI_KEY) para realizar la descarga desde Copernicus CDS API.
├── .gitignore                 # Ignora carpetas como logs, datos y archivos sensibles como .env.
└── README.md                  # Documentación general del proyecto.
```
---
## 🚀 ¿Qué hace este proyecto?

### ✅ Automatiza la descarga de:

- **CHIRPS**: precipitación diaria como `.tif.gz`, descomprimida y recortada en un área de interés.
- **Copernicus CDS**: variables climáticas en `.zip`/`.tgz` con extracción automática y recortada en un área de interés.
- **Google Earth Engine**: Indices a nivel diario obtenidos, calculados y recortados en un área de interés. 
  - **NDVI** (Índice de Vegetación)
  - **NDWI** (Índice de Agua Vegetal)

### ✅ Exporta todo a:

- Formatos `.nc` o `.tif` según la fuente
- Resoluciones compatibles con análisis de cambio climático o modelado agroclimático

---
## ⚙️ Requisitos

### 1. Configurar el archivo `.env` para poder descargar datos desde Copernicus CDS API.

1. Copia el archivo `.env.example` y crea uno llamado `.env.`

```
.env.example → .env
```

2. Abre `.env` y coloca tu clave de la API de Copernicus:

```
CDSAPI_KEY=123456:abcde-tu-clave-personal
```

> Puedes obtener más información: [https://cds.climate.copernicus.eu/how-to-api](https://cds.climate.copernicus.eu/how-to-api). No es necesario crear ningún archivo adicional o instalar el paquete cdsapi, ya que automaticamente se crean los archivos necesarios en la ruta del usuario y se instalan las dependencias necesarias.

---
## 🛰️ Flujos de descarga disponibles

### ☁️ CHIRPS (precipitación diaria)

- Fuente: Climate Hazards Group
- Archivo: `download/chirps.py`
- Proceso: descarga → descomprime → recorta al shapefile de interés

### ☀️ Copernicus CDS (temperatura, radiación, humedad, etc.)

- Fuente: ERA5 AgERA5
- Archivo: `download/copernicus.py`
- Proceso: descarga `.zip` o `.tgz` → extracción → organización

### 🌿 Google Earth Engine (MODIS)

- Fuente: MOD09GA
- Archivo: `download/gee.py`
- Exporta:
  - `NDVI`: usando bandas RED y NIR
  - `NDWI`: usando bandas NIR y SWIR
- Salida: `.tif` por día

---

## 🧠 Recomendaciones

- Asegúrate de tener conexión a internet para conectarte a las APIs
- Puedes personalizar el área de interés para realizar la descarga.
- Si usas Earth Engine por primera vez, debes seguir las instrucciones del notebook `03_download_gee`.

---

