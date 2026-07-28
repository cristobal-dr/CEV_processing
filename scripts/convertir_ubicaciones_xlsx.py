#!/usr/bin/env python3
"""Convierte la hoja consolidada Hoja2 del XLSX CEV a CSV UTF-8."""

import argparse
import csv
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    REPO_ROOT
    / "data/raw/2026-07-03_DITEC_HyEE_Evaluaciones CEV con ubicación_V3.xlsx"
)
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "data/processed/2026-07-03_DITEC_HyEE_Evaluaciones_CEV_ubicaciones.csv"
)
SHEET_XML = "xl/worksheets/sheet3.xml"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

FIELD_MAP = {
    "NombreProyecto": "nombre_proyecto",
    "FechaEmisionEtiqueta": "fecha_emision",
    "TipoCalificación": "estado_cev",
    "Región": "region_codigo",
    "Comuna": "comuna",
    "ZonaTérmica": "zona_termica",
    "Calificación": "etiqueta",
    "RolVivienda": "rol_vivienda",
    "RolProyecto": "rol_proyecto",
    "Longitud": "longitud",
    "Latitud": "latitud",
}


def column_number(cell_reference):
    letters = re.match(r"[A-Z]+", cell_reference).group()
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number - 1


def cell_value(cell, shared_strings):
    value = cell.find(NS + "v")
    if value is None:
        inline = cell.find(NS + "is")
        if inline is None:
            return ""
        return "".join(node.text or "" for node in inline.iter(NS + "t"))
    if cell.get("t") == "s":
        return shared_strings[int(value.text)]
    return value.text or ""


def excel_date(value):
    if not value:
        return ""
    try:
        serial = float(value)
    except ValueError:
        return value.strip()
    return (datetime(1899, 12, 30) + timedelta(days=serial)).date().isoformat()


def normalized_status(value):
    plain = "".join(
        char for char in unicodedata.normalize("NFKD", value.lower())
        if not unicodedata.combining(char)
    )
    if "pre" in plain and "calificacion" in plain:
        return "precalificada"
    if "calificacion" in plain:
        return "calificada"
    return value.strip()


def normalized_zone(value):
    match = re.search(r"([A-I])\s*$", value.strip(), re.IGNORECASE)
    return match.group(1).upper() if match else value.strip()


def rows(archive, shared_strings):
    for _, row in ET.iterparse(archive.open(SHEET_XML), events=("end",)):
        if row.tag != NS + "row":
            continue
        values = {}
        for cell in row.findall(NS + "c"):
            values[column_number(cell.get("r"))] = cell_value(cell, shared_strings)
        width = max(values, default=-1) + 1
        yield int(row.get("r")), [values.get(index, "") for index in range(width)]
        row.clear()


def arguments():
    parser = argparse.ArgumentParser(
        description="Convierte la hoja consolidada Hoja2 del XLSX CEV a CSV UTF-8."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main():
    args = arguments()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.input) as archive:
        shared_strings = []
        for _, item in ET.iterparse(
            archive.open("xl/sharedStrings.xml"), events=("end",)
        ):
            if item.tag == NS + "si":
                shared_strings.append(
                    "".join(node.text or "" for node in item.iter(NS + "t"))
                )
                item.clear()

        iterator = rows(archive, shared_strings)
        _, source_headers = next(iterator)
        headers = [FIELD_MAP.get(header, header) for header in source_headers]
        if headers != list(FIELD_MAP.values()):
            raise ValueError(f"Encabezados inesperados: {source_headers!r}")

        with args.output.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.writer(destination, lineterminator="\n")
            writer.writerow(["fila_origen", *headers])
            for source_row, values in iterator:
                values += [""] * (len(headers) - len(values))
                record = dict(zip(headers, values))
                if not any(value.strip() for value in values):
                    continue
                record["fecha_emision"] = excel_date(record["fecha_emision"])
                record["estado_cev"] = normalized_status(record["estado_cev"])
                record["zona_termica"] = normalized_zone(record["zona_termica"])
                writer.writerow([source_row, *(record[name].strip() for name in headers)])


if __name__ == "__main__":
    main()
