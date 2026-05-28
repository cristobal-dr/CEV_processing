from __future__ import annotations
import argparse
import csv
import json
import re
import sqlite3
import sys
import statistics
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cev_analisis.paths import default_paths_config, load_paths, path_value

try:
    import pymupdf as fitz
except ImportError:
    import fitz


# =============================================================================
# Configuración base
# =============================================================================

OCCLUDER_TYPES = {"fill-path", "fill-image", "fill-shade"}

DESC_GENERAL_REGION_01_FIELDS = [
    None,   #código CEV: se usará como document_id, no como campo independiente
    "region",
    "comuna",
    "direccion",
    "rol_vivienda",
    "tipo_vivienda",
    "superficie_util",
]

DESC_GENERAL_REGION_02_FIELDS = [
    "zona_termica",
    None,  # superficie útil: se descarta porque ya viene desde página 1
    "solicitante",
    "evaluador",
    None,  # código CEV: se usará como document_id, no como campo independiente
]

REQUERIMIENTOS_REGION_01_FIELDS = [
    "acs_kwhm2",
    "ilum_kwhm2",
    "calef_kwhm2",
    "ernc_kwhm2",
]

EQUIPOS_REGION_04_FIELDS = [
    "calef_proyec_descripcion",
    "ilum_proyec_descripcion",
    "acs_proyec_descripcion",
    "ernc_proyec_descripcion",
]

EQUIPOS_REGION_05_FIELDS = [
    "calef_proyec_kwh",
    "ilum_proyec_kwh",
    "acs_proyec_kwh",
    "ernc_proyec_kwh",
]

EQUIPOS_REGION_06_FIELDS = [
    "calef_ref_descripcion",
    "ilum_ref_descripcion",
    "acs_ref_descripcion",
    "ernc_ref_descripcion",
]

EQUIPOS_REGION_07_FIELDS = [
    "calef_ref_kwh",
    "ilum_ref_kwh",
    "acs_ref_kwh",
    "ernc_ref_kwh",
]

REQ_TOTAL_REGION_08_FIELDS = [
    "consumo_primario_calef",
    "consumo_primario_acs",
    "consumo_primario_ilum",
    "consumo_primario_vent",
]

REQ_TOTAL_REGION_09_FIELDS = [
    "generacion_pv",
    "pv_consumos_basicos",
    "dif_pv_consumo",
]

REQ_TOTAL_REGION_10_FIELDS = [
    "st_calef",
    "st_acs",
]

REQ_TOTAL_REGION_11_FIELDS = [
    "consumo_primario",
    "aporte_pv",
    "consumos_energia_externa",
]

REQ_TOTAL_REGION_12_FIELDS = [
    "consumo_primario_2",
    "energia_ref",
    "coef_energetico_c",
]

ELEMENTOS_MATERIALIDADES = [
    "muro_principal",
    "muro_secundario",
    "piso_principal",
    "puerta_principal",
    "techo_principal",
    "techo_secundario",
    "sup_vid_principal",
    "sup_vid_secundaria",
    "ventilacion",
    "infiltraciones",
]

ELEMENTOS_OPACOS_ORIENTACIONES = [
    "horiz",
    "n",
    "ne",
    "e",
    "se",
    "s",
    "so",
    "o",
    "no",
    "pisos",
]

ELEMENTOS_TRASLUCIDOS_ORIENTACIONES = [
    "horiz",
    "n",
    "ne",
    "e",
    "se",
    "s",
    "so",
    "o",
    "no",
]

PUENTES_TERMICOS_ORIENTACIONES = [
    "n",
    "ne",
    "e",
    "se",
    "s",
    "so",
    "o",
    "no",
]

PUENTES_TERMICOS_FIELDS = [
    "p01",
    "p02",
    "p03",
    "p04",
    "p05",
]


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass(frozen=True)
class Char:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    seqno: int
    layer: str = ""

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    seqno: int
    layer: str = ""

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1


# =============================================================================
# Utilidades generales
# =============================================================================
def infer_estado_cev_from_filename(filename: str) -> str | None:
    """
    Deduce el estado CEV desde el nombre del archivo.

    Ejemplos:
    - *_precalificadas_ver_informe.pdf -> precalificada
    - *_calificadas_ver_informe.pdf    -> calificada

    Importante: se evalúa primero 'precalificada', porque contiene
    la palabra 'calificada' como substring.
    """
    name = filename.lower()

    if re.search(r"(^|[_\-\s])precalificad[ao]s?([_\-\s\.]|$)", name):
        return "precalificada"

    if re.search(r"(^|[_\-\s])calificad[ao]s?([_\-\s\.]|$)", name):
        return "calificada"

    return None

def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ")
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text.strip()


def safe_name(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"[^A-Za-z0-9_\-.]+", "_", text)
    return text.strip("._") or "sin_nombre"


