# 🌎 Climate Data Downloader

Este repositorio contiene un conjunto de scripts y notebooks para descargar, procesar y organizar variables climáticas provenientes de diferentes fuentes: **CHIRPS**, **Copernicus CDS**, y **Google Earth Engine (GEE)**.

---

## 📦 Estructura del proyecto

```plaintext
climate_data_downloader/
├── data/                      # Carpeta raíz de todos los datos descargados y procesados
│   ├── auxiliary/             # Información de apoyo, como shapefiles o archivos de máscara
│   ├── raw/                   # Datos originales sin procesar desde la fuente (e.g. .gz, .nc, .tif)
│   ├── interim/               # Datos descomprimidos y/o transformados, listos para análisis posterior
│   └── processed/             # Datos finales ya recortados o formateados para modelado o análisis
│
├── download/                  # Scripts principales para descarga de datos por fuente
│   ├── chirps.py              # Descarga y validación de precipitación diaria CHIRPS
│   ├── copernicus.py          # Descarga de variables climáticas vía Copernicus CDS API
│   ├── gee.py                 # Exportación de NDVI/NDWI desde Google Earth Engine (GEE)
│   └── utils.py               # Utilidades generales: logs, validaciones, conexión
│
├── notebooks/                 # Notebooks de demostración y ejecución del pipeline
│   └── pipeline_demo.ipynb    # Notebook principal con ejemplos de descarga y procesamiento
│
├── requirements.txt           # Lista de paquetes necesarios para reproducir el entorno
├── .env.example               # Ejemplo de archivo de variables de entorno (CDSAPI_KEY)
├── .gitignore                 # Ignora carpetas como logs, datos y archivos sensibles como .env
└── README.md                  # Documentación general del proyecto

## ⚙️ Requisitos

Instala los paquetes necesarios:

```bash
pip install -r requirements.txt
🔐 Configuración del archivo .env
Para descargar datos desde la API de Copernicus CDS es necesario autenticarte:

Crea una cuenta en: https://cds.climate.copernicus.eu

Copia tu clave de API (uid:api-key)

Crea un archivo .env (puedes partir de .env.example):

bash
Always show details

Copy
cp .env.example .env
Modifica el valor de CDSAPI_KEY:

env
Always show details

Copy
CDSAPI_KEY=123456:abcdefg-your-api-key
El archivo .cdsapirc se generará automáticamente cuando ejecutes los scripts.

🚀 Fuentes de datos y flujos incluidos
🌧️ CHIRPS
Datos diarios de precipitación

Se descargan como .tif.gz, se descomprimen y recortan al área de interés

Usa: download/chirps.py

☀️ Copernicus (CDS)
Variables como radiación solar, temperatura, humedad, viento, etc.

Se descargan como .nc.gz y se descomprimen

Usa: download/copernicus.py

🌱 Google Earth Engine (GEE)
Variables derivadas de MODIS como NDVI y NDWI

Se exportan como .tif ya recortadas

Usa: download/gee.py

📓 Ejecución
Puedes usar directamente el notebook:

bash
Always show details

Copy
notebooks/pipeline_demo.ipynb
O correr los scripts modularmente con tus propias fechas y shapefiles.

🧪 Reproducibilidad
Todos los flujos están diseñados para ser:

Automatizados

Controlados por fecha y área

Basados en estructuras de carpetas replicables

Sincronizados con .env y logs (logs/download_log.txt)

📬 Contacto
Este repositorio ha sido desarrollado por Diego Agudelo como parte de un flujo de trabajo climático reproducible para la zona de Cali, Colombia.

📝 Licencia
Puedes usar este código con fines educativos o de investigación. Si lo reutilizas, por favor cita el repositorio o a su autor. 