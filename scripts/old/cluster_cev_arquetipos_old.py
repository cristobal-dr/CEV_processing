from __future__ import annotations

"""Version historica/deprecada del clustering CEV.

Este script conserva el flujo anterior, donde `calef_proyec` y `acs_proyec`
podian entrar como descripciones textuales categoricas. Ese enfoque quedo
deprecado porque el texto libre de equipamientos generaba muchas categorias
equivalentes o casi equivalentes, fragmentaba los grupos y elevaba el ruido de
HDBSCAN. La version principal en `../cluster_cev_arquetipos.py` usa
`calef_proyec_kwh` y `acs_proyec_kwh` como variables numericas con `log1p`,
lo que produjo clusters mas interpretables y menor ruido.
"""

import argparse
import json
import platform
import re
import sys
import warnings
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import gower
import hdbscan
from scipy import stats
from sklearn.compose import ColumnTransformer
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_samples, silhouette_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from tabulate import tabulate


COLUMN_ALIASES: dict[str, list[str]] = {
    "superficie_util": ["superficie_util", "superficie util", "sup_util", "superficie"],
    "zona_termica": ["zona_termica", "zona termica", "zt"],
    "tipo_inmueble": ["tipo_inmueble", "tipo inmueble", "tipo_vivienda"],
    "calef_proyec": ["calef_proyec", "calefaccion_proyectada", "calefaccion proyectada"],
    "acs_proyec": ["acs_proyec", "acs_proyectada", "agua_caliente_sanitaria_proyectada"],
    "calef_proyec_kwh": ["calef_proyec_kwh", "calefaccion_proyectada_kwh"],
    "acs_proyec_kwh": ["acs_proyec_kwh", "acs_proyectada_kwh"],
    "u_norm_muro_principal": [
        "u_norm_muro_principal",
        "exigencia_u_muro_principal",
        "u_muro_principal_normativa",
        "u_muro_principal",
    ],
    "u_norm_muro_secundario": [
        "u_norm_muro_secundario",
        "exigencia_u_muro_secundario",
        "u_muro_secundario_normativa",
        "u_muro_secundario",
    ],
    "u_norm_techo_principal": [
        "u_norm_techo_principal",
        "exigencia_u_techo_principal",
        "u_techo_principal_normativa",
        "u_techo_principal",
    ],
    "u_norm_techo_secundario": [
        "u_norm_techo_secundario",
        "exigencia_u_techo_secundario",
        "u_techo_secundario_normativa",
        "u_techo_secundario",
    ],
}

NUMERIC_BASE_COLS = [
    "superficie_util",
    "u_norm_muro_principal",
    "u_norm_muro_secundario",
    "u_norm_techo_principal",
    "u_norm_techo_secundario",
]
SYSTEM_KWH_COLS = ["calef_proyec_kwh", "acs_proyec_kwh"]
CATEGORICAL_COLS = ["zona_termica", "tipo_inmueble", "calef_proyec", "acs_proyec"]
BASE_CATEGORICAL_COLS = ["zona_termica", "tipo_inmueble"]
MISSING_CATEGORY_VALUES = {
    "",
    "nan",
    "none",
    "s/i",
    "sin informacion",
    "sin información",
    "na",
    "n/a",
    "null",
}


def normalize_column_names(columns: pd.Index | list[str]) -> list[str]:
    """Normalize dataframe column names for predictable matching."""
    return [re.sub(r"\s+", "_", str(col).strip().lower()) for col in columns]


