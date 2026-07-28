# Guía de carga de ubicaciones CEV y vista PostGIS

Esta guía documenta la conversión del registro Excel de evaluaciones CEV, su
carga en PostgreSQL/PostGIS y la construcción de una vista materializada para
consulta desde QGIS.

## Archivos del proceso

- Fuente: `data/raw/2026-07-03_DITEC_HyEE_Evaluaciones CEV con ubicación_V3.xlsx`.
- CSV normalizado:
  `data/processed/2026-07-03_DITEC_HyEE_Evaluaciones_CEV_ubicaciones.csv`.
- Conversor: `scripts/convertir_ubicaciones_xlsx.py`.
- Carga de ubicaciones: `sql/cargar_ubicaciones.sql`.
- Vista georreferenciada:
  `sql/crear_vista_fichas_cev_georreferenciadas.sql`.

Los comandos de esta guía deben ejecutarse desde la raíz del repositorio.

## Estructura del Excel

El libro contiene las hojas `V1 2013 a 2019`, `V2 2018 a actualidad` y
`Hoja2`. Se usa `Hoja2` porque es la versión consolidada más completa. Contiene
259.787 registros e incorpora nombre de proyecto, roles y coordenadas. Cargar
también las otras hojas introduciría duplicados.

El conversor utiliza solamente la biblioteca estándar de Python; no requiere
`pandas` ni `openpyxl`. Lee directamente el XML interno del archivo XLSX.

## Conversión a CSV

Ejecutar:

```bash
python3 scripts/convertir_ubicaciones_xlsx.py
```

También se pueden indicar rutas diferentes:

```bash
python3 scripts/convertir_ubicaciones_xlsx.py \
  --input "data/raw/archivo.xlsx" \
  --output "data/processed/ubicaciones.csv"
```

El resultado es un CSV UTF-8, delimitado por comas y con fechas ISO
`YYYY-MM-DD`. Las columnas resultantes son:

```text
fila_origen
nombre_proyecto
fecha_emision
estado_cev
region_codigo
comuna
zona_termica
etiqueta
rol_vivienda
rol_proyecto
longitud
latitud
```

Las principales homologaciones con el esquema `fichas_cev` son:

| Campo Excel | Campo CSV | Tratamiento |
|---|---|---|
| `FechaEmisionEtiqueta` | `fecha_emision` | Serial Excel a fecha ISO |
| `TipoCalificación` | `estado_cev` | `calificada` o `precalificada` |
| `Región` | `region_codigo` | Se conserva como código numérico |
| `ZonaTérmica` | `zona_termica` | Se extrae la letra `A` a `I` |
| `Calificación` | `etiqueta` | Nombre compatible con la base |
| `RolVivienda` | `rol_vivienda` | Nombre compatible con la base |
| `Longitud`, `Latitud` | `longitud`, `latitud` | Valores numéricos EPSG:4326 |

Se usa `region_codigo` porque el Excel almacena un número, mientras que
`descripcion_general.region` contiene el nombre de la región.

## Conexión a PostgreSQL

Los scripts no guardan credenciales. La conexión se entrega a `psql` mediante
sus parámetros habituales o variables de entorno:

```bash
export PGHOST="<host>"
export PGPORT="5432"
export PGDATABASE="geonode_local_data"
export PGUSER="<usuario>"
export PGPASSWORD="<contraseña>"
```

No se debe guardar `PGPASSWORD` en archivos versionados.

La base debe tener PostGIS habilitado y debe existir el esquema `fichas_cev`
con las tablas `descripcion_general`, `requerimientos_total` y
`exigencia_u_normativa`.

## Carga de `fichas_cev.ubicaciones`

Ejecutar desde la raíz del repositorio:

```bash
psql -X -v ON_ERROR_STOP=1 -f sql/cargar_ubicaciones.sql
```

El proceso:

1. Crea `fichas_cev.ubicaciones` si todavía no existe.
2. Copia el CSV a una tabla temporal mediante `\copy`.
3. Inserta o actualiza registros usando `fila_origen` como clave estable.
4. Crea índices sobre `rol_vivienda + fecha_emision` y `comuna`.
5. Actualiza las estadísticas del optimizador.

La carga es repetible: ejecutar nuevamente el SQL actualiza las filas ya
existentes sin duplicarlas. No elimina registros antiguos que hayan
desaparecido de una versión posterior del CSV.

## Creación de la vista materializada

En una instalación inicial:

```bash
psql -X -v ON_ERROR_STOP=1 \
  -f sql/crear_vista_fichas_cev_georreferenciadas.sql
```

Esto crea:

```text
fichas_cev.fichas_cev_georreferenciadas
```

La vista tiene una fila por `document_id` y combina:

- `descripcion_general`;
- `requerimientos_total`;
- las exigencias de `exigencia_u_normativa` convertidas de filas a columnas;
- la ubicación seleccionada desde `ubicaciones`;
- una geometría `Point` con SRID 4326.

Las exigencias se pivotan con agregaciones filtradas y quedan, por ejemplo,
como `exigencia_u_muro_principal`, `exigencia_u_techo_principal` y
`exigencia_u_sup_vid_principal`. Este diseño evita multiplicar cada ficha por
los diez elementos de la tabla original y resulta más eficiente para QGIS.

La relación con ubicaciones se basa en `rol_vivienda + fecha_emision`. Cuando
existen varios candidatos, se priorizan las coincidencias de comuna, estado
CEV, etiqueta y zona térmica. `ubicacion_id` permite auditar el registro
seleccionado.

Los valores de longitud y latitud se conservan, pero `geom` se genera solamente
si la coordenada cae en el ámbito plausible de Chile:

```text
longitud: -110 a -65
latitud:  -60 a -15
```

Esto evita tratar el marcador `0,0` como una ubicación real. El campo
`ubicacion_coordenada_valida` indica el resultado del control.

## Actualización de la vista

Después de actualizar `ubicaciones` o cualquiera de las tablas fuente:

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY
    fichas_cev.fichas_cev_georreferenciadas;
```

El refresco concurrente es posible porque la vista tiene un índice único sobre
`document_id`.

El SQL de creación es para la instalación inicial. Si la vista ya existe, no
debe ejecutarse nuevamente; corresponde usar `REFRESH MATERIALIZED VIEW`.

## Controles observados en la carga inicial

| Control | Resultado |
|---|---:|
| Filas del Excel consolidado | 259.787 |
| Fichas en la vista | 228.973 |
| Fichas vinculadas al Excel | 228.329 |
| Fichas con geometría válida | 220.960 |
| Fichas sin coincidencia | 644 |
| Ubicaciones seleccionadas con coordenada inválida | 7.369 |
| Cobertura georreferenciada | 96,50 % |

La vista conserva exactamente 228.973 valores únicos de `document_id`. Su
geometría está registrada como `Point`, SRID 4326, y cuenta con un índice
espacial GiST.

## Visualización en QGIS

1. Crear una conexión PostgreSQL a la base `geonode_local_data`.
2. Abrir el esquema `fichas_cev`.
3. Agregar `fichas_cev_georreferenciadas`.
4. Usar `document_id` como identificador único si QGIS lo solicita.

QGIS debería reconocer automáticamente:

```text
geometría: geom
tipo: Point
CRS: EPSG:4326
```

Las 644 fichas sin coincidencia y las coordenadas inválidas permanecen en la
vista con `geom = NULL`; por ello no se dibujan, pero siguen disponibles para
auditoría tabular.
