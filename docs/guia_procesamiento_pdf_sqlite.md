# Guia de procesamiento de PDFs y generacion de SQLite

Esta guia explica como se procesan las fichas PDF CEV, como se usan las regiones de extraccion y como se genera la base SQLite consolidada.

## Insumos

El procesamiento usa tres insumos principales:

- PDFs descargados en `cev_pdf_dir`.
- Regiones de extraccion en `pdf_regions_path`, actualmente `config/table_regions.json`.
- Rutas de salida definidas en `config/paths.yaml`, especialmente `pdf_extract_out_dir` y `cev_db_path`.

Las imagenes siguientes son la ficha de referencia exportada a JPG. Sirven para documentar visualmente que informacion aparece en cada pagina y sobre que estructura se marcaron las regiones.

![Pagina 1: descripcion general y resumen energetico](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0001.jpg)

![Pagina 2: requerimientos, equipos y aportes energeticos](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0002.jpg)

![Pagina 3: envolvente, elementos opacos y traslucidos](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0003.jpg)

![Pagina 4: puentes termicos y materialidades](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0004.jpg)

![Pagina 5: detalle de soluciones constructivas](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0005.jpg)

![Pagina 6: exigencias normativas y antecedentes complementarios](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0006.jpg)

![Pagina 7: informacion final del informe](img/ficha_la_cisterna_edificio_goycolea_100_22397-depto_201_departamento_precalificadas_ver_informe_page-0007.jpg)

## Marcado de regiones

El archivo `config/table_regions.json` fue generado con `scripts/pdf_coord_picker_multipage.py`. Ese script abre un PDF de referencia y permite dibujar rectangulos por pagina. Cada rectangulo queda guardado con coordenadas PDF y un identificador como `page_01_region_01`.

La interfaz del picker muestra la pagina renderizada del PDF y superpone las regiones ya definidas. Al seleccionar o crear rectangulos, el script registra las coordenadas en puntos PDF y las guarda en el JSON de regiones.

![Interfaz del picker de coordenadas para definir regiones PDF](img/interfaz_reion_picker.png)

Para revisar o regenerar las regiones:

```bash
python scripts/pdf_coord_picker_multipage.py \
  --pdf ruta/a/ficha_referencia.pdf
```

Por defecto el JSON se guarda en la ruta `pdf_regions_path` definida en `config/paths.yaml`. Tambien puedes indicar una salida explicita:

```bash
python scripts/pdf_coord_picker_multipage.py \
  --pdf ruta/a/ficha_referencia.pdf \
  --out config/table_regions.json
```

## Extraccion y compilacion

El script principal de procesamiento es `scripts/extract_visible_rows_and_build_db.py`:

```bash
python scripts/extract_visible_rows_and_build_db.py --recursive
```

Si no pasas rutas, el script toma desde `config/paths.yaml`:

- `cev_pdf_dir` como carpeta de entrada.
- `pdf_regions_path` como configuracion de regiones.
- `pdf_extract_out_dir` como carpeta de salida intermedia.
- `cev_db_path` como base SQLite final.

Para una ejecucion manual con rutas explicitas:

```bash
python scripts/extract_visible_rows_and_build_db.py \
  --input-dir data/raw/cev_fichas \
  --recursive \
  --regions config/table_regions.json \
  --out-dir data/interim/output_cev_compiled \
  --db data/processed/cev_compiled.sqlite
```

## Procesamiento supervisado por lotes

Para lotes grandes conviene usar `scripts/run_cev_supervised.py`, que llama al extractor PDF por PDF y registra fallos sin detener todo el proceso:

```bash
python scripts/run_cev_supervised.py \
  --folder-mtime-before "2026-05-11 17:00:00" \
  --progress-every 100
```

Este runner tambien usa `config/paths.yaml` por defecto. El log de fallos se escribe en `failed_processing_log_path`.

## Seleccion de documentos procesables

Antes de construir la base SQLite se hizo una revision estadistica de la estructura de las fichas descargadas, contando cuantas paginas tenia cada documento. Ese diagnostico mostro que existian dos formatos principales: fichas de 4 paginas y fichas de 7 paginas.

Las fichas de 4 paginas corresponden a una version antigua del informe CEV y contienen menos informacion que el formato de 7 paginas. Como el extractor fue disenado sobre las regiones del formato completo de 7 paginas, se descarto el procesamiento de los documentos de 4 paginas para evitar registros incompletos o no comparables.

En total se identificaron 262.913 fichas. De ellas, 33.940 tenian 4 paginas, equivalente a 12,9% del total, y no fueron incorporadas a la base de datos. El resto corresponde a fichas de 7 paginas y constituye el universo procesado para la SQLite consolidada.

## Como se construye la base SQLite

El extractor abre cada PDF con PyMuPDF, valida que tenga 7 paginas y recorre las regiones definidas en `table_regions.json`. Para cada region obtiene texto visible, limpia valores numericos y arma registros estructurados.

La base se inicializa con tablas tematicas:

- `descripcion_general`: identificacion del documento, region, comuna, direccion, tipo de vivienda, tipo de inmueble, superficie, etiqueta y datos generales.
- `requerimientos`: consumos por ACS, iluminacion, calefaccion, ERNC, consumo total y emisiones.
- `descripcion_equipos`: descripcion y consumo de equipos de proyecto y referencia.
- `requerimientos_total`: consumos primarios, aportes fotovoltaicos, solar termico y coeficiente energetico.
- `elementos_opacos`: areas y transmitancias por orientacion.
- `elementos_traslucidos`: ventanas u otros elementos traslucidos por orientacion.
- `puentes_termicos`: valores por orientacion y tipo de puente termico.
- `materialidades`: descripcion de materialidades por elemento constructivo.
- `exigencia_u_normativa`: exigencias normativas de transmitancia por elemento.