def load_data(input_path: Path) -> pd.DataFrame:
    """Load csv, xlsx, or parquet input data."""
    if not input_path.exists():
        raise FileNotFoundError(f"No existe el archivo de entrada: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(input_path)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(input_path)
    if suffix == ".parquet":
        return pd.read_parquet(input_path)

    raise ValueError("Formato no soportado. Usa .csv, .xlsx o .parquet.")


def resolve_column_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known aliases to canonical clustering column names."""
    normalized_aliases = {
        key: normalize_column_names(values) for key, values in COLUMN_ALIASES.items()
    }
    rename_map: dict[str, str] = {}

    for canonical, aliases in normalized_aliases.items():
        matches = [col for col in df.columns if col in aliases]
        if not matches:
            continue
        if canonical not in df.columns:
            rename_map[matches[0]] = canonical
        extra_matches = [match for match in matches[1:] if match != canonical]
        if extra_matches:
            warnings.warn(
                f"Multiples alias encontrados para {canonical}: {matches}. "
                f"Se usara {matches[0]}.",
                stacklevel=2,
            )

    return df.rename(columns=rename_map)


def clean_categorical(series: pd.Series) -> pd.Series:
    """Normalize categorical strings and convert empty-like values to NaN."""
    cleaned = (
        series.astype("string")
        .str.lower()
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
    )
    return cleaned.mask(cleaned.isin(MISSING_CATEGORY_VALUES), np.nan)


def clean_numeric(series: pd.Series) -> pd.Series:
    """Convert messy numeric strings to float, accepting comma decimals."""
    text = series.astype("string").str.strip()
    both_sep = text.str.contains(",", na=False) & text.str.contains(r"\.", na=False)
    text = text.where(~both_sep, text.str.replace(".", "", regex=False))
    text = text.str.replace(",", ".", regex=False)
    text = text.str.replace(r"[^0-9eE+\-.]", "", regex=True)
    text = text.mask(text.isin(["", "+", "-", ".", "+.", "-."]), pd.NA)
    return pd.to_numeric(text, errors="coerce").astype(float)


def prepare_clustering_dataframe(
    df: pd.DataFrame,
    use_log_superficie: bool,
    use_kwh_systems: bool = False,
    use_log_kwh_systems: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    """Clean, validate, impute, and select final variables for clustering."""
    df = df.copy()
    df.columns = normalize_column_names(df.columns)
    df = resolve_column_aliases(df)
    before_dedup = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    if before_dedup != len(df):
        print(f"[INFO] Duplicados exactos eliminados: {before_dedup - len(df)}", flush=True)

    categorical_cols = BASE_CATEGORICAL_COLS.copy() if use_kwh_systems else CATEGORICAL_COLS.copy()
    numeric_raw_cols = NUMERIC_BASE_COLS + (SYSTEM_KWH_COLS if use_kwh_systems else [])
    required = numeric_raw_cols + categorical_cols
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(
            "Faltan columnas requeridas tras normalizar nombres y alias: "
            + ", ".join(missing)
        )

    for col in numeric_raw_cols:
        df[col] = clean_numeric(df[col])
    for col in categorical_cols:
        df[col] = clean_categorical(df[col])

    if (df["superficie_util"] < 0).any():
        warnings.warn(
            "Se encontraron superficies negativas; log1p producira NaN para esos casos.",
            stacklevel=2,
        )

    numeric_cols = NUMERIC_BASE_COLS.copy()
    if use_log_superficie:
        df["log_superficie_util"] = np.log1p(df["superficie_util"])
        numeric_cols = ["log_superficie_util"] + NUMERIC_BASE_COLS[1:]

    if use_kwh_systems:
        if use_log_kwh_systems:
            for col in SYSTEM_KWH_COLS:
                log_col = f"log_{col}"
                if (df[col] < 0).any():
                    warnings.warn(
                        f"Se encontraron valores negativos en {col}; log1p producira NaN.",
                        stacklevel=2,
                    )
                df[log_col] = np.log1p(df[col])
                numeric_cols.append(log_col)
        else:
            numeric_cols.extend(SYSTEM_KWH_COLS)

    final_cols = numeric_cols + categorical_cols
    missing_before = df[final_cols].isna().sum().rename("missing_before")

    clustering_df = df[final_cols].copy()
    for col in numeric_cols:
        median = clustering_df[col].median()
        if pd.isna(median):
            raise ValueError(f"La columna numerica {col} no tiene valores validos.")
        clustering_df[col] = clustering_df[col].fillna(median)
    for col in categorical_cols:
        clustering_df[col] = clustering_df[col].fillna("desconocido")

    missing_after = clustering_df.isna().sum().rename("missing_after")
    missing_report = pd.concat([missing_before, missing_after], axis=1)
    missing_report["pct_missing_before"] = missing_report["missing_before"] / len(df) * 100
    missing_report["pct_missing_after"] = missing_report["missing_after"] / len(df) * 100
    missing_report.index.name = "variable"
    missing_report = missing_report.reset_index()

    return df, clustering_df, missing_report, numeric_cols, categorical_cols


def compute_gower_distance(clustering_df: pd.DataFrame) -> np.ndarray:
    """Compute a dense Gower distance matrix for mixed data."""
    n = len(clustering_df)
    print(
        "[WARN] La matriz de Gower escala aproximadamente como n x n; "
        "puede ser costosa en memoria para datasets grandes.",
        flush=True,
    )
    if n > 20_000:
        print(
            "[WARN] Dataset con mas de 20.000 filas: la matriz densa puede requerir "
            "mucha memoria y tiempo.",
            flush=True,
        )

    gower_input = clustering_df.copy()
    for col in gower_input.select_dtypes(include=["string"]).columns:
        gower_input[col] = gower_input[col].astype(object)

    distance_matrix = gower.gower_matrix(gower_input)
    distance_matrix = np.asarray(distance_matrix, dtype=np.float64)
    np.fill_diagonal(distance_matrix, 0.0)
    return distance_matrix


def run_hdbscan(
    distance_matrix: np.ndarray,
    min_cluster_size: int,
    min_samples: int,
) -> hdbscan.HDBSCAN:
    """Run HDBSCAN over a precomputed distance matrix."""
    clusterer = hdbscan.HDBSCAN(
        metric="precomputed",
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        cluster_selection_method="eom",
        prediction_data=False,
    )
    clusterer.fit(distance_matrix)
    return clusterer


def compute_metrics(labels: np.ndarray, distance_matrix: np.ndarray) -> dict[str, Any]:
    """Compute clustering diagnostics and optional silhouette score."""
    total = int(len(labels))
    non_noise = labels != -1
    cluster_labels = sorted(int(label) for label in set(labels) if label != -1)
    noise_count = int((labels == -1).sum())

    metrics: dict[str, Any] = {
        "n_total": total,
        "n_clusters_excluding_noise": len(cluster_labels),
        "n_noise": noise_count,
        "pct_noise": float(noise_count / total * 100) if total else 0.0,
        "silhouette_precomputed_non_noise": None,
    }

    filtered_labels = labels[non_noise]
    if len(set(filtered_labels)) >= 2 and len(filtered_labels) > len(set(filtered_labels)):
        try:
            filtered_dist = distance_matrix[np.ix_(non_noise, non_noise)]
            metrics["silhouette_precomputed_non_noise"] = float(
                silhouette_score(filtered_dist, filtered_labels, metric="precomputed")
            )
        except Exception as exc:
            metrics["silhouette_error"] = str(exc)

    return metrics


def compute_silhouette_by_cluster(
    labels: np.ndarray,
    distance_matrix: np.ndarray,
) -> pd.DataFrame:
    """Compute silhouette diagnostics by non-noise cluster."""
    non_noise = labels != -1
    filtered_labels = labels[non_noise]
    if len(set(filtered_labels)) < 2 or len(filtered_labels) <= len(set(filtered_labels)):
        return pd.DataFrame(
            columns=[
                "cluster_hdbscan",
                "n",
                "silhouette_media",
                "silhouette_mediana",
                "silhouette_min",
                "silhouette_p25",
                "silhouette_p75",
                "silhouette_max",
                "pct_silhouette_negativo",
            ]
        )

    filtered_dist = distance_matrix[np.ix_(non_noise, non_noise)]
    sample_values = silhouette_samples(filtered_dist, filtered_labels, metric="precomputed")
    values_df = pd.DataFrame(
        {
            "cluster_hdbscan": filtered_labels,
            "silhouette": sample_values,
        }
    )

    rows: list[dict[str, Any]] = []
    for cluster, group in values_df.groupby("cluster_hdbscan"):
        values = group["silhouette"]
        rows.append(
            {
                "cluster_hdbscan": int(cluster),
                "n": int(len(values)),
                "silhouette_media": float(values.mean()),
                "silhouette_mediana": float(values.median()),
                "silhouette_min": float(values.min()),
                "silhouette_p25": float(values.quantile(0.25)),
                "silhouette_p75": float(values.quantile(0.75)),
                "silhouette_max": float(values.max()),
                "pct_silhouette_negativo": float((values < 0).mean() * 100),
            }
        )

    return pd.DataFrame(rows).sort_values("cluster_hdbscan").reset_index(drop=True)


def numeric_stats(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Return descriptive numeric statistics."""
    rows: list[dict[str, Any]] = []
    for col in cols:
        values = df[col].dropna()
        mean = values.mean()
        std = values.std()
        rows.append(
            {
                "variable": col,
                "count": int(values.count()),
                "media": mean,
                "mediana": values.median(),
                "desviacion_estandar": std,
                "minimo": values.min(),
                "p25": values.quantile(0.25),
                "p75": values.quantile(0.75),
                "maximo": values.max(),
                "coeficiente_variacion": std / mean if mean not in [0, np.nan] else np.nan,
            }
        )
    return pd.DataFrame(rows)


def categorical_stats(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Return compact categorical statistics."""
    rows: list[dict[str, Any]] = []
    for col in cols:
        counts = df[col].value_counts(dropna=False)
        total = counts.sum()
        moda = counts.index[0] if len(counts) else np.nan
        rows.append(
            {
                "variable": col,
                "moda": moda,
                "frecuencia_moda": int(counts.iloc[0]) if len(counts) else 0,
                "pct_moda": float(counts.iloc[0] / total * 100) if total else 0.0,
                "n_categorias": int(df[col].nunique(dropna=False)),
                "top_categorias": "; ".join(
                    f"{idx}: {count} ({count / total * 100:.1f}%)"
                    for idx, count in counts.head(8).items()
                ),
            }
        )
    return pd.DataFrame(rows)


def compute_statistical_analysis(
    result_df: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
) -> dict[str, pd.DataFrame]:
    """Generate descriptive statistics and exploratory tests."""
    non_noise = result_df[result_df["cluster_hdbscan"] != -1].copy()

    cluster_numeric_rows = []
    for cluster, group in result_df.groupby("cluster_hdbscan"):
        stats_df = numeric_stats(group, numeric_cols)
        stats_df.insert(0, "cluster_hdbscan", cluster)
        cluster_numeric_rows.append(stats_df)

    cluster_categorical_rows = []
    for cluster, group in result_df.groupby("cluster_hdbscan"):
        stats_df = categorical_stats(group, categorical_cols)
        stats_df.insert(0, "cluster_hdbscan", cluster)
        cluster_categorical_rows.append(stats_df)

    tests: list[dict[str, Any]] = []
    for col in numeric_cols:
        groups = [
            group[col].dropna().to_numpy()
            for _, group in non_noise.groupby("cluster_hdbscan")
            if group[col].dropna().size > 0
        ]
        if len(groups) >= 2:
            try:
                stat, p_value = stats.kruskal(*groups)
                n_obs = int(sum(len(group) for group in groups))
                k_groups = len(groups)
                dof = k_groups - 1
                epsilon_squared = (
                    (float(stat) - k_groups + 1) / (n_obs - k_groups)
                    if n_obs > k_groups
                    else np.nan
                )
                tests.append(
                    {
                        "variable": col,
                        "tipo": "numerica",
                        "prueba": "Kruskal-Wallis",
                        "estadistico": float(stat),
                        "p_value": float(p_value),
                        "grados_libertad": int(dof),
                        "tamano_efecto": float(epsilon_squared),
                        "tamano_efecto_nombre": "epsilon_cuadrado",
                        "nota": "Exploratoria, no concluyente.",
                    }
                )
            except Exception as exc:
                tests.append({"variable": col, "tipo": "numerica", "error": str(exc)})

    for col in categorical_cols:
        if non_noise["cluster_hdbscan"].nunique() >= 2 and non_noise[col].nunique() >= 2:
            try:
                contingency = pd.crosstab(non_noise["cluster_hdbscan"], non_noise[col])
                stat, p_value, dof, _ = stats.chi2_contingency(contingency)
                n_obs = contingency.to_numpy().sum()
                min_dim = min(contingency.shape) - 1
                cramers_v = np.sqrt(float(stat) / (n_obs * min_dim)) if min_dim > 0 else np.nan
                tests.append(
                    {
                        "variable": col,
                        "tipo": "categorica",
                        "prueba": "Chi-cuadrado independencia",
                        "estadistico": float(stat),
                        "p_value": float(p_value),
                        "grados_libertad": int(dof),
                        "tamano_efecto": float(cramers_v),
                        "tamano_efecto_nombre": "v_cramer",
                        "nota": "Exploratoria, no concluyente.",
                    }
                )
            except Exception as exc:
                tests.append({"variable": col, "tipo": "categorica", "error": str(exc)})

    return {
        "estadisticos_globales_numericos": numeric_stats(result_df, numeric_cols),
        "estadisticos_clusters_numericos": pd.concat(
            cluster_numeric_rows, ignore_index=True
        )
        if cluster_numeric_rows
        else pd.DataFrame(),
        "estadisticos_globales_categoricos": categorical_stats(result_df, categorical_cols),
        "estadisticos_clusters_categoricos": pd.concat(
            cluster_categorical_rows, ignore_index=True
        )
        if cluster_categorical_rows
        else pd.DataFrame(),
        "pruebas_estadisticas_exploratorias": pd.DataFrame(tests),
    }


def profile_clusters(
    result_df: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Create cluster summaries and preliminary archetype labels."""
    total = len(result_df)
    summary_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    categorical_rows: list[dict[str, Any]] = []

    for cluster, group in result_df.groupby("cluster_hdbscan"):
        cluster = int(cluster)
        row: dict[str, Any] = {
            "cluster_hdbscan": cluster,
            "n": int(len(group)),
            "pct_total": float(len(group) / total * 100) if total else 0.0,
        }

        for col in numeric_cols:
            row[f"{col}_mediana"] = group[col].median()
            row[f"{col}_media"] = group[col].mean()
            row[f"{col}_p25"] = group[col].quantile(0.25)
            row[f"{col}_p75"] = group[col].quantile(0.75)
            numeric_rows.append(
                {
                    "cluster_hdbscan": cluster,
                    "variable": col,
                    "count": int(group[col].count()),
                    "media": group[col].mean(),
                    "mediana": group[col].median(),
                    "p25": group[col].quantile(0.25),
                    "p75": group[col].quantile(0.75),
                }
            )

        for col in SYSTEM_KWH_COLS:
            if col in group.columns and col not in numeric_cols:
                row[f"{col}_mediana"] = group[col].median()
                row[f"{col}_media"] = group[col].mean()
                row[f"{col}_p25"] = group[col].quantile(0.25)
                row[f"{col}_p75"] = group[col].quantile(0.75)

        for col in categorical_cols:
            counts = group[col].value_counts(dropna=False)
            total_cluster = counts.sum()
            mode = counts.index[0] if len(counts) else np.nan
            row[f"{col}_moda"] = mode
            row[f"{col}_pct_moda"] = (
                float(counts.iloc[0] / total_cluster * 100) if total_cluster else 0.0
            )
            for category, count in counts.head(10).items():
                categorical_rows.append(
                    {
                        "cluster_hdbscan": cluster,
                        "variable": col,
                        "categoria": category,
                        "frecuencia": int(count),
                        "pct_cluster": float(count / total_cluster * 100)
                        if total_cluster
                        else 0.0,
                        "n_categorias": int(group[col].nunique(dropna=False)),
                    }
                )

        if cluster != -1:
            sup_median = group["superficie_util"].median()
            calef_label = (
                row.get("calef_proyec_moda")
                if "calef_proyec_moda" in row
                else f"{group['calef_proyec_kwh'].median():.0f} kWh"
                if "calef_proyec_kwh" in group.columns
                else "sin dato"
            )
            acs_label = (
                row.get("acs_proyec_moda")
                if "acs_proyec_moda" in row
                else f"{group['acs_proyec_kwh'].median():.0f} kWh"
                if "acs_proyec_kwh" in group.columns
                else "sin dato"
            )
            row["etiqueta_arquetipo_preliminar"] = (
                f"{row.get('tipo_inmueble_moda')} | {row.get('zona_termica_moda')} | "
                f"sup_mediana {sup_median:.0f} m2 | "
                f"calef: {calef_label} | acs: {acs_label}"
            )
        else:
            row["etiqueta_arquetipo_preliminar"] = "ruido / observacion atipica"

        summary_rows.append(row)

    return (
        pd.DataFrame(summary_rows).sort_values("cluster_hdbscan").reset_index(drop=True),
        pd.DataFrame(numeric_rows),
        pd.DataFrame(categorical_rows),
    )


def find_cluster_medoids(
    result_df: pd.DataFrame,
    distance_matrix: np.ndarray,
    id_col: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Select the medoid-like representative observation for each non-noise cluster."""
    rows: list[dict[str, Any]] = []
    full_rows: list[pd.Series] = []

    for cluster in sorted(label for label in result_df["cluster_hdbscan"].unique() if label != -1):
        positions = np.flatnonzero(result_df["cluster_hdbscan"].to_numpy() == cluster)
        subdist = distance_matrix[np.ix_(positions, positions)]
        mean_dist = subdist.mean(axis=1) if len(positions) > 1 else np.array([0.0])
        best_pos = int(positions[int(np.argmin(mean_dist))])
        representative = result_df.iloc[best_pos].copy()
        representative["cluster_hdbscan"] = int(cluster)
        full_rows.append(representative)
        rows.append(
            {
                "cluster_hdbscan": int(cluster),
                "row_position": best_pos,
                "index_original": result_df.index[best_pos],
                "id_col": id_col,
                "id_value": representative.get(id_col) if id_col else None,
                "distancia_media_intra_cluster": float(mean_dist.min()),
            }
        )

    return pd.DataFrame(rows), pd.DataFrame(full_rows)


def _save_bar(path: Path, title: str, data: pd.Series, ylabel: str) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    data.plot(kind="bar", ax=ax, color="#2f6f73")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("cluster_hdbscan")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def generate_figures(
    result_df: pd.DataFrame,
    clustering_df: pd.DataFrame,
    output_dir: Path,
    numeric_cols: list[str],
    categorical_cols: list[str],
    random_state: int,
) -> list[Path]:
    """Generate required diagnostic figures."""
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    cluster_sizes = result_df["cluster_hdbscan"].value_counts().sort_index()
    path = figures_dir / "cluster_sizes.png"
    _save_bar(path, "Tamano de clusters", cluster_sizes, "observaciones")
    generated.append(path)

    path = figures_dir / "noise_share.png"
    fig, ax = plt.subplots(figsize=(6, 5))
    noise = int((result_df["cluster_hdbscan"] == -1).sum())
    clustered = int(len(result_df) - noise)
    ax.bar(["clusterizadas", "ruido"], [clustered, noise], color=["#2f6f73", "#b44b4b"])
    ax.set_ylabel("observaciones")
    ax.set_title("Observaciones clusterizadas vs ruido")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    generated.append(path)

    boxplot_cols = ["superficie_util"] + [c for c in NUMERIC_BASE_COLS[1:] if c in result_df.columns]
    boxplot_cols.extend([c for c in SYSTEM_KWH_COLS if c in result_df.columns])
    for col in boxplot_cols:
        path = figures_dir / f"{col}_by_cluster.png"
        fig, ax = plt.subplots(figsize=(10, 5))
        result_df.boxplot(column=col, by="cluster_hdbscan", ax=ax, grid=False)
        ax.set_title(f"{col} por cluster")
        ax.set_xlabel("cluster_hdbscan")
        ax.set_ylabel(col)
        fig.suptitle("")
        fig.tight_layout()
        fig.savefig(path, dpi=160)
        plt.close(fig)
        generated.append(path)

    for col in ["tipo_inmueble", "zona_termica"]:
        if col not in categorical_cols:
            continue
        path = figures_dir / f"categoria_{col}_by_cluster.png"
        pct = pd.crosstab(result_df["cluster_hdbscan"], result_df[col], normalize="index") * 100
        top_cols = result_df[col].value_counts().head(8).index
        pct = pct.loc[:, pct.columns.isin(top_cols)]
        fig, ax = plt.subplots(figsize=(11, 6))
        pct.plot(kind="bar", stacked=True, ax=ax, colormap="tab20")
        ax.set_ylabel("% del cluster")
        ax.set_xlabel("cluster_hdbscan")
        ax.set_title(f"Distribucion porcentual de {col}")
        ax.legend(title=col, bbox_to_anchor=(1.02, 1), loc="upper left")
        fig.tight_layout()
        fig.savefig(path, dpi=160)
        plt.close(fig)
        generated.append(path)

    try:
        path = figures_dir / "cluster_pca_2d.png"
        numeric_for_pca = [col for col in numeric_cols if col in clustering_df.columns]
        categorical_for_pca = [col for col in categorical_cols if col in clustering_df.columns]
        preprocessor = ColumnTransformer(
            transformers=[
                ("num", StandardScaler(), numeric_for_pca),
                (
                    "cat",
                    OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                    categorical_for_pca,
                ),
            ]
        )
        pipeline = Pipeline([("prep", preprocessor), ("pca", PCA(n_components=2, random_state=random_state))])
        coords = pipeline.fit_transform(clustering_df)
        fig, ax = plt.subplots(figsize=(8, 6))
        scatter = ax.scatter(
            coords[:, 0],
            coords[:, 1],
            c=result_df["cluster_hdbscan"],
            cmap="tab20",
            s=14,
            alpha=0.75,
        )
        ax.set_title("PCA 2D auxiliar (no usada para clusterizar)")
        ax.set_xlabel("PC1")
        ax.set_ylabel("PC2")
        fig.colorbar(scatter, ax=ax, label="cluster_hdbscan")
        fig.tight_layout()
        fig.savefig(path, dpi=160)
        plt.close(fig)
        generated.append(path)
    except Exception as exc:
        print(f"[WARN] No se pudo generar PCA 2D: {exc}", flush=True)

    return generated


def package_versions() -> dict[str, str]:
    """Collect Python and library versions for reproducibility."""
    packages = ["pandas", "numpy", "scikit-learn", "hdbscan", "gower", "matplotlib", "scipy"]
    versions = {"python": platform.python_version(), "platform": platform.platform()}
    for package in packages:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "no instalado"
    return versions


def save_outputs(
    result_df: pd.DataFrame,
    summary: pd.DataFrame,
    numeric_profile: pd.DataFrame,
    categorical_profile: pd.DataFrame,
    medoids: pd.DataFrame,
    medoid_rows: pd.DataFrame,
    metrics: dict[str, Any],
    missing_report: pd.DataFrame,
    stats_tables: dict[str, pd.DataFrame],
    config: dict[str, Any],
    output_dir: Path,
) -> list[Path]:
    """Persist tabular outputs, metrics, and configuration."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    outputs: dict[str, pd.DataFrame] = {
        "resumen_clusters.csv": summary,
        "perfil_numerico_clusters.csv": numeric_profile,
        "perfil_categorico_clusters.csv": categorical_profile,
        "representantes_clusters.csv": medoids,
        "representantes_clusters_filas_completas.csv": medoid_rows,
        "missing_values_report.csv": missing_report,
        **{f"{name}.csv": table for name, table in stats_tables.items()},
    }

    csv_path = output_dir / "fichas_cev_clusterizadas.csv"
    result_df.to_csv(csv_path, index=False)
    written.append(csv_path)

    parquet_path = output_dir / "fichas_cev_clusterizadas.parquet"
    result_df.to_parquet(parquet_path, index=False)
    written.append(parquet_path)

    for filename, table in outputs.items():
        path = output_dir / filename
        table.to_csv(path, index=False)
        written.append(path)

    metrics_path = output_dir / "metricas_clustering.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    written.append(metrics_path)

    config_path = output_dir / "config_clustering.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    written.append(config_path)

    return written


def _markdown_table(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df.empty:
        return "_Sin datos disponibles._"
    return tabulate(df.head(max_rows), headers="keys", tablefmt="github", showindex=False)


def _format_pct(value: Any) -> str:
    """Format a percentage with explicit symbol for Markdown reports."""
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.2f}%".replace(".", ",")


def generate_markdown_report(
    reports_dir: Path,
    output_dir: Path,
    metrics: dict[str, Any],
    summary: pd.DataFrame,
    missing_report: pd.DataFrame,
    stats_tables: dict[str, pd.DataFrame],
    figures: list[Path],
    written_files: list[Path],
    numeric_cols: list[str],
    categorical_cols: list[str],
    config: dict[str, Any],
    report_name: str = "reporte_clusterizacion_cev_arquetipos.md",
) -> Path:
    """Write the Markdown report requested by the workflow."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / report_name

    def rel(path: Path) -> str:
        try:
            return str(path.relative_to(report_path.parent))
        except ValueError:
            return str(Path("..") / path)

    cluster_sizes = summary[summary["cluster_hdbscan"] != -1]["n"]
    size_text = {
        "minimo": int(cluster_sizes.min()) if not cluster_sizes.empty else None,
        "mediano": float(cluster_sizes.median()) if not cluster_sizes.empty else None,
        "maximo": int(cluster_sizes.max()) if not cluster_sizes.empty else None,
    }
    non_noise_summary = summary[summary["cluster_hdbscan"] != -1].copy()
    largest_cluster = (
        non_noise_summary.sort_values("n", ascending=False).iloc[0]
        if not non_noise_summary.empty
        else None
    )
    largest_cluster_text = "n/a"
    if largest_cluster is not None:
        largest_cluster_text = (
            f"{int(largest_cluster['cluster_hdbscan'])}, con {int(largest_cluster['n'])} "
            f"fichas ({largest_cluster['pct_total']:.2f}% del total) y etiqueta preliminar: "
            f"{largest_cluster.get('etiqueta_arquetipo_preliminar')}"
        )
    top_types = (
        non_noise_summary["tipo_inmueble_moda"].value_counts().head(3).index.tolist()
        if "tipo_inmueble_moda" in non_noise_summary.columns
        else []
    )
    top_zones = (
        non_noise_summary["zona_termica_moda"].value_counts().head(3).index.tolist()
        if "zona_termica_moda" in non_noise_summary.columns
        else []
    )
    dominant_heating = (
        non_noise_summary["calef_proyec_moda"].value_counts().head(3).index.tolist()
        if "calef_proyec_moda" in non_noise_summary.columns
        else []
    )
    dominant_acs = (
        non_noise_summary["acs_proyec_moda"].value_counts().head(3).index.tolist()
        if "acs_proyec_moda" in non_noise_summary.columns
        else []
    )
    if not dominant_heating and "calef_proyec_kwh_mediana" in non_noise_summary.columns:
        dominant_heating = [
            f"mediana kWh {non_noise_summary['calef_proyec_kwh_mediana'].median():.0f}"
        ]
    if not dominant_acs and "acs_proyec_kwh_mediana" in non_noise_summary.columns:
        dominant_acs = [
            f"mediana kWh {non_noise_summary['acs_proyec_kwh_mediana'].median():.0f}"
        ]
    silhouette_clusters = stats_tables.get("silhouette_clusters", pd.DataFrame())
    silhouette_lookup = (
        silhouette_clusters.set_index("cluster_hdbscan")["silhouette_media"].to_dict()
        if not silhouette_clusters.empty and "silhouette_media" in silhouette_clusters.columns
        else {}
    )

    variables_rows = []
    for col in numeric_cols + categorical_cols:
        miss = missing_report.loc[missing_report["variable"] == col]
        variables_rows.append(
            {
                "variable": col,
                "tipo": "numerica" if col in numeric_cols else "categorica",
                "tratamiento": "limpieza numerica + mediana"
                if col in numeric_cols
                else "normalizacion string + desconocido",
                "% faltante antes": _format_pct(miss["pct_missing_before"].iloc[0])
                if not miss.empty
                else "",
                "imputacion": "mediana" if col in numeric_cols else "desconocido",
            }
        )

    lines: list[str] = [
        "# Reporte de clusterización de fichas CEV para definición de arquetipos habitacionales",
        "",
        "## Resumen ejecutivo",
        "",
        f"- Registros analizados: {metrics.get('n_input', metrics.get('n_total'))}",
        f"- Registros usados finalmente: {metrics.get('n_total')}",
        f"- Clusters encontrados (sin ruido): {metrics.get('n_clusters_excluding_noise')}",
        f"- Ruido: {metrics.get('n_noise')} ({metrics.get('pct_noise', 0):.2f}%)",
        f"- Se trabajo con {metrics.get('n_total')} fichas sobre {metrics.get('n_input', metrics.get('n_total'))} registros disponibles; si se uso `--max-rows`, la muestra es reproducible.",
        "- Los arquetipos y sus etiquetas son preliminares; requieren revision tecnica antes de usarse como verdad operacional.",
        "",
        "### Principales hallazgos",
        "",
        f"- HDBSCAN identifico {metrics.get('n_clusters_excluding_noise')} clusters no ruido y dejo {metrics.get('n_noise')} observaciones como ruido ({metrics.get('pct_noise', 0):.2f}%).",
        f"- Los clusters son de tamano minimo {size_text['minimo']}, mediano {size_text['mediano']} y maximo {size_text['maximo']}.",
        f"- El cluster no ruido mas grande es {largest_cluster_text}.",
        f"- Tipos de inmueble dominantes entre clusters: {', '.join(map(str, top_types)) if top_types else 'sin datos'}.",
        f"- Zonas termicas dominantes entre clusters: {', '.join(map(str, top_zones)) if top_zones else 'sin datos'}.",
        f"- Sistemas dominantes: calefaccion {', '.join(map(str, dominant_heating)) if dominant_heating else 'sin datos'}; ACS {', '.join(map(str, dominant_acs)) if dominant_acs else 'sin datos'}.",
        "- Las pruebas estadisticas son exploratorias; ayudan a detectar diferencias entre grupos, pero no reemplazan la revision tecnica de los arquetipos.",
        "",
        "## Metodologia",
        "",
        "Se usaron variables mixtas de superficie, zona termica, tipo de inmueble, sistemas proyectados y exigencias U normativas. "
        "La distancia de Gower permite comparar variables numericas y categoricas en una misma matriz de similitud. "
        "HDBSCAN se aplico con `metric=\"precomputed\"` porque no exige fijar previamente la cantidad de clusters y permite clasificar observaciones como ruido (`-1`). "
        "Los datos faltantes se imputaron con mediana en variables numericas y `desconocido` en categoricas. "
        "La matriz de Gower escala aproximadamente como `n x n`, por lo que puede ser costosa para bases grandes.",
        "",
        f"- Parametros: `min_cluster_size={config['params']['min_cluster_size']}`, `min_samples={config['params']['min_samples']}`, `use_log_superficie={config['params']['use_log_superficie']}`, `use_kwh_systems={config['params'].get('use_kwh_systems', False)}`, `use_log_kwh_systems={config['params'].get('use_log_kwh_systems', False)}`",
        "- Visualizacion PCA 2D: auxiliar para inspeccion, no usada para clusterizar.",
        "",
        "## Variables consideradas",
        "",
        _markdown_table(pd.DataFrame(variables_rows)),
        "",
        "## Metricas de clustering",
        "",
        f"- Numero de clusters: {metrics.get('n_clusters_excluding_noise')}",
        f"- Porcentaje de ruido: {metrics.get('pct_noise', 0):.2f}%",
        f"- Silhouette precomputado sin ruido: {metrics.get('silhouette_precomputed_non_noise')}",
        f"- Tamano de clusters: minimo={size_text['minimo']}, mediano={size_text['mediano']}, maximo={size_text['maximo']}",
        "",
        "El silhouette global reportado es el promedio de los silhouette individuales de las observaciones no ruido. "
        "Valores cercanos a 1 indican observaciones bien separadas de otros clusters; valores cercanos a 0 indican fronteras difusas; valores negativos sugieren observaciones mas parecidas a otro cluster. "
        "La tabla siguiente muestra el promedio por cluster para ayudar a detectar grupos mas o menos compactos.",
        "",
        _markdown_table(silhouette_clusters),
        "",
        _markdown_table(summary),
        "",
        "## Visualizaciones",
        "",
    ]

    captions = {
        "cluster_sizes.png": "Tamano de clusters",
        "noise_share.png": "Porcentaje de ruido",
        "superficie_util_by_cluster.png": "Superficie util por cluster",
        "u_norm_muro_principal_by_cluster.png": "U normativa muro principal por cluster",
        "u_norm_muro_secundario_by_cluster.png": "U normativa muro secundario por cluster",
        "u_norm_techo_principal_by_cluster.png": "U normativa techo principal por cluster",
        "u_norm_techo_secundario_by_cluster.png": "U normativa techo secundario por cluster",
        "categoria_tipo_inmueble_by_cluster.png": "Tipo de inmueble por cluster",
        "categoria_zona_termica_by_cluster.png": "Zona termica por cluster",
        "cluster_pca_2d.png": "PCA 2D auxiliar",
    }
    for figure in figures:
        lines.extend([f"![{captions.get(figure.name, figure.stem)}]({rel(figure)})", ""])

    tests = stats_tables.get("pruebas_estadisticas_exploratorias", pd.DataFrame())
    lines.extend(
        [
            "### Como leer el PCA 2D",
            "",
            "El PCA 2D es una proyeccion auxiliar construida con variables numericas escaladas y categoricas codificadas con one-hot. "
            "No fue usado para clusterizar; solo sirve como mapa visual aproximado de similitudes. "
            "Puntos cercanos en el grafico tienden a compartir combinaciones parecidas de variables, y colores separados sugieren clusters visualmente distinguibles. "
            "Si dos colores se superponen, no significa necesariamente que HDBSCAN este mal: la proyeccion reduce muchas dimensiones a dos y puede esconder separaciones que existen en el espacio original de Gower.",
            "",
            "## Analisis estadistico",
            "",
            "Los estadisticos y pruebas Kruskal-Wallis o chi-cuadrado son exploratorios, no concluyentes, y no definen por si solos la validez tecnica de un cluster.",
            "",
            "### Estadisticos globales numericos",
            "",
            _markdown_table(stats_tables["estadisticos_globales_numericos"]),
            "",
            "### Estadisticos por cluster",
            "",
            _markdown_table(stats_tables["estadisticos_clusters_numericos"]),
            "",
            "### Como leer las pruebas exploratorias",
            "",
            "Kruskal-Wallis se aplica a variables numericas y compara si las distribuciones por cluster tienden a tener rangos similares. "
            "Es una alternativa no parametrica a ANOVA: no exige normalidad y trabaja con ordenamientos/rangos. "
            "El estadistico H no esta en las unidades originales de la variable: para superficie no son m2, y para exigencias U tampoco son W/m2K. "
            "Es una medida de separacion entre rangos promedio de los clusters. No hay un umbral universal para decir alto o bajo, porque depende del tamano muestral y de los grados de libertad. "
            "Como referencia practica, se compara con una distribucion chi-cuadrado con `k - 1` grados de libertad, donde `k` es la cantidad de clusters comparados: si H es mucho mayor que esos grados de libertad y el p-value es muy pequeno, hay evidencia de diferencias entre clusters. "
            "Para interpretar magnitud, se reporta epsilon-cuadrado: valores cercanos a 0 sugieren efecto pequeno; alrededor de 0,01 pequeno, 0,06 moderado y 0,14 o mas grande, como regla orientativa.",
            "",
            "Chi-cuadrado se aplica a variables categoricas y compara la tabla cluster x categoria contra lo que se esperaria si cluster y categoria fueran independientes. "
            "Su estadistico tampoco tiene unidades; resume cuanto se apartan las frecuencias observadas de las frecuencias esperadas bajo independencia. "
            "Tampoco hay un umbral universal: se evalua contra una distribucion chi-cuadrado con grados de libertad dados por `(filas - 1) * (columnas - 1)`. "
            "Como regla rapida, si el estadistico es mucho mayor que los grados de libertad y el p-value es pequeno, cluster y categoria no parecen independientes. "
            "Para magnitud se reporta V de Cramer, que va entre 0 y 1: cerca de 0 indica asociacion debil; alrededor de 0,1 baja, 0,3 moderada y 0,5 alta, aunque el contexto tecnico importa.",
            "",
            "En esta corrida, p-values muy bajos sugieren que las variables analizadas efectivamente cambian entre clusters. "
            "La conclusion practica no es que los clusters sean automaticamente validos, sino que capturan diferencias observables en superficie, exigencias U, tipo de inmueble, zona termica y sistemas proyectados. "
            "Los tamanos de efecto ayudan a distinguir diferencias estadisticamente detectables de diferencias tecnicamente relevantes.",
            "",
            "### Pruebas exploratorias",
            "",
            _markdown_table(tests),
            "",
            "## Perfil de arquetipos",
            "",
        ]
    )

    for _, row in summary[summary["cluster_hdbscan"] != -1].iterrows():
        cluster = int(row["cluster_hdbscan"])
        lines.extend(
            [
                f"## Arquetipo / Cluster {cluster}",
                "",
                f"- Tamano del cluster: {int(row['n'])}",
                f"- Porcentaje del total: {row['pct_total']:.2f}%",
                f"- Etiqueta preliminar: {row.get('etiqueta_arquetipo_preliminar')}",
                f"- Silhouette promedio del cluster: {silhouette_lookup.get(cluster, 'no calculable')}",
                f"- Superficie util mediana: {row.get('superficie_util_mediana')}",
                f"- Zona termica dominante: {row.get('zona_termica_moda')}",
                f"- Tipo de inmueble dominante: {row.get('tipo_inmueble_moda')}",
                f"- Calefaccion proyectada dominante: {row.get('calef_proyec_moda', row.get('calef_proyec_kwh_mediana'))}",
                f"- ACS proyectado dominante: {row.get('acs_proyec_moda', row.get('acs_proyec_kwh_mediana'))}",
                f"- Exigencia U muro principal mediana: {row.get('u_norm_muro_principal_mediana')}",
                f"- Exigencia U muro secundario mediana: {row.get('u_norm_muro_secundario_mediana')}",
                f"- Exigencia U techo principal mediana: {row.get('u_norm_techo_principal_mediana')}",
                f"- Exigencia U techo secundario mediana: {row.get('u_norm_techo_secundario_mediana')}",
                "- Vivienda representativa: ver `representantes_clusters.csv` y `representantes_clusters_filas_completas.csv`.",
                "- Interpretacion tecnica preliminar: revisar el representante, los percentiles y la distribucion categorica antes de nombrar el arquetipo final.",
                "",
            ]
        )

    lines.extend(
        [
            "## Observaciones ruido",
            "",
            f"- Cantidad: {metrics.get('n_noise')}",
            f"- Porcentaje: {metrics.get('pct_noise', 0):.2f}%",
            "- Posible interpretacion: casos atipicos, combinaciones poco frecuentes o datos inconsistentes. Se recomienda revision tecnica.",
            "",
            "## Limitaciones",
            "",
            "- No se cuenta con año de construcción.",
            "- Los clusters dependen de las variables disponibles.",
            "- Gower + HDBSCAN identifica similitud estadistica, no causalidad.",
            "- Las etiquetas de arquetipo son preliminares.",
            "- La calidad depende de limpieza y consistencia de las fichas CEV.",
            "- No se debe usar silhouette como unico criterio de validez.",
            "",
            "## Conclusiones",
            "",
            "Los clusters mas representativos corresponden a los grupos con mayor cantidad de observaciones en `resumen_clusters.csv`. "
            "Una alta proporcion de ruido debe interpretarse como senal para revisar parametros, calidad de datos o heterogeneidad real. "
            "La siguiente etapa recomendada es revisar tecnicamente las viviendas representantes por cluster.",
            "",
            "## Archivos generados",
            "",
        ]
    )

    for path in sorted(set(written_files + figures)):
        if path.suffix.lower() in {".csv", ".parquet", ".json", ".png"}:
            lines.append(f"- `{rel(path)}`")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def generate_demo_data(random_state: int, n: int = 180) -> pd.DataFrame:
    """Create a small explicit demo dataset for smoke testing."""
    rng = np.random.default_rng(random_state)
    groups = [
        ("casa", "zona 5", "electrica", "gas", 72, 0.6, 0.8, 0.35, 0.45),
        ("departamento", "zona 3", "sin sistema", "electrico", 48, 0.7, 0.9, 0.4, 0.5),
        ("vivienda pareada", "zona 6", "gas", "gas", 95, 0.55, 0.75, 0.32, 0.42),
    ]
    rows = []
    for i in range(n):
        group = groups[i % len(groups)]
        rows.append(
            {
                "id_ficha": f"demo_{i:04d}",
                "tipo_inmueble": group[0],
                "zona_termica": group[1],
                "calef_proyec": group[2],
                "acs_proyec": group[3],
                "calef_proyec_kwh": max(0, rng.normal(group[4] * 60, 700)),
                "acs_proyec_kwh": max(0, rng.normal(group[4] * 45, 400)),
                "superficie_util": max(15, rng.normal(group[4], 8)),
                "u_norm_muro_principal": max(0.1, rng.normal(group[5], 0.04)),
                "u_norm_muro_secundario": max(0.1, rng.normal(group[6], 0.05)),
                "u_norm_techo_principal": max(0.1, rng.normal(group[7], 0.03)),
                "u_norm_techo_secundario": max(0.1, rng.normal(group[8], 0.03)),
            }
        )
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Clusteriza fichas CEV para definir arquetipos habitacionales preliminares."
    )
    parser.add_argument("--input", type=Path, default=None, help="Archivo .csv, .xlsx o .parquet.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--min-cluster-size", type=int, required=True)
    parser.add_argument("--min-samples", type=int, required=True)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--use-log-superficie", action="store_true")
    parser.add_argument(
        "--use-kwh-systems",
        action="store_true",
        help="Usa calef_proyec_kwh y acs_proyec_kwh como variables numericas en vez de descripciones de equipos.",
    )
    parser.add_argument(
        "--use-log-kwh-systems",
        action="store_true",
        help="Usa log1p de calef_proyec_kwh y acs_proyec_kwh para reducir asimetria y outliers.",
    )
    parser.add_argument("--id-col", type=str, default=None)
    parser.add_argument(
        "--report-name",
        type=str,
        default="reporte_clusterizacion_cev_arquetipos.md",
    )
    parser.add_argument("--generate-demo-data", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Run the complete clustering workflow."""
    args = parse_args()
    if not args.generate_demo_data and args.input is None:
        raise SystemExit("Debes indicar --input o usar explicitamente --generate-demo-data.")

    if args.generate_demo_data:
        raw_df = generate_demo_data(args.random_state)
        input_label = "demo_generado"
    else:
        raw_df = load_data(args.input)
        input_label = str(args.input)

    raw_n = len(raw_df)
    if args.max_rows is not None and len(raw_df) > args.max_rows:
        raw_df = raw_df.sample(n=args.max_rows, random_state=args.random_state).reset_index(drop=True)
        print(f"[INFO] Muestra reproducible aplicada: {args.max_rows} filas", flush=True)

    cleaned_df, clustering_df, missing_report, numeric_cols, categorical_cols = (
        prepare_clustering_dataframe(
            raw_df,
            args.use_log_superficie,
            use_kwh_systems=args.use_kwh_systems,
            use_log_kwh_systems=args.use_log_kwh_systems,
        )
    )

    id_col = normalize_column_names([args.id_col])[0] if args.id_col else None
    if id_col and id_col not in cleaned_df.columns:
        raise ValueError(f"--id-col no existe tras normalizar columnas: {id_col}")

    distance_matrix = compute_gower_distance(clustering_df)
    clusterer = run_hdbscan(distance_matrix, args.min_cluster_size, args.min_samples)
    labels = clusterer.labels_.astype(int)

    result_df = cleaned_df.copy()
    result_df["cluster_hdbscan"] = labels
    result_df["cluster_probability"] = getattr(clusterer, "probabilities_", np.full(len(labels), np.nan))
    result_df["cluster_is_noise"] = result_df["cluster_hdbscan"] == -1

    metrics = compute_metrics(labels, distance_matrix)
    metrics["n_input"] = int(raw_n)
    metrics["n_used_after_sampling_and_dedup"] = int(len(result_df))

    summary, numeric_profile, categorical_profile = profile_clusters(
        result_df, numeric_cols, categorical_cols
    )
    stats_tables = compute_statistical_analysis(result_df, numeric_cols, categorical_cols)
    stats_tables["silhouette_clusters"] = compute_silhouette_by_cluster(
        labels, distance_matrix
    )
    medoids, medoid_rows = find_cluster_medoids(result_df, distance_matrix, id_col)
    figures = generate_figures(
        result_df,
        clustering_df,
        args.output_dir,
        numeric_cols,
        categorical_cols,
        args.random_state,
    )

    config = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input": input_label,
        "params": {
            "min_cluster_size": args.min_cluster_size,
            "min_samples": args.min_samples,
            "max_rows": args.max_rows,
            "random_state": args.random_state,
            "use_log_superficie": args.use_log_superficie,
            "use_kwh_systems": args.use_kwh_systems,
            "use_log_kwh_systems": args.use_log_kwh_systems,
            "id_col": id_col,
            "generate_demo_data": args.generate_demo_data,
        },
        "columns": {
            "numeric": numeric_cols,
            "categorical": categorical_cols,
            "aliases": COLUMN_ALIASES,
        },
        "versions": package_versions(),
    }

    written_files = save_outputs(
        result_df,
        summary,
        numeric_profile,
        categorical_profile,
        medoids,
        medoid_rows,
        metrics,
        missing_report,
        stats_tables,
        config,
        args.output_dir,
    )
    report = generate_markdown_report(
        args.reports_dir,
        args.output_dir,
        metrics,
        summary,
        missing_report,
        stats_tables,
        figures,
        written_files,
        numeric_cols,
        categorical_cols,
        config,
        report_name=args.report_name,
    )

    print(f"[OK] Clusters encontrados sin ruido: {metrics['n_clusters_excluding_noise']}", flush=True)
    print(f"[OK] Ruido: {metrics['n_noise']} ({metrics['pct_noise']:.2f}%)", flush=True)
    print(f"[OK] Reporte generado: {report}", flush=True)


if __name__ == "__main__":
    main()
