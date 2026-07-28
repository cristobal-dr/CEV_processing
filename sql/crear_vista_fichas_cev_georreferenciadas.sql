SET work_mem = '64MB';
SET maintenance_work_mem = '128MB';

CREATE MATERIALIZED VIEW fichas_cev.fichas_cev_georreferenciadas AS
WITH exigencias AS (
    SELECT
        document_id,
        max(exigencia_u) FILTER (WHERE elemento = 'muro_principal')
            AS exigencia_u_muro_principal,
        max(exigencia_u) FILTER (WHERE elemento = 'muro_secundario')
            AS exigencia_u_muro_secundario,
        max(exigencia_u) FILTER (WHERE elemento = 'techo_principal')
            AS exigencia_u_techo_principal,
        max(exigencia_u) FILTER (WHERE elemento = 'techo_secundario')
            AS exigencia_u_techo_secundario,
        max(exigencia_u) FILTER (WHERE elemento = 'piso_principal')
            AS exigencia_u_piso_principal,
        max(exigencia_u) FILTER (WHERE elemento = 'puerta_principal')
            AS exigencia_u_puerta_principal,
        max(exigencia_u) FILTER (WHERE elemento = 'sup_vid_principal')
            AS exigencia_u_sup_vid_principal,
        max(exigencia_u) FILTER (WHERE elemento = 'sup_vid_secundaria')
            AS exigencia_u_sup_vid_secundaria,
        max(exigencia_u) FILTER (WHERE elemento = 'ventilacion')
            AS exigencia_u_ventilacion,
        max(exigencia_u) FILTER (WHERE elemento = 'infiltraciones')
            AS exigencia_u_infiltraciones
    FROM fichas_cev.exigencia_u_normativa
    GROUP BY document_id
)
SELECT
    d.document_id,
    d.source_pdf,
    d.estado_cev,
    d.region,
    d.comuna,
    d.direccion,
    d.rol_vivienda,
    d.tipo_vivienda,
    d.superficie_util,
    d.ahorro,
    d.etiqueta,
    d.demanda_calef,
    d.demanda_refri,
    to_date(d.fecha_emision, 'DD-MM-YYYY') AS fecha_emision,
    d.zona_termica,
    d.solicitante,
    d.evaluador,
    d.tipo_inmueble,
    r.consumo_primario_calef,
    r.consumo_primario_acs,
    r.consumo_primario_ilum,
    r.consumo_primario_vent,
    r.generacion_pv,
    r.pv_consumos_basicos,
    r.dif_pv_consumo,
    r.st_calef,
    r.st_acs,
    r.consumo_primario,
    r.aporte_pv,
    r.consumos_energia_externa,
    r.consumo_primario_2,
    r.energia_ref,
    r.coef_energetico_c,
    e.exigencia_u_muro_principal,
    e.exigencia_u_muro_secundario,
    e.exigencia_u_techo_principal,
    e.exigencia_u_techo_secundario,
    e.exigencia_u_piso_principal,
    e.exigencia_u_puerta_principal,
    e.exigencia_u_sup_vid_principal,
    e.exigencia_u_sup_vid_secundaria,
    e.exigencia_u_ventilacion,
    e.exigencia_u_infiltraciones,
    u.ubicacion_id,
    u.nombre_proyecto,
    u.region_codigo,
    u.rol_proyecto,
    u.longitud,
    u.latitud,
    (
        u.longitud BETWEEN -110 AND -65
        AND u.latitud BETWEEN -60 AND -15
    ) AS ubicacion_coordenada_valida,
    CASE
        WHEN u.ubicacion_id IS NULL
          OR u.longitud NOT BETWEEN -110 AND -65
          OR u.latitud NOT BETWEEN -60 AND -15
        THEN NULL
        ELSE ST_SetSRID(ST_MakePoint(u.longitud, u.latitud), 4326)
    END::geometry(Point, 4326) AS geom
FROM fichas_cev.descripcion_general AS d
JOIN fichas_cev.requerimientos_total AS r USING (document_id)
JOIN exigencias AS e USING (document_id)
LEFT JOIN LATERAL (
    SELECT ubicacion.*
    FROM fichas_cev.ubicaciones AS ubicacion
    WHERE ubicacion.rol_vivienda = d.rol_vivienda
      AND ubicacion.fecha_emision = to_date(d.fecha_emision, 'DD-MM-YYYY')
    ORDER BY
        (lower(btrim(ubicacion.comuna)) = lower(btrim(d.comuna))) DESC,
        (ubicacion.estado_cev = d.estado_cev) DESC,
        (ubicacion.etiqueta = d.etiqueta) DESC,
        (ubicacion.zona_termica = d.zona_termica) DESC,
        ubicacion.ubicacion_id
    LIMIT 1
) AS u ON true;

CREATE UNIQUE INDEX fichas_cev_georreferenciadas_document_id_idx
    ON fichas_cev.fichas_cev_georreferenciadas (document_id);

CREATE INDEX fichas_cev_georreferenciadas_geom_idx
    ON fichas_cev.fichas_cev_georreferenciadas USING gist (geom);

CREATE INDEX fichas_cev_georreferenciadas_comuna_idx
    ON fichas_cev.fichas_cev_georreferenciadas (comuna);

COMMENT ON MATERIALIZED VIEW fichas_cev.fichas_cev_georreferenciadas IS
    'Fichas CEV con requerimientos totales, exigencias U pivotadas y ubicación EPSG:4326.';

COMMENT ON COLUMN fichas_cev.fichas_cev_georreferenciadas.geom IS
    'Punto PostGIS de longitud/latitud con SRID EPSG:4326.';

ANALYZE fichas_cev.fichas_cev_georreferenciadas;
