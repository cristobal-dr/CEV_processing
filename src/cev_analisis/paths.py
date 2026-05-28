from __future__ import annotations

from pathlib import Path
from typing import Any


def find_project_root(start: Path | None = None) -> Path:
    """Find the repository root from a script or current working directory."""
    current = (start or Path.cwd()).resolve()

    for candidate in [current, *current.parents]:
        if (candidate / "config").is_dir() and (candidate / "scripts").is_dir():
            return candidate

    return current


def default_paths_config(start: Path | None = None) -> Path:
    return find_project_root(start) / "config" / "paths.yaml"


def _parse_scalar(value: str) -> Any:
    value = value.strip()

    if not value:
        return ""

    if value in {"true", "True"}:
        return True

    if value in {"false", "False"}:
        return False

    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        return value[1:-1]

    return value


def load_paths(config_path: Path | str | None = None) -> dict[str, Any]:
    """
    Load the simple key/value paths file used by this project.

    The file intentionally stays compatible with plain YAML. If PyYAML is
    installed it is used; otherwise this fallback parser accepts the current
    `key: value` format with comments and blank lines.
    """
    path = Path(config_path) if config_path else default_paths_config()
    path = path.expanduser().resolve()

    try:
        import yaml  # type: ignore
    except ImportError:
        data: dict[str, Any] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            data[key.strip()] = _parse_scalar(value)
    else:
        with path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        data = dict(loaded)

    project_root_value = data.get("project_root")
    if project_root_value:
        project_root = Path(str(project_root_value)).expanduser()
        if not project_root.is_absolute():
            project_root = (path.parent / project_root).resolve()
        else:
            project_root = project_root.resolve()
    else:
        project_root = find_project_root(path.parent)

    data["project_root"] = project_root

    for key, value in list(data.items()):
        if key == "project_root" or not key.endswith(("_dir", "_path", "_archive")):
            continue

        value_path = Path(str(value)).expanduser()
        if not value_path.is_absolute():
            value_path = project_root / value_path
        data[key] = value_path.resolve()

    return data


def path_value(
    paths: dict[str, Any],
    key: str,
    fallback: Path | str | None = None,
) -> Path | None:
    value = paths.get(key, fallback)
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    return Path(str(value)).expanduser()
