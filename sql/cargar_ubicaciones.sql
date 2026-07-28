\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS fichas_cev.ubicaciones (
    ubicacion_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fila_origen integer NOT NULL UNIQUE,
    nombre_proyecto text,
    fecha_emision date NOT NULL,
    estado_cev text,
    region_codigo smallint,
    comuna text,
    zona_termica text,
    etiqueta text,
    rol_vivienda text,
    rol_proyecto text,
    longitud double precision NOT NULL
        CHECK (longitud BETWEEN -180 AND 180),
    latitud double precision NOT NULL
        CHECK (latitud BETWEEN -90 AND 90)
);

BEGIN;

CREATE TEMP TABLE ubicaciones_carga (
    fila_origen integer,
    nombre_proyecto text,
    fecha_emision date,
    estado_cev text,
    region_codigo smallint,
    comuna text,
    zona_termica text,
    etiqueta text,
    rol_vivienda text,
    rol_proyecto text,
    longitud double precision,
    latitud double precision
) ON COMMIT DROP;

\copy ubicaciones_carga FROM 'data/processed/2026-07-03_DITEC_HyEE_Evaluaciones_CEV_ubicaciones.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')

INSERT INTO fichas_cev.ubicaciones (
    fila_origen,
    nombre_proyecto,
    fecha_emision,
    estado_cev,
    region_codigo,
    comuna,
    zona_termica,
    etiqueta,
    rol_vivienda,
    rol_proyecto,
    longitud,
    latitud
)
SELECT
    fila_origen,
    nombre_proyecto,
    fecha_emision,
    estado_cev,
    region_codigo,
    comuna,
    zona_termica,
    etiqueta,
    rol_vivienda,
    rol_proyecto,
    longitud,
    latitud
FROM ubicaciones_carga
ON CONFLICT (fila_origen) DO UPDATE SET
    nombre_proyecto = excluded.nombre_proyecto,
    fecha_emision = excluded.fecha_emision,
    estado_cev = excluded.estado_cev,
    region_codigo = excluded.region_codigo,
    comuna = excluded.comuna,
    zona_termica = excluded.zona_termica,
    etiqueta = excluded.etiqueta,
    rol_vivienda = excluded.rol_vivienda,
    rol_proyecto = excluded.rol_proyecto,
    longitud = excluded.longitud,
    latitud = excluded.latitud;

COMMIT;

CREATE INDEX IF NOT EXISTS ubicaciones_rol_fecha_idx
    ON fichas_cev.ubicaciones (rol_vivienda, fecha_emision);

CREATE INDEX IF NOT EXISTS ubicaciones_comuna_idx
    ON fichas_cev.ubicaciones (comuna);

ANALYZE fichas_cev.ubicaciones;

SELECT
    count(*) AS filas,
    count(DISTINCT (rol_vivienda, fecha_emision)) AS claves_rol_fecha,
    min(fecha_emision) AS fecha_min,
    max(fecha_emision) AS fecha_max
FROM fichas_cev.ubicaciones;
