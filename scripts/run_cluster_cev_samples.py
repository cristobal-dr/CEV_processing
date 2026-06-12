from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cluster_cev_arquetipos as cev_cluster


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Ejecuta varias muestras del clustering CEV principal y guarda solo metricas."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=10_000)
    parser.add_argument("--min-cluster-size", type=int, default=50)
    parser.add_argument("--min-samples", type=int, default=15)
    parser.add_argument("--id-col", type=str, default="document_id")
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=[101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115],
        help="Semillas de muestreo. Por defecto ejecuta 15 muestras.",
    )
    parser.add_argument("--use-log-superficie", action="store_true")
    return parser.parse_args()


def run_one_sample(
    raw_df: pd.DataFrame,
    seed: int,
    max_rows: int,
    min_cluster_size: int,
    min_samples: int,
    use_log_superficie: bool,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Run one lightweight clustering sample with kWh system variables."""
    sampled = raw_df.sample(n=max_rows, random_state=seed).reset_index(drop=True)
    cleaned_df, clustering_df, _, numeric_cols, categorical_cols = (
        cev_cluster.prepare_clustering_dataframe(
            sampled,
            use_log_superficie=use_log_superficie,
            use_kwh_systems=True,
            use_log_kwh_systems=True,
        )
    )
    distance_matrix = cev_cluster.compute_gower_distance(clustering_df)
    clusterer = cev_cluster.run_hdbscan(distance_matrix, min_cluster_size, min_samples)
    labels = clusterer.labels_.astype(int)

    result_df = cleaned_df.copy()
    result_df["cluster_hdbscan"] = labels
    result_df["cluster_probability"] = getattr(
        clusterer, "probabilities_", pd.Series([pd.NA] * len(labels))
    )
    result_df["cluster_is_noise"] = result_df["cluster_hdbscan"] == -1

    metrics = cev_cluster.compute_metrics(labels, distance_matrix)
    metrics["random_state"] = seed
    metrics["n_input"] = int(len(raw_df))
    metrics["n_used_after_sampling_and_dedup"] = int(len(result_df))

    summary, _, _ = cev_cluster.profile_clusters(result_df, numeric_cols, categorical_cols)
    silhouette = cev_cluster.compute_silhouette_by_cluster(labels, distance_matrix)
    summary.insert(0, "random_state", seed)
    silhouette.insert(0, "random_state", seed)

    return metrics, summary, silhouette


def main() -> None:
    """Run all samples and save compact outputs."""
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    raw_df = cev_cluster.load_data(args.input)
    if len(raw_df) < args.max_rows:
        raise SystemExit(
            f"--max-rows={args.max_rows} excede las filas disponibles ({len(raw_df)})."
        )

    metrics_rows: list[dict[str, Any]] = []
    summary_rows: list[pd.DataFrame] = []
    silhouette_rows: list[pd.DataFrame] = []
    top_rows: list[pd.DataFrame] = []

    for i, seed in enumerate(args.seeds, start=1):
        print(f"[INFO] muestra {i}/{len(args.seeds)} | random_state={seed}", flush=True)
        metrics, summary, silhouette = run_one_sample(
            raw_df=raw_df,
            seed=seed,
            max_rows=args.max_rows,
            min_cluster_size=args.min_cluster_size,
            min_samples=args.min_samples,
            use_log_superficie=args.use_log_superficie,
        )
        metrics_rows.append(metrics)
        summary_rows.append(summary)
        silhouette_rows.append(silhouette)

        top = summary[summary["cluster_hdbscan"] != -1].copy()
        if not silhouette.empty:
            top = top.merge(
                silhouette[["cluster_hdbscan", "silhouette_media"]],
                on="cluster_hdbscan",
                how="left",
            )
        top = top.sort_values("n", ascending=False).head(10)
        top_rows.append(top)
        print(
            "[OK] "
            f"clusters={metrics['n_clusters_excluding_noise']} | "
            f"ruido={metrics['pct_noise']:.2f}% | "
            f"silhouette={metrics.get('silhouette_precomputed_non_noise')}",
            flush=True,
        )

    metrics_df = pd.DataFrame(metrics_rows)
    summary_df = pd.concat(summary_rows, ignore_index=True)
    silhouette_df = pd.concat(silhouette_rows, ignore_index=True)
    top_df = pd.concat(top_rows, ignore_index=True)

    metrics_df.to_csv(args.output_dir / "metricas_muestras.csv", index=False)
    summary_df.to_csv(args.output_dir / "resumen_clusters_muestras.csv", index=False)
    silhouette_df.to_csv(args.output_dir / "silhouette_clusters_muestras.csv", index=False)
    top_df.to_csv(args.output_dir / "top_clusters_muestras.csv", index=False)

    aggregate = metrics_df.describe(include="all").transpose()
    aggregate.to_csv(args.output_dir / "resumen_metricas_agregado.csv")

    config = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input": str(args.input),
        "params": {
            "seeds": args.seeds,
            "max_rows": args.max_rows,
            "min_cluster_size": args.min_cluster_size,
            "min_samples": args.min_samples,
            "use_kwh_systems": True,
            "use_log_kwh_systems": True,
            "use_log_superficie": args.use_log_superficie,
            "id_col": args.id_col,
        },
        "outputs": [
            "metricas_muestras.csv",
            "resumen_clusters_muestras.csv",
            "silhouette_clusters_muestras.csv",
            "top_clusters_muestras.csv",
            "resumen_metricas_agregado.csv",
        ],
    }
    (args.output_dir / "config_muestras.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("[OK] archivos compactos generados en", args.output_dir, flush=True)


if __name__ == "__main__":
    main()
