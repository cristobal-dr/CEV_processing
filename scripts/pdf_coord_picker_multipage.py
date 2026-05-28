from __future__ import annotations

import argparse
import json
import sys
import re
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

import matplotlib.pyplot as plt
import matplotlib.patches as patches
from PIL import Image


def render_page_to_image(page: fitz.Page, dpi: int = 144) -> Image.Image:
    """
    Renderiza una página PDF a imagen.
    Las coordenadas mostradas por matplotlib se mantienen en puntos PDF.
    """
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img


def safe_region_id(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_\-.]+", "_", text)
    return text.strip("._") or "region"


class PdfCoordinatePickerMultiPage:
    def __init__(
        self,
        pdf_path: Path,
        output_path: Path,
        start_page: int = 1,
        dpi: int = 144,
        load_existing: bool = True,
    ):
        self.pdf_path = pdf_path.resolve()
        self.output_path = output_path.resolve()
        self.dpi = dpi

        self.doc = fitz.open(self.pdf_path)
        self.n_pages = len(self.doc)

        if self.n_pages == 0:
            raise ValueError("El PDF no tiene páginas.")

        self.current_page_number = max(1, min(start_page, self.n_pages))

        self.points: list[tuple[float, float]] = []
        self.regions: list[dict[str, Any]] = []

        self.fig = None
        self.ax = None
        self._closed_by_enter = False

        if load_existing and self.output_path.exists():
            self.load_existing_regions()

    # -------------------------------------------------------------------------
    # Carga / guardado
    # -------------------------------------------------------------------------

    def load_existing_regions(self) -> None:
        """
        Carga regiones existentes desde el JSON de salida.
        Esto permite continuar marcando regiones en un archivo ya iniciado.
        """
        try:
            with self.output_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            existing_regions = data.get("regions", [])

            if isinstance(existing_regions, list):
                self.regions = existing_regions
                print(f"[INFO] Regiones existentes cargadas: {len(self.regions)}")
            else:
                print("[WARN] El JSON existente no tiene una lista válida en 'regions'.")

        except Exception as exc:
            print(f"[WARN] No se pudo cargar el JSON existente: {exc}")

    def save_regions(self) -> None:
        """
        Guarda el estado completo actual. No agrega duplicados porque sobrescribe
        el archivo con la lista completa en memoria.
        """
        output = {
            "pdf_template": self.pdf_path.name,
            "pdf_path": str(self.pdf_path),
            "n_pages": self.n_pages,
            "coordinate_system": {
                "origin": "top-left",
                "unit": "PDF points",
                "description": "Coordinates are directly usable with fitz.Rect(x0, y0, x1, y1).",
            },
            "regions": self.regions,
        }

        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        with self.output_path.open("w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"[OK] JSON guardado en: {self.output_path}")
        print(f"[OK] Total regiones guardadas: {len(self.regions)}")

    # -------------------------------------------------------------------------
    # Navegación
    # -------------------------------------------------------------------------

    def get_page(self) -> fitz.Page:
        return self.doc[self.current_page_number - 1]

    def next_page(self) -> None:
        if self.current_page_number < self.n_pages:
            self.current_page_number += 1
            self.points = []
            self.redraw()
        else:
            print("[INFO] Ya estás en la última página.")

    def previous_page(self) -> None:
        if self.current_page_number > 1:
            self.current_page_number -= 1
            self.points = []
            self.redraw()
        else:
            print("[INFO] Ya estás en la primera página.")

    # -------------------------------------------------------------------------
    # Regiones
    # -------------------------------------------------------------------------

    def current_page_regions(self) -> list[dict[str, Any]]:
        return [
            region
            for region in self.regions
            if int(region.get("page", -1)) == self.current_page_number
        ]

    def next_region_id(self, page_number: int) -> str:
        """
        Genera un ID incremental por página:
        page_03_region_01, page_03_region_02, etc.
        """
        page_regions = [
            region
            for region in self.regions
            if int(region.get("page", -1)) == page_number
        ]

        max_idx = 0

        pattern = re.compile(rf"page_{page_number:02d}_region_(\d+)$")

        for region in page_regions:
            region_id = str(region.get("id", ""))
            match = pattern.search(region_id)

            if match:
                max_idx = max(max_idx, int(match.group(1)))

        return f"page_{page_number:02d}_region_{max_idx + 1:02d}"

    def add_region_from_two_points(self) -> None:
        (x_a, y_a), (x_b, y_b) = self.points

        x0 = min(x_a, x_b)
        y0 = min(y_a, y_b)
        x1 = max(x_a, x_b)
        y1 = max(y_a, y_b)

        region_id = self.next_region_id(self.current_page_number)

        region = {
            "id": region_id,
            "page": self.current_page_number,
            "x0": round(x0, 2),
            "y0": round(y0, 2),
            "x1": round(x1, 2),
            "y1": round(y1, 2),
        }

        self.regions.append(region)

        print("[OK] Región agregada:")
        print(json.dumps(region, ensure_ascii=False, indent=2))

        self.points = []
        self.redraw()

    def undo_last_region_current_page(self) -> None:
        """
        Elimina la última región creada en la página actual.
        """
        for idx in range(len(self.regions) - 1, -1, -1):
            if int(self.regions[idx].get("page", -1)) == self.current_page_number:
                removed = self.regions.pop(idx)
                print(f"[OK] Región eliminada de página actual: {removed.get('id')}")
                self.redraw()
                return

        print("[INFO] No hay regiones en la página actual para eliminar.")

    def undo_last_region_global(self) -> None:
        """
        Elimina la última región creada, independiente de la página.
        """
        if not self.regions:
            print("[INFO] No hay regiones para eliminar.")
            return

        removed = self.regions.pop()
        print(f"[OK] Última región global eliminada: {removed.get('id')}")
        self.redraw()

    # -------------------------------------------------------------------------
    # Interfaz gráfica
    # -------------------------------------------------------------------------

    def start(self) -> None:
        self.fig, self.ax = plt.subplots(figsize=(10, 13))

        self.fig.canvas.mpl_connect("button_press_event", self.on_click)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)
        self.fig.canvas.mpl_connect("close_event", self.on_close)

        self.redraw()
        plt.show()

    def redraw(self) -> None:
        if self.ax is None or self.fig is None:
            return

        page = self.get_page()
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)

        img = render_page_to_image(page, dpi=self.dpi)

        self.ax.clear()

        self.ax.imshow(
            img,
            extent=(0, page_width, page_height, 0),
        )

        current_regions = self.current_page_regions()

        title = (
            f"PDF coordinate picker | Página {self.current_page_number}/{self.n_pages}\n"
            f"Regiones en esta página: {len(current_regions)} | Regiones totales: {len(self.regions)}\n"
            "Clicks: esquina sup. izq. + esquina inf. der. | "
            "n/siguiente | p/anterior | s/guardar | ENTER/guardar y cerrar | u/deshacer página"
        )

        self.ax.set_title(title, fontsize=9)
        self.ax.set_xlabel("x [PDF points]")
        self.ax.set_ylabel("y [PDF points]")
        self.ax.grid(True, linewidth=0.4)

        # Dibujar regiones de la página actual.
        for region in current_regions:
            x0 = float(region["x0"])
            y0 = float(region["y0"])
            x1 = float(region["x1"])
            y1 = float(region["y1"])

            rect_patch = patches.Rectangle(
                (x0, y0),
                x1 - x0,
                y1 - y0,
                fill=False,
                linewidth=1.5,
            )

            self.ax.add_patch(rect_patch)

            self.ax.text(
                x0,
                max(0, y0 - 4),
                str(region["id"]),
                fontsize=8,
                verticalalignment="bottom",
                bbox={"facecolor": "white", "alpha": 0.6, "edgecolor": "none"},
            )

        # Dibujar clicks pendientes.
        for x, y in self.points:
            self.ax.plot(x, y, marker="x", markersize=8)

        self.fig.canvas.draw_idle()

    def on_click(self, event) -> None:
        if event.inaxes != self.ax:
            return

        if event.xdata is None or event.ydata is None:
            return

        x = float(event.xdata)
        y = float(event.ydata)

        page = self.get_page()

        # Evitar clicks fuera del área de página.
        if not (0 <= x <= page.rect.width and 0 <= y <= page.rect.height):
            return

        self.points.append((x, y))

        print(
            f"[CLICK] página={self.current_page_number}, "
            f"x={x:.2f}, y={y:.2f}"
        )

        if len(self.points) == 2:
            self.add_region_from_two_points()
        else:
            self.redraw()

    def on_key(self, event) -> None:
        key = event.key

        if key in {"n", "right", "pagedown"}:
            self.next_page()

        elif key in {"p", "left", "pageup"}:
            self.previous_page()

        elif key == "s":
            self.save_regions()

        elif key == "enter":
            self._closed_by_enter = True
            self.save_regions()
            plt.close(self.fig)

        elif key == "u":
            self.undo_last_region_current_page()

        elif key == "backspace":
            self.undo_last_region_global()

        elif key == "r":
            self.points = []
            print("[OK] Clicks actuales reseteados.")
            self.redraw()

        elif key == "escape":
            print("[INFO] Saliendo. Se guardará automáticamente al cerrar.")
            plt.close(self.fig)

    def on_close(self, event) -> None:
        """
        Guarda automáticamente al cerrar la ventana.
        """
        if self.regions:
            self.save_regions()
        else:
            print("[INFO] Ventana cerrada. No había regiones para guardar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clicker multipágina para obtener coordenadas PDF de regiones."
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
        required=True,
        help="Ruta al PDF.",
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Ruta del JSON de salida. Por defecto usa pdf_regions_path de paths.yaml.",
    )

    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Página inicial, indexada desde 1.",
    )

    parser.add_argument(
        "--dpi",
        type=int,
        default=144,
        help="DPI de renderizado. No cambia las coordenadas PDF.",
    )

    parser.add_argument(
        "--no-load-existing",
        action="store_true",
        help="No cargar regiones existentes aunque el JSON ya exista.",
    )

    args = parser.parse_args()

    paths = load_paths(args.paths_config)
    args.out = args.out or path_value(paths, "pdf_regions_path", Path("table_regions.json"))

    picker = PdfCoordinatePickerMultiPage(
        pdf_path=args.pdf,
        output_path=args.out,
        start_page=args.start_page,
        dpi=args.dpi,
        load_existing=not args.no_load_existing,
    )

    picker.start()


if __name__ == "__main__":
    main()