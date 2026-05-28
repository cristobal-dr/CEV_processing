from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cev_analisis.paths import default_paths_config, load_paths, path_value


def parse_local_datetime_to_timestamp(value: str) -> float:
    value = value.strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass

    raise ValueError("Formato inválido. Usa 'YYYY-MM-DD HH:MM:SS'.")


def discover_pdfs(
    input_dir: Path,
    folder_mtime_before: str | None = None,
    folder_mtime_after: str | None = None,
) -> list[Path]:
    pdfs: list[Path] = []

    before_ts = (
        parse_local_datetime_to_timestamp(folder_mtime_before)
        if folder_mtime_before
        else None
    )

    after_ts = (
        parse_local_datetime_to_timestamp(folder_mtime_after)
        if folder_mtime_after
        else None
    )

    selected_dirs: list[Path] = []

    for child in sorted(input_dir.iterdir()):
        if not child.is_dir():
            continue

        try:
            folder_mtime = child.stat().st_mtime
        except FileNotFoundError:
            continue

        # Igual que folder-mtime-before, pero en sentido contrario.
        # before: mtime < cutoff
        # after:  mtime > cutoff
        if before_ts is not None and folder_mtime >= before_ts:
            continue

        if after_ts is not None and folder_mtime <= after_ts:
            continue

        selected_dirs.append(child)

    print(f"[INFO] Carpetas seleccionadas: {len(selected_dirs)}", flush=True)

    for folder in selected_dirs:
        for pdf in folder.rglob("*.pdf"):
            try:
                if pdf.is_file():
                    pdfs.append(pdf.resolve())
            except FileNotFoundError:
                continue

    return sorted(set(pdfs))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Procesa PDFs CEV uno a uno y registra fallos sin detener todo el lote."
    )
    parser.add_argument(
        "--paths-config",
        type=Path,
        default=default_paths_config(Path(__file__).resolve()),
        help="Archivo YAML con rutas del proyecto.",
    )
    parser.add_argument("--worker-script", type=Path, default=None)
    parser.add_argument("--input-dir", type=Path, default=None)
    parser.add_argument("--regions", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--folder-mtime-after",type=str,default=None,
    help="Procesa solo subcarpetas de --input-dir con mtime posterior a esta fecha local. Ej: '2026-05-17 17:26:00'.")
    parser.add_argument("--folder-mtime-before",type=str,default=None,
    help="Procesa solo subcarpetas de --input-dir con mtime anterior a esta fecha local.")
    parser.add_argument("--failed-log", type=Path, default=None)
    parser.add_argument("--progress-every", type=int, default=100)
    args = parser.parse_args()

    paths = load_paths(args.paths_config)
    project_root = paths["project_root"]
    args.worker_script = args.worker_script or project_root / "scripts" / "extract_visible_rows_and_build_db.py"
    args.input_dir = args.input_dir or path_value(paths, "cev_pdf_dir")
    args.regions = args.regions or path_value(paths, "pdf_regions_path")
    args.out_dir = args.out_dir or path_value(paths, "pdf_extract_out_dir", project_root / "data/interim/output_cev_compiled")
    args.db = args.db or path_value(paths, "cev_db_path")
    args.failed_log = args.failed_log or path_value(paths, "failed_processing_log_path", project_root / "logs/cev_processing_failed.log")

    required_paths = {
        "worker-script": args.worker_script,
        "input-dir": args.input_dir,
        "regions": args.regions,
        "out-dir": args.out_dir,
        "db": args.db,
        "failed-log": args.failed_log,
    }
    missing = [name for name, value in required_paths.items() if value is None]
    if missing:
        raise SystemExit(f"Faltan rutas requeridas: {', '.join(missing)}")

    pdfs = discover_pdfs(
        input_dir=args.input_dir,
        folder_mtime_before=args.folder_mtime_before,
        folder_mtime_after=args.folder_mtime_after,
    )

    print(f"[INFO] PDFs seleccionados: {len(pdfs)}", flush=True)

    args.failed_log.parent.mkdir(parents=True, exist_ok=True)

    ok = 0
    failed = 0

    for i, pdf in enumerate(pdfs, start=1):
        cmd = [
            "python",
            str(args.worker_script),
            "--pdf",
            str(pdf),
            "--regions",
            str(args.regions),
            "--out-dir",
            str(args.out_dir),
            "--db",
            str(args.db),
            "--batch-size",
            "1",
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.returncode == 0:
            ok += 1
        else:
            failed += 1
            with args.failed_log.open("a", encoding="utf-8") as f:
                f.write(f"\n[FAILED] {pdf}\n")
                f.write(result.stderr[-4000:])
                f.write("\n")

        if i % args.progress_every == 0:
            print(
                f"[INFO] avance={i}/{len(pdfs)} | ok={ok} | failed={failed}",
                flush=True,
            )

    print(
        f"[OK] terminado | total={len(pdfs)} | ok={ok} | failed={failed}",
        flush=True,
    )


if __name__ == "__main__":
    main()