def write_csv(path: Path, rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerows(rows)


def rect_area(bbox: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def rects_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (
        a[0] >= b[2]
        or a[2] <= b[0]
        or a[1] >= b[3]
        or a[3] <= b[1]
    )


def bbox_overlap_ratio(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    area_a = rect_area(a)
    if area_a <= 0:
        return 0.0

    return intersection_area(a, b) / area_a


# =============================================================================
# Números y fechas
# =============================================================================

NUMBER_RE = re.compile(
    r"""
    (?<![\w])
    [-+]?
    (?:
        \d{1,3}(?:\.\d{3})+(?:,\d+)? |
        \d+(?:,\d+)?
    )
    %?
    (?![\w])
    """,
    re.VERBOSE,
)

DATE_RE = re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b")


def parse_chilean_number(value: Any) -> float | None:
    text = normalize_text(value)

    if not text:
        return None

    text = text.replace("%", "")
    text = text.replace(".", "")
    text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def extract_numbers(text: str) -> list[str]:
    text = normalize_text(text)
    return [m.group(0) for m in NUMBER_RE.finditer(text)]


def extract_first_number(text: str) -> float | None:
    nums = extract_numbers(text)
    if not nums:
        return None
    return parse_chilean_number(nums[0])


def extract_last_number(text: str) -> float | None:
    nums = extract_numbers(text)
    if not nums:
        return None
    return parse_chilean_number(nums[-1])


def extract_first_date(text: str) -> str | None:
    match = DATE_RE.search(normalize_text(text))
    return match.group(0) if match else None


def value_after_colon(text: str) -> str:
    text = normalize_text(text)

    if ":" in text:
        return normalize_text(text.split(":", 1)[1])

    return text


def region_text(rows: list[str]) -> str:
    return normalize_text(" ".join(rows))


# =============================================================================
# Texto visible
# =============================================================================

def get_later_occluders(
    page: fitz.Page,
    min_occluder_area: float = 20.0,
) -> list[tuple[int, tuple[float, float, float, float], str]]:
    occluders: list[tuple[int, tuple[float, float, float, float], str]] = []

    for seqno, item in enumerate(page.get_bboxlog()):
        kind = item[0]
        bbox = tuple(float(v) for v in item[1])

        if kind in OCCLUDER_TYPES and rect_area(bbox) >= min_occluder_area:
            occluders.append((seqno, bbox, kind))

    return occluders


def is_char_occluded(
    bbox: tuple[float, float, float, float],
    seqno: int,
    occluders: list[tuple[int, tuple[float, float, float, float], str]],
    overlap_threshold: float = 0.55,
) -> bool:
    area = rect_area(bbox)

    if area <= 0:
        return False

    for occ_seqno, occ_bbox, _kind in occluders:
        if occ_seqno <= seqno:
            continue

        if not rects_intersect(bbox, occ_bbox):
            continue

        overlap = intersection_area(bbox, occ_bbox) / area

        if overlap >= overlap_threshold:
            return True

    return False


def visible_words_from_page(
    page: fitz.Page,
    min_occluder_area: float = 20.0,
    overlap_threshold: float = 0.55,
    line_y_tol: float = 2.8,
    gap_factor: float = 1.8,
    min_gap: float = 2.0,
) -> list[Word]:
    occluders = get_later_occluders(
        page=page,
        min_occluder_area=min_occluder_area,
    )

    visible_words: list[Word] = []

    for span in page.get_texttrace():
        span_seqno = int(span.get("seqno", -1))
        layer = str(span.get("layer", ""))

        opacity = span.get("opacity", 1)
        try:
            if float(opacity) <= 0.05:
                continue
        except Exception:
            pass

        chars: list[Char] = []

        for ch in span.get("chars", []):
            char_text = chr(ch[0])
            bbox = tuple(float(v) for v in ch[3])

            if not char_text.isspace():
                if is_char_occluded(
                    bbox=bbox,
                    seqno=span_seqno,
                    occluders=occluders,
                    overlap_threshold=overlap_threshold,
                ):
                    char_text = " "

            chars.append(
                Char(
                    text=char_text,
                    x0=bbox[0],
                    y0=bbox[1],
                    x1=bbox[2],
                    y1=bbox[3],
                    seqno=span_seqno,
                    layer=layer,
                )
            )

        if not chars:
            continue

        lines: list[dict[str, Any]] = []

        for c in sorted(chars, key=lambda item: (item.cy, item.x0)):
            if not lines or abs(c.cy - lines[-1]["cy"]) > line_y_tol:
                lines.append({"cy": c.cy, "chars": [c]})
            else:
                lines[-1]["chars"].append(c)
                lines[-1]["cy"] = (
                    sum(item.cy for item in lines[-1]["chars"])
                    / len(lines[-1]["chars"])
                )

        for line in lines:
            line_chars = sorted(line["chars"], key=lambda item: item.x0)

            widths = [
                max(0.1, c.x1 - c.x0)
                for c in line_chars
                if not c.text.isspace()
            ]

            median_width = statistics.median(widths) if widths else 3.0
            gap_threshold = max(min_gap, gap_factor * median_width)

            group: list[Char] = []
            previous: Char | None = None

            def flush_group() -> None:
                nonlocal group

                if not group:
                    return

                text = normalize_text("".join(item.text for item in group))

                if text:
                    visible_words.append(
                        Word(
                            text=text,
                            x0=min(item.x0 for item in group),
                            y0=min(item.y0 for item in group),
                            x1=max(item.x1 for item in group),
                            y1=max(item.y1 for item in group),
                            seqno=max(item.seqno for item in group),
                            layer=",".join(sorted(set(item.layer for item in group))),
                        )
                    )

                group = []

            for c in line_chars:
                gap = c.x0 - previous.x1 if previous is not None else 0.0

                big_gap = (
                    previous is not None
                    and not previous.text.isspace()
                    and gap > gap_threshold
                )

                if c.text.isspace() or big_gap:
                    flush_group()
                    previous = c
                    continue

                group.append(c)
                previous = c

            flush_group()

    return sorted(visible_words, key=lambda item: (item.y0, item.x0, item.seqno))


# =============================================================================
# Región, deduplicación y filas
# =============================================================================

def words_in_rect(
    words: list[Word],
    rect: fitz.Rect,
    margin: float = 1.0,
) -> list[Word]:
    selected: list[Word] = []

    for w in words:
        if (
            rect.x0 - margin <= w.cx <= rect.x1 + margin
            and rect.y0 - margin <= w.cy <= rect.y1 + margin
        ):
            selected.append(w)

    return sorted(selected, key=lambda item: (item.y0, item.x0, item.seqno))


def deduplicate_words_by_geometry(
    words: list[Word],
    coord_tol: float = 0.7,
    overlap_threshold: float = 0.80,
) -> list[Word]:
    if not words:
        return []

    seen: set[tuple[Any, ...]] = set()
    first_pass: list[Word] = []

    def q(value: float) -> int:
        return round(value / coord_tol)

    for w in sorted(words, key=lambda item: (item.y0, item.x0, item.seqno)):
        key = (
            normalize_text(w.text).lower(),
            q(w.x0),
            q(w.y0),
            q(w.x1),
            q(w.y1),
        )

        if key in seen:
            continue

        seen.add(key)
        first_pass.append(w)

    unique_words: list[Word] = []

    for w in sorted(first_pass, key=lambda item: (item.y0, item.x0, item.seqno)):
        duplicate = False

        for u in unique_words:
            same_text = normalize_text(w.text).lower() == normalize_text(u.text).lower()

            if not same_text:
                continue

            same_position = (
                abs(w.cx - u.cx) <= coord_tol
                and abs(w.cy - u.cy) <= coord_tol
            )

            high_overlap = (
                bbox_overlap_ratio(w.bbox, u.bbox) >= overlap_threshold
                or bbox_overlap_ratio(u.bbox, w.bbox) >= overlap_threshold
            )

            if same_position or high_overlap:
                duplicate = True
                break

        if not duplicate:
            unique_words.append(w)

    return sorted(unique_words, key=lambda item: (item.y0, item.x0, item.seqno))


def group_words_by_rows(
    words: list[Word],
    y_tol: float = 3.5,
) -> list[list[Word]]:
    if not words:
        return []

    rows: list[dict[str, Any]] = []

    for w in sorted(words, key=lambda item: (item.cy, item.x0)):
        if not rows or abs(w.cy - rows[-1]["cy"]) > y_tol:
            rows.append({"cy": w.cy, "words": [w]})
        else:
            rows[-1]["words"].append(w)
            rows[-1]["cy"] = (
                sum(item.cy for item in rows[-1]["words"])
                / len(rows[-1]["words"])
            )

    return [
        sorted(row["words"], key=lambda item: item.x0)
        for row in rows
    ]


def rows_to_phrase_records(
    region_id: str,
    page_number: int,
    words: list[Word],
    y_tol: float = 3.5,
) -> list[list[Any]]:
    grouped_rows = group_words_by_rows(words, y_tol=y_tol)

    output: list[list[Any]] = [
        ["region_id", "page", "row_index", "text", "x0", "y0", "x1", "y1", "n_words"]
    ]

    for row_index, row_words in enumerate(grouped_rows, start=1):
        row_words = deduplicate_words_by_geometry(row_words)

        if not row_words:
            continue

        row_text = normalize_text(" ".join(w.text for w in row_words))

        output.append(
            [
                region_id,
                page_number,
                row_index,
                row_text,
                round(min(w.x0 for w in row_words), 3),
                round(min(w.y0 for w in row_words), 3),
                round(max(w.x1 for w in row_words), 3),
                round(max(w.y1 for w in row_words), 3),
                len(row_words),
            ]
        )

    return output


def extract_region_rows(
    doc: fitz.Document,
    regions: list[dict[str, Any]],
    rows_dir: Path | None,
    min_occluder_area: float,
    overlap_threshold: float,
    region_margin: float,
    y_tol: float,
    dedup_coord_tol: float,
) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    visible_words_cache: dict[int, list[Word]] = {}
    collected_rows: dict[str, list[str]] = {}
    collected_row_records: dict[str, list[dict[str, Any]]] = {}

    for region in regions:
        region_id = str(region["id"])
        page_number = int(region["page"])

        page = doc[page_number - 1]

        rect = fitz.Rect(
            float(region["x0"]),
            float(region["y0"]),
            float(region["x1"]),
            float(region["y1"]),
        )

        if page_number not in visible_words_cache:
            page_words = visible_words_from_page(
                page=page,
                min_occluder_area=min_occluder_area,
                overlap_threshold=overlap_threshold,
            )

            page_words = deduplicate_words_by_geometry(
                page_words,
                coord_tol=dedup_coord_tol,
            )

            visible_words_cache[page_number] = page_words

        region_words = words_in_rect(
            words=visible_words_cache[page_number],
            rect=rect,
            margin=region_margin,
        )

        region_words = deduplicate_words_by_geometry(
            region_words,
            coord_tol=dedup_coord_tol,
        )

        if not region_words:
            collected_rows[region_id] = []
            collected_row_records[region_id] = []
            continue

        rows_data = rows_to_phrase_records(
            region_id=region_id,
            page_number=page_number,
            words=region_words,
            y_tol=y_tol,
        )

        text_rows: list[str] = []
        row_records: list[dict[str, Any]] = []

        for row in rows_data[1:]:
            text = normalize_text(row[3])

            if not text:
                continue

            text_rows.append(text)

            row_records.append(
                {
                    "region_id": row[0],
                    "page": int(row[1]),
                    "row_index": int(row[2]),
                    "text": text,
                    "x0": float(row[4]),
                    "y0": float(row[5]),
                    "x1": float(row[6]),
                    "y1": float(row[7]),
                    "n_words": int(row[8]),
                }
            )

        collected_rows[region_id] = text_rows
        collected_row_records[region_id] = row_records

        if rows_dir is not None:
            rows_csv = rows_dir / f"{safe_name(region_id)}_visible_rows.csv"
            write_csv(rows_csv, rows_data)
            print(f"[OK] {region_id} -> filas={len(text_rows)} | {rows_csv}")

    return collected_rows, collected_row_records


# =============================================================================
# Compilación lógica por tablas
# =============================================================================

def get_region_rows(collected: dict[str, list[str]], region_id: str) -> list[str]:
    return collected.get(region_id, [])


def get_numeric_values_from_region(
    collected: dict[str, list[str]],
    region_id: str,
    mode: str = "last",
) -> list[float | None]:
    values: list[float | None] = []

    for row in get_region_rows(collected, region_id):
        if mode == "first":
            values.append(extract_first_number(row))
        else:
            values.append(extract_last_number(row))

    return values


def get_text_values_from_region(
    collected: dict[str, list[str]],
    region_id: str,
) -> list[str | None]:
    return [normalize_text(row) for row in get_region_rows(collected, region_id)]


def assign_values_by_order(
    target: dict[str, Any],
    fields: list[str | None],
    values: list[Any],
) -> None:
    for field, value in zip(fields, values):
        if field is None:
            continue
        target[field] = value


def parse_ahorro_etiqueta(rows: list[str]) -> tuple[float | None, str | None]:
    """
    Busca el primer token numérico y toma el token inmediatamente posterior como etiqueta.
    """
    text = region_text(rows)

    if not text:
        return None, None

    tokens = text.split()

    for i, token in enumerate(tokens):
        clean = token.strip()

        if parse_chilean_number(clean) is not None:
            ahorro = parse_chilean_number(clean)
            etiqueta = tokens[i + 1] if i + 1 < len(tokens) else None
            return ahorro, etiqueta

    nums = extract_numbers(text)
    if nums:
        return parse_chilean_number(nums[0]), None

    return None, None


def build_descripcion_general(
    collected: dict[str, list[str]],
    source_pdf: str,
    estado_cev: str | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "document_id": None,
        "source_pdf": source_pdf,
        "estado_cev": estado_cev,
        "region": None,
        "comuna": None,
        "direccion": None,
        "rol_vivienda": None,
        "tipo_vivienda": None,
        "superficie_util": None,
        "ahorro": None,
        "etiqueta": None,
        "demanda_calef": None,
        "demanda_refri": None,
        "fecha_emision": None,
        "zona_termica": None,
        "solicitante": None,
        "evaluador": None,
    }

    # page_01_region_01
    r1 = get_region_rows(collected, "page_01_region_01")
    r1_values = [value_after_colon(row) for row in r1]

    # El primer valor suele ser el código CEV de la etiqueta.
    # No se guarda como columna; se usa solo como respaldo para document_id.
    codigo_from_page1 = r1_values[0] if len(r1_values) >= 1 else None

    assign_values_by_order(record, DESC_GENERAL_REGION_01_FIELDS, r1_values)

    record["superficie_util"] = parse_chilean_number(record.get("superficie_util"))

    # page_01_region_02
    ahorro, etiqueta = parse_ahorro_etiqueta(get_region_rows(collected, "page_01_region_02"))
    record["ahorro"] = ahorro
    record["etiqueta"] = etiqueta

    # page_01_region_03, 04, 05
    record["demanda_calef"] = extract_last_number(
        region_text(get_region_rows(collected, "page_01_region_03"))
    )

    record["demanda_refri"] = extract_last_number(
        region_text(get_region_rows(collected, "page_01_region_04"))
    )

    fecha_text = region_text(get_region_rows(collected, "page_01_region_05"))
    record["fecha_emision"] = extract_first_date(fecha_text) or fecha_text or None

    # page_02_region_01
    r2 = get_region_rows(collected, "page_02_region_01")
    r2_values = [value_after_colon(row) for row in r2]

    # El quinto valor corresponde al código CEV.
    # Se usa como document_id, pero no se guarda como columna codigo_cev.
    codigo_cev_from_page2 = r2_values[4] if len(r2_values) >= 5 else None

    assign_values_by_order(record, DESC_GENERAL_REGION_02_FIELDS, r2_values)

    # document_id único para relacionar todas las tablas.
    # Prioridad: código CEV de página 2; respaldo: código de página 1; último respaldo: nombre archivo.
    record["document_id"] = (
        normalize_text(codigo_cev_from_page2)
        or normalize_text(codigo_from_page1)
        or safe_name(source_pdf)
    )

    return record


def build_requerimientos(
    collected: dict[str, list[str]],
    document_id: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "document_id": document_id,
        "acs_kwhm2": None,
        "ilum_kwhm2": None,
        "calef_kwhm2": None,
        "ernc_kwhm2": None,
        "consumo_kwhm2": None,
        "emisiones_co2": None,
    }

    assign_values_by_order(
        record,
        REQUERIMIENTOS_REGION_01_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_01"),
    )

    record["consumo_kwhm2"] = extract_last_number(region_text(get_region_rows(collected, "page_03_region_02")))
    record["emisiones_co2"] = extract_last_number(region_text(get_region_rows(collected, "page_03_region_03")))

    return record


def build_descripcion_equipos(
    collected: dict[str, list[str]],
    document_id: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "document_id": document_id,
    }

    for field in (
        EQUIPOS_REGION_04_FIELDS
        + EQUIPOS_REGION_05_FIELDS
        + EQUIPOS_REGION_06_FIELDS
        + EQUIPOS_REGION_07_FIELDS
    ):
        record[field] = None

    assign_values_by_order(
        record,
        EQUIPOS_REGION_04_FIELDS,
        get_text_values_from_region(collected, "page_03_region_04"),
    )

    assign_values_by_order(
        record,
        EQUIPOS_REGION_05_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_05"),
    )

    assign_values_by_order(
        record,
        EQUIPOS_REGION_06_FIELDS,
        get_text_values_from_region(collected, "page_03_region_06"),
    )

    assign_values_by_order(
        record,
        EQUIPOS_REGION_07_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_07"),
    )

    return record


def build_requerimientos_total(
    collected: dict[str, list[str]],
    document_id: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "document_id": document_id,
    }

    all_fields = (
        REQ_TOTAL_REGION_08_FIELDS
        + REQ_TOTAL_REGION_09_FIELDS
        + REQ_TOTAL_REGION_10_FIELDS
        + REQ_TOTAL_REGION_11_FIELDS
        + REQ_TOTAL_REGION_12_FIELDS
    )

    for field in all_fields:
        record[field] = None

    assign_values_by_order(
        record,
        REQ_TOTAL_REGION_08_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_08"),
    )

    assign_values_by_order(
        record,
        REQ_TOTAL_REGION_09_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_09"),
    )

    assign_values_by_order(
        record,
        REQ_TOTAL_REGION_10_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_10"),
    )

    assign_values_by_order(
        record,
        REQ_TOTAL_REGION_11_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_11"),
    )

    assign_values_by_order(
        record,
        REQ_TOTAL_REGION_12_FIELDS,
        get_numeric_values_from_region(collected, "page_03_region_12"),
    )

    return record

def merge_wrapped_rows_by_vertical_gap(
    row_records: list[dict[str, Any]],
    max_gap: float = 10.0,
) -> list[dict[str, Any]]:
    """
    Une filas visuales que pertenecen al mismo registro lógico.

    Ejemplo:
    fila 1: Muro Hormigon Armado 300mm ..., espesor
    fila 2: aislante de 3 [cm]

    Si el gap vertical entre fila 1 y fila 2 es pequeño, se unen.
    """
    if not row_records:
        return []

    rows = sorted(row_records, key=lambda r: (r["y0"], r["x0"]))

    merged: list[dict[str, Any]] = []
    current = dict(rows[0])

    for nxt in rows[1:]:
        gap = float(nxt["y0"]) - float(current["y1"])

        if gap <= max_gap:
            current["text"] = normalize_text(current["text"] + " " + nxt["text"])
            current["x0"] = min(float(current["x0"]), float(nxt["x0"]))
            current["y0"] = min(float(current["y0"]), float(nxt["y0"]))
            current["x1"] = max(float(current["x1"]), float(nxt["x1"]))
            current["y1"] = max(float(current["y1"]), float(nxt["y1"]))
            current["n_words"] = int(current["n_words"]) + int(nxt["n_words"])
        else:
            merged.append(current)
            current = dict(nxt)

    merged.append(current)

    return merged

def build_materialidades(
    collected_row_records: dict[str, list[dict[str, Any]]],
    document_id: str,
    region_id: str = "page_02_region_02",
    max_gap: float = 8.0,
) -> list[dict[str, Any]]:
    rows = collected_row_records.get(region_id, [])

    merged_rows = merge_wrapped_rows_by_vertical_gap(
        row_records=rows,
        max_gap=max_gap,
    )

    records: list[dict[str, Any]] = []

    for idx, elemento in enumerate(ELEMENTOS_MATERIALIDADES):
        descripcion = None

        if idx < len(merged_rows):
            descripcion = merged_rows[idx]["text"]

        records.append(
            {
                "document_id": document_id,
                "elemento": elemento,
                "descripcion": descripcion,
            }
        )

    return records

PERCENT_RE = re.compile(
    r"""
    [-+]?
    (?:
        \d+(?:,\d+)? |
        ,\d+
    )
    \s*%
    """,
    re.VERBOSE,
)


def extract_percent_value(text: str) -> float | None:
    """
    Extrae porcentajes desde textos como:
    - '% máximo = 25%'
    - '% máximo = ,6%'

    Retorna el valor en puntos porcentuales:
    - 25% -> 25.0
    - ,6% -> 0.6
    """
    text = normalize_text(text)

    match = PERCENT_RE.search(text)
    if not match:
        return None

    token = match.group(0)
    token = token.replace("%", "").replace(" ", "")

    if token.startswith(","):
        token = "0" + token

    return parse_chilean_number(token)

def extract_u_normativa_value(text: str) -> float | None:
    """
    Extrae el valor numérico de la columna EXIGENCIA U.

    Casos:
    - '1,9 [W/m2K]'       -> 1.9
    - '% máximo = 25%'    -> 25.0
    - '% máximo = ,6%'    -> 0.6
    - 'sin exigencia'     -> None
    """
    text_n = normalize_text(text).lower()

    if "%" in text_n:
        return extract_percent_value(text)

    if "w/m2" in text_n or "w/m²" in text_n:
        return extract_first_number(text)

    return None


def build_exigencia_u_normativa(
    collected_row_records: dict[str, list[dict[str, Any]]],
    document_id: str,
    region_id: str = "page_02_region_03",
    max_gap: float = 8.0,
) -> list[dict[str, Any]]:
    rows = collected_row_records.get(region_id, [])

    merged_rows = merge_wrapped_rows_by_vertical_gap(
        row_records=rows,
        max_gap=max_gap,
    )

    records: list[dict[str, Any]] = []

    for idx, elemento in enumerate(ELEMENTOS_MATERIALIDADES):
        exigencia_texto = None
        exigencia_u = None

        if idx < len(merged_rows):
            exigencia_texto = normalize_text(merged_rows[idx]["text"])
            exigencia_u = extract_u_normativa_value(exigencia_texto)

        records.append(
            {
                "document_id": document_id,
                "elemento": elemento,
                "exigencia_texto": exigencia_texto,
                "exigencia_u": exigencia_u,
            }
        )

    return records

def build_elementos_table_long(
    collected: dict[str, list[str]],
    document_id: str,
    area_region_id: str,
    u_region_id: str,
    orientaciones: list[str],
) -> list[dict[str, Any]]:
    """
    Construye tabla larga para elementos opacos o traslúcidos.

    Salida:
    [
        {"document_id": ..., "orientacion": "horiz", "area": ..., "u": ...},
        {"document_id": ..., "orientacion": "n", "area": ..., "u": ...},
        ...
    ]
    """

    area_values = get_numeric_values_from_region(
        collected=collected,
        region_id=area_region_id,
    )

    u_values = get_numeric_values_from_region(
        collected=collected,
        region_id=u_region_id,
    )

    records: list[dict[str, Any]] = []

    for idx, orientacion in enumerate(orientaciones):
        area = area_values[idx] if idx < len(area_values) else None
        u = u_values[idx] if idx < len(u_values) else None

        records.append(
            {
                "document_id": document_id,
                "orientacion": orientacion,
                "area": area,
                "u": u,
            }
        )

    return records

def parse_numeric_tokens_from_row(row: str) -> list[float | None]:
    """
    Separa una fila tipo:
    '0,7 -0,3 0,3 0 0'

    y devuelve:
    [0.7, -0.3, 0.3, 0.0, 0.0]
    """
    values: list[float | None] = []

    for token in normalize_text(row).split():
        value = parse_chilean_number(token)
        if value is not None:
            values.append(value)

    return values


def build_puentes_termicos(
    collected: dict[str, list[str]],
    document_id: str,
    region_id: str = "page_03_region_15",
) -> list[dict[str, Any]]:
    rows = get_region_rows(collected, region_id)

    records: list[dict[str, Any]] = []

    for orientacion, row in zip(PUENTES_TERMICOS_ORIENTACIONES, rows):
        values = parse_numeric_tokens_from_row(row)

        # Asegura largo 5: p01, p02, p03, p04, p05
        values = (values + [None] * len(PUENTES_TERMICOS_FIELDS))[:len(PUENTES_TERMICOS_FIELDS)]

        record: dict[str, Any] = {
            "document_id": document_id,
            "orientacion": orientacion,
        }

        for field, value in zip(PUENTES_TERMICOS_FIELDS, values):
            record[field] = value

        records.append(record)

    return records


# =============================================================================
# SQLite
# =============================================================================

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS descripcion_general (
            document_id TEXT PRIMARY KEY,
            source_pdf TEXT,
            estado_cev TEXT,
            region TEXT,
            comuna TEXT,
            direccion TEXT,
            rol_vivienda TEXT,
            tipo_vivienda TEXT,
            superficie_util REAL,
            ahorro REAL,
            etiqueta TEXT,
            demanda_calef REAL,
            demanda_refri REAL,
            fecha_emision TEXT,
            zona_termica TEXT,
            solicitante TEXT,
            evaluador TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS requerimientos (
            document_id TEXT PRIMARY KEY,
            acs_kwhm2 REAL,
            ilum_kwhm2 REAL,
            calef_kwhm2 REAL,
            ernc_kwhm2 REAL,
            consumo_kwhm2 REAL,
            emisiones_co2 REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS descripcion_equipos (
            document_id TEXT PRIMARY KEY,
            calef_proyec_descripcion TEXT,
            ilum_proyec_descripcion TEXT,
            acs_proyec_descripcion TEXT,
            ernc_proyec_descripcion TEXT,
            calef_proyec_kwh REAL,
            ilum_proyec_kwh REAL,
            acs_proyec_kwh REAL,
            ernc_proyec_kwh REAL,
            calef_ref_descripcion TEXT,
            ilum_ref_descripcion TEXT,
            acs_ref_descripcion TEXT,
            ernc_ref_descripcion TEXT,
            calef_ref_kwh REAL,
            ilum_ref_kwh REAL,
            acs_ref_kwh REAL,
            ernc_ref_kwh REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS requerimientos_total (
            document_id TEXT PRIMARY KEY,
            consumo_primario_calef REAL,
            consumo_primario_acs REAL,
            consumo_primario_ilum REAL,
            consumo_primario_vent REAL,
            generacion_pv REAL,
            pv_consumos_basicos REAL,
            dif_pv_consumo REAL,
            st_calef REAL,
            st_acs REAL,
            consumo_primario REAL,
            aporte_pv REAL,
            consumos_energia_externa REAL,
            consumo_primario_2 REAL,
            energia_ref REAL,
            coef_energetico_c REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS elementos_opacos (
            document_id TEXT,
            orientacion TEXT,
            area REAL,
            u REAL,
            PRIMARY KEY (document_id, orientacion)
        )
    """) 

    conn.execute("""
        CREATE TABLE IF NOT EXISTS elementos_traslucidos (
            document_id TEXT,
            orientacion TEXT,
            area REAL,
            u REAL,
            PRIMARY KEY (document_id, orientacion)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS puentes_termicos (
            document_id TEXT,
            orientacion TEXT,
            p01 REAL,
            p02 REAL,
            p03 REAL,
            p04 REAL,
            p05 REAL,
            PRIMARY KEY (document_id, orientacion)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS materialidades (
            document_id TEXT,
            elemento TEXT,
            descripcion TEXT,
            PRIMARY KEY (document_id, elemento)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS exigencia_u_normativa (
            document_id TEXT,
            elemento TEXT,
            exigencia_texto TEXT,
            exigencia_u REAL,
            PRIMARY KEY (document_id, elemento)
        )
    """)


def upsert_record(conn: sqlite3.Connection, table_name: str, record: dict[str, Any]) -> None:
    cols = list(record.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_sql = ", ".join(cols)

    sql = f"""
        INSERT OR REPLACE INTO {table_name} ({col_sql})
        VALUES ({placeholders})
    """

    conn.execute(sql, [record[col] for col in cols])


def export_sqlite_tables_to_csv(conn: sqlite3.Connection, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    table_names = [
        "descripcion_general",
        "requerimientos",
        "descripcion_equipos",
        "requerimientos_total",
        "elementos_opacos",
        "elementos_traslucidos",
    ]

    for table in table_names:
        cursor = conn.execute(f"SELECT * FROM {table}")
        rows = cursor.fetchall()
        headers = [desc[0] for desc in cursor.description]

        csv_rows = [headers] + [list(row) for row in rows]
        write_csv(out_dir / f"{table}.csv", csv_rows)


# =============================================================================
# Procesamiento de PDFs
# =============================================================================

from datetime import datetime


def parse_local_datetime_to_timestamp(value: str) -> float:
    """
    Interpreta una fecha local del servidor.
    Formatos aceptados:
    - 2026-05-11 17:00:00
    - 2026-05-11T17:00:00
    """
    value = value.strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass

    raise ValueError(
        f"Formato de fecha no válido: {value}. Usa 'YYYY-MM-DD HH:MM:SS'."
    )


def discover_pdfs(
    pdf_files: list[Path] | None,
    input_dir: Path | None,
    recursive: bool,
    folder_mtime_before: str | None = None,
    filter_pdf_mtime: bool = True,
) -> list[Path]:
    """
    Descubre PDFs.

    Si folder_mtime_before está definido:
    - toma una foto fija de las subcarpetas directas de input_dir
    - conserva solo carpetas con mtime anterior al cutoff
    - opcionalmente conserva solo PDFs con mtime anterior al cutoff

    Esto evita procesar comunas que siguen descargándose.
    """
    paths: list[Path] = []

    cutoff_ts: float | None = None
    if folder_mtime_before:
        cutoff_ts = parse_local_datetime_to_timestamp(folder_mtime_before)

    if pdf_files:
        for path in pdf_files:
            if path.exists() and path.suffix.lower() == ".pdf":
                if cutoff_ts is not None and filter_pdf_mtime:
                    try:
                        if path.stat().st_mtime >= cutoff_ts:
                            continue
                    except FileNotFoundError:
                        continue
                paths.append(path)

    if input_dir:
        input_dir = input_dir.resolve()

        if cutoff_ts is not None:
            # Foto fija de subcarpetas directas. No se consideran carpetas nuevas
            # creadas después de iniciar el script.
            commune_dirs: list[Path] = []

            for child in sorted(input_dir.iterdir()):
                if not child.is_dir():
                    continue

                try:
                    folder_mtime = child.stat().st_mtime
                except FileNotFoundError:
                    continue

                if folder_mtime < cutoff_ts:
                    commune_dirs.append(child)

            print(f"[INFO] Carpetas seleccionadas por cutoff: {len(commune_dirs)}")

            for folder in commune_dirs:
                iterator = folder.rglob("*.pdf") if recursive else folder.glob("*.pdf")

                for pdf in iterator:
                    try:
                        if not pdf.is_file():
                            continue

                        if filter_pdf_mtime and pdf.stat().st_mtime >= cutoff_ts:
                            continue

                        paths.append(pdf)
                    except FileNotFoundError:
                        continue

        else:
            pattern = "**/*.pdf" if recursive else "*.pdf"
            paths.extend(sorted(input_dir.glob(pattern)))

    unique: list[Path] = []
    seen: set[Path] = set()

    for path in paths:
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            continue

        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)

    return sorted(unique)


def process_one_pdf(
    pdf_path: Path,
    regions: list[dict[str, Any]],
    conn: sqlite3.Connection,
    output_dir: Path,
    min_occluder_area: float,
    overlap_threshold: float,
    region_margin: float,
    y_tol: float,
    dedup_coord_tol: float,
) -> None:
    # print(f"\n[PDF] Procesando: {pdf_path.name}")

    doc = fitz.open(pdf_path)

    if len(doc) != 7:
        print(f"[SKIP] PDF con {len(doc)} páginas, se esperaba 7: {pdf_path}")
        doc.close()
        return

    collected, collected_row_records = extract_region_rows(
        doc=doc,
        regions=regions,
        rows_dir=None,
        min_occluder_area=min_occluder_area,
        overlap_threshold=overlap_threshold,
        region_margin=region_margin,
        y_tol=y_tol,
        dedup_coord_tol=dedup_coord_tol,
    )

    estado_cev = infer_estado_cev_from_filename(pdf_path.name)

    desc_general = build_descripcion_general(
        collected=collected,
        source_pdf=pdf_path.name,
        estado_cev=estado_cev,
    )

    document_id = desc_general["document_id"]

    requerimientos = build_requerimientos(
        collected=collected,
        document_id=document_id,
    )

    descripcion_equipos = build_descripcion_equipos(
        collected=collected,
        document_id=document_id,
    )

    requerimientos_total = build_requerimientos_total(
        collected=collected,
        document_id=document_id,
    )

    materialidades_records = build_materialidades(
        collected_row_records=collected_row_records,
        document_id=document_id,
        region_id="page_02_region_02",
        max_gap=4.0,
    )

    exigencia_u_normativa_records = build_exigencia_u_normativa(
        collected_row_records=collected_row_records,
        document_id=document_id,
        region_id="page_02_region_03",
        max_gap=4.0,
    )

    elementos_opacos_records = build_elementos_table_long(
        collected=collected,
        document_id=document_id,
        area_region_id="page_03_region_13",
        u_region_id="page_03_region_16",
        orientaciones=ELEMENTOS_OPACOS_ORIENTACIONES,
    )

    elementos_traslucidos_records = build_elementos_table_long(
        collected=collected,
        document_id=document_id,
        area_region_id="page_03_region_14",
        u_region_id="page_03_region_17",
        orientaciones=ELEMENTOS_TRASLUCIDOS_ORIENTACIONES,
    )

    puentes_termicos_records = build_puentes_termicos(
        collected=collected,
        document_id=document_id,
        region_id="page_03_region_15",
    )

    upsert_record(conn, "descripcion_general", desc_general)
    upsert_record(conn, "requerimientos", requerimientos)
    upsert_record(conn, "descripcion_equipos", descripcion_equipos)
    upsert_record(conn, "requerimientos_total", requerimientos_total)
    for record in elementos_opacos_records:
        upsert_record(conn, "elementos_opacos", record)

    for record in elementos_traslucidos_records:
        upsert_record(conn, "elementos_traslucidos", record)

    for record in puentes_termicos_records:
        upsert_record(conn, "puentes_termicos", record)

    for record in materialidades_records:
        upsert_record(conn, "materialidades", record)

    for record in exigencia_u_normativa_records:
        upsert_record(conn, "exigencia_u_normativa", record)

    doc.close()

    # print(f"[OK] Registros compilados en SQLite para document_id={document_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrae texto visible desde regiones PDF CEV y compila una base SQLite."
    )

    parser.add_argument(
        "--folder-mtime-before",
        type=str,
        default=None,
        help="Procesa solo subcarpetas de --input-dir con mtime anterior a esta fecha local. Ej: '2026-05-11 17:00:00'.",
    )

    parser.add_argument(
        "--no-filter-pdf-mtime",
        action="store_true",
        help="No filtra PDFs por mtime. Por defecto también exige que el PDF tenga mtime anterior al cutoff.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Cantidad de PDFs entre commits SQLite.",
    )

    parser.add_argument(
        "--paths-config",
        type=Path,
        default=default_paths_config(Path(__file__).resolve()),
        help="Archivo YAML con rutas del proyecto.",
    )

    parser.add_argument(
        "--pdf",
        type=Path,
        nargs="*",
        default=None,
        help="Uno o más PDFs a procesar.",
    )

    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="Carpeta con PDFs a procesar.",
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Buscar PDFs recursivamente dentro de --input-dir.",
    )

    parser.add_argument(
        "--regions",
        type=Path,
        default=None,
        help="JSON de regiones generado con el clicker multipagina.",
    )

    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Carpeta de salida.",
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Ruta de la base SQLite. Si no se define, se guarda dentro de --out-dir.",
    )

    parser.add_argument(
        "--min-occluder-area",
        type=float,
        default=20.0,
    )

    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.55,
    )

    parser.add_argument(
        "--region-margin",
        type=float,
        default=1.0,
    )

    parser.add_argument(
        "--y-tol",
        type=float,
        default=3.5,
    )

    parser.add_argument(
        "--dedup-coord-tol",
        type=float,
        default=0.7,
    )

    args = parser.parse_args()

    paths = load_paths(args.paths_config)
    project_root = paths["project_root"]
    args.input_dir = args.input_dir or path_value(paths, "cev_pdf_dir")
    args.regions = args.regions or path_value(paths, "pdf_regions_path")
    args.out_dir = args.out_dir or path_value(paths, "pdf_extract_out_dir", project_root / "data/interim/output_cev_compiled")
    args.db = args.db or path_value(paths, "cev_db_path")

    if args.regions is None:
        raise SystemExit("Falta --regions o pdf_regions_path en config/paths.yaml.")
    if args.out_dir is None:
        raise SystemExit("Falta --out-dir o pdf_extract_out_dir en config/paths.yaml.")

    pdfs = discover_pdfs(
        pdf_files=args.pdf,
        input_dir=args.input_dir,
        recursive=args.recursive,
        folder_mtime_before=args.folder_mtime_before,
        filter_pdf_mtime=not args.no_filter_pdf_mtime,
    )

    if not pdfs:
        raise SystemExit("No se encontraron PDFs para procesar. Usa --pdf o --input-dir.")

    with args.regions.open("r", encoding="utf-8") as f:
        regions_config = json.load(f)

    regions = regions_config["regions"]

    args.out_dir.mkdir(parents=True, exist_ok=True)

    db_path = args.db or (args.out_dir / "cev_compiled.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")

    init_db(conn)

    print(f"[INFO] PDFs seleccionados para procesar: {len(pdfs)}")

    processed_ok = 0
    processed_error = 0

    for i, pdf_path in enumerate(pdfs, start=1):
        savepoint_name = f"pdf_{i}"

        try:
            conn.execute(f"SAVEPOINT {savepoint_name}")

            process_one_pdf(
                pdf_path=pdf_path,
                regions=regions,
                conn=conn,
                output_dir=args.out_dir,
                min_occluder_area=args.min_occluder_area,
                overlap_threshold=args.overlap_threshold,
                region_margin=args.region_margin,
                y_tol=args.y_tol,
                dedup_coord_tol=args.dedup_coord_tol,
            )

            conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            processed_ok += 1

        except Exception as exc:
            processed_error += 1

            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except Exception:
                pass

            print(f"[ERROR] Falló PDF: {pdf_path}")
            print(f"[ERROR] {type(exc).__name__}: {exc}")

        if i % args.batch_size == 0:
            conn.commit()
            print(
                f"[INFO] Commit lote {i}/{len(pdfs)} | "
                f"ok={processed_ok} | error={processed_error}"
            )

    # Commit final para guardar el último bloque, aunque no alcance batch_size.
    conn.commit()
    print(
        f"\n[OK] Procesamiento terminado | "
        f"ok={processed_ok} | error={processed_error}"
    )

    conn.close()

    print(f"[OK] Base SQLite guardada en: {db_path}")


if __name__ == "__main__":
    main()