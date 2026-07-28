# CEV Análisis

Repositorio para organizar la descarga, procesamiento y análisis de fichas CEV del MINVU.

## Objetivo

Construir una base ordenada de información proveniente de fichas CEV, incluyendo trazabilidad de descargas, extracción de datos desde PDFs, consolidación en base SQLite/PostgreSQL y análisis exploratorio.

## Estructura del repositorio

- `scripts/`: scripts ejecutables para descarga, extracción y procesamiento.
- `src/cev_analisis/`: funciones reutilizables.
- `sql/`: consultas SQL usadas para exploración y validación.
- `notebooks/`: análisis exploratorio.
- `data/`: datos crudos, intermedios y procesados. No se versionan archivos pesados.
- `logs/`: logs de descarga y procesamiento.
- `docs/`: documentación metodológica.
- `reports/`: tablas, gráficos y resultados.

## Datos principales

La base SQLite principal no se versiona en Git. Se recomienda mantenerla en una ruta externa, por ejemplo:

```text
/home/cdo/Downloads/CEV_db/cev_compiled.sqlite

## Guias

- [Guia de descarga de informacion CEV](docs/guia_descarga_informacion.md)
- [Guia de procesamiento de PDFs y generacion de SQLite](docs/guia_procesamiento_pdf_sqlite.md)
- [Guia de ubicaciones CEV y vista PostGIS](docs/guia_ubicaciones_postgis.md)

## Configuracion

Las rutas operativas se definen en `config/paths.yaml`, creado a partir de `config/paths.example.yaml`. El archivo local `paths.yaml` no se versiona para evitar subir rutas personales o bases pesadas.