Cada tabla usa `document_id` como llave principal o como parte de una llave compuesta. El script usa `INSERT OR REPLACE`, por lo que volver a procesar un PDF actualiza sus registros en la base.

El campo `tipo_inmueble` es una clasificacion derivada a partir de `tipo_vivienda`. Se calcula en el notebook `scripts/corregir_tipo_inmueble_cev.ipynb`: primero normaliza el texto de `tipo_vivienda`, luego busca patrones asociados a departamentos (`departamento`, `depto`, `edificio`, `torre`, `condominio`, `duplex`), casas pareadas o continuas (`pareada`, `pareado`, `continua`) y casas aisladas (`aislada`, `aislado`, `casa`). El resultado esperado queda en tres categorias principales: `depto`, `casa_pareada` y `casa_aislada`; si no hay coincidencia suficiente, se completa como `indeterminado`. Ese notebook agrega la columna con `ALTER TABLE descripcion_general ADD COLUMN tipo_inmueble TEXT` cuando no existe y luego actualiza cada registro por `document_id`.

## Diagrama entidad-relacion

El diagrama entidad-relacion esta disponible como archivo Mermaid en `docs/diagrams/cev_sqlite_er.mmd`. Las relaciones son logicas: el esquema SQLite actual no declara `FOREIGN KEY`, pero las tablas se conectan por `document_id`.

```mermaid
erDiagram
    descripcion_general ||--|| requerimientos : "document_id"
    descripcion_general ||--|| descripcion_equipos : "document_id"
    descripcion_general ||--|| requerimientos_total : "document_id"
    descripcion_general ||--o{ elementos_opacos : "document_id"
    descripcion_general ||--o{ elementos_traslucidos : "document_id"
    descripcion_general ||--o{ puentes_termicos : "document_id"
    descripcion_general ||--o{ materialidades : "document_id"
    descripcion_general ||--o{ exigencia_u_normativa : "document_id"

    descripcion_general {
        TEXT document_id PK
        TEXT source_pdf
        TEXT estado_cev
        TEXT region
        TEXT comuna
        TEXT direccion
        TEXT rol_vivienda
        TEXT tipo_vivienda
        TEXT tipo_inmueble
        REAL superficie_util
        REAL ahorro
        TEXT etiqueta
        REAL demanda_calef
        REAL demanda_refri
        TEXT fecha_emision
        TEXT zona_termica
        TEXT solicitante
        TEXT evaluador
    }

    requerimientos {
        TEXT document_id PK
        REAL acs_kwhm2
        REAL ilum_kwhm2
        REAL calef_kwhm2
        REAL ernc_kwhm2
        REAL consumo_kwhm2
        REAL emisiones_co2
    }

    descripcion_equipos {
        TEXT document_id PK
        TEXT calef_proyec_descripcion
        TEXT ilum_proyec_descripcion
        TEXT acs_proyec_descripcion
        TEXT ernc_proyec_descripcion
        REAL calef_proyec_kwh
        REAL ilum_proyec_kwh
        REAL acs_proyec_kwh
        REAL ernc_proyec_kwh
        TEXT calef_ref_descripcion
        TEXT ilum_ref_descripcion
        TEXT acs_ref_descripcion
        TEXT ernc_ref_descripcion
        REAL calef_ref_kwh
        REAL ilum_ref_kwh
        REAL acs_ref_kwh
        REAL ernc_ref_kwh
    }

    requerimientos_total {
        TEXT document_id PK
        REAL consumo_primario_calef
        REAL consumo_primario_acs
        REAL consumo_primario_ilum
        REAL consumo_primario_vent
        REAL generacion_pv
        REAL pv_consumos_basicos
        REAL dif_pv_consumo
        REAL st_calef
        REAL st_acs
        REAL consumo_primario
        REAL aporte_pv
        REAL consumos_energia_externa
        REAL consumo_primario_2
        REAL energia_ref
        REAL coef_energetico_c
    }

    elementos_opacos {
        TEXT document_id PK
        TEXT orientacion PK
        REAL area
        REAL u
    }

    elementos_traslucidos {
        TEXT document_id PK
        TEXT orientacion PK
        REAL area
        REAL u
    }

    puentes_termicos {
        TEXT document_id PK
        TEXT orientacion PK
        REAL p01
        REAL p02
        REAL p03
        REAL p04
        REAL p05
    }

    materialidades {
        TEXT document_id PK
        TEXT elemento PK
        TEXT descripcion
    }

    exigencia_u_normativa {
        TEXT document_id PK
        TEXT elemento PK
        TEXT exigencia_texto
        REAL exigencia_u
    }
```

## Salidas

Las salidas principales son:

```text
pdf_extract_out_dir/
  *_visible_rows.csv        # archivos auxiliares por region, si se generan durante depuracion
cev_db_path                 # base SQLite consolidada
failed_processing_log_path  # errores por PDF cuando se usa run_cev_supervised.py
```

La base SQLite no se versiona en Git porque puede crecer rapidamente. Para compartir resultados, conviene exportar tablas o consultas puntuales a `reports/` o `data/processed/` segun el peso del archivo.
