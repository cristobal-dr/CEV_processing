# Guia de descarga de informacion CEV

Esta guia describe como se descargan las fichas PDF desde el buscador publico de Calificacion Energetica de Viviendas (CEV) del MINVU y donde queda registrada la trazabilidad de la descarga.

## Configuracion de rutas

Las rutas se centralizan en `config/paths.yaml`. Ese archivo queda fuera de Git porque puede contener rutas locales o externas pesadas. Para crear una configuracion nueva, copia `config/paths.example.yaml` a `config/paths.yaml` y ajusta estos campos principales:

- `cev_pdf_dir`: carpeta donde se guardan las fichas PDF descargadas.
- `download_log_csv_path`: CSV con el historial de descargas.
- `logs_dir`: carpeta general de logs.
- `project_root`: raiz del repositorio; si usas rutas relativas, se resuelven desde esta carpeta.

Ejemplo minimo:

```yaml
project_root: .
cev_pdf_dir: data/raw/cev_fichas
download_log_csv_path: logs/descargas_cev_log.csv
logs_dir: logs
```

## Script principal

El script de descarga es `scripts/download_cev_fichas.py`. Por defecto lee `config/paths.yaml`:

```bash
python scripts/download_cev_fichas.py
```

Tambien puedes pasar rutas manualmente para una corrida puntual:

```bash
python scripts/download_cev_fichas.py \
  --output-dir data/raw/cev_fichas \
  --log-csv logs/descargas_cev_log.csv
```

Para probar una sola comuna antes de lanzar una descarga grande:

```bash
python scripts/download_cev_fichas.py \
  --only-one-comuna \
  --target-region-contains Metropolitana \
  --target-comuna "La Cisterna"
```

## Que hace el script

1. Abre el buscador publico CEV con Selenium para cargar regiones y comunas disponibles.
2. Consulta cada comuna seleccionada.
3. Recorre las secciones `precalificadas` y `calificadas`.
4. Descarga los documentos disponibles como PDF, principalmente la accion `Ver Informe`.
5. Guarda cada PDF dentro de una subcarpeta por comuna.
6. Registra cada intento en `download_log_csv_path`.

## Estructura esperada de salida

```text
cev_pdf_dir/
  la_cisterna/
    ficha_..._precalificadas_ver_informe.pdf
    ficha_..._calificadas_ver_informe.pdf
  providencia/
    ficha_..._precalificadas_ver_informe.pdf
logs/
  descargas_cev_log.csv
```

El log de descarga permite retomar ejecuciones. El script identifica registros exitosos (`estado == "ok"`) y evita repetir descargas ya registradas con la misma combinacion de region, comuna, seccion, identificacion y accion.

## Recomendaciones operativas

- Ejecuta primero una comuna con `--only-one-comuna` para validar navegador, permisos y estructura de carpetas.
- Mantén los PDFs pesados fuera de Git; el repositorio ya ignora `data/raw/*` y archivos `.pdf`.
- Si una corrida se interrumpe, vuelve a ejecutar el script usando el mismo `download_log_csv_path`.
- Si cambias la ubicacion de los PDFs, actualiza solo `config/paths.yaml`.
