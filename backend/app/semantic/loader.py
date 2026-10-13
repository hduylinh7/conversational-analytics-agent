"""Loader module for reading and parsing Semantic Layer YAML definition files."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.semantic.models import (
    DataSourcesCatalog,
    DimensionsCatalog,
    MetricsCatalog,
    SemanticCatalog,
)


class SemanticLayerError(Exception):
    """Base exception for all semantic layer loading and parsing errors."""

    pass


class SemanticFileNotFoundError(SemanticLayerError):
    """Raised when a required semantic layer YAML file is missing."""

    pass


class SemanticYamlSyntaxError(SemanticLayerError):
    """Raised when a YAML file contains malformed syntax."""

    pass


class SemanticSchemaValidationError(SemanticLayerError):
    """Raised when YAML structure fails Pydantic schema validation."""

    pass


def get_default_semantic_directory() -> Path:
    """Return the default filesystem directory containing semantic layer YAML files."""
    return Path(__file__).resolve().parent


def load_yaml_file(file_path: Path | str) -> dict[str, Any]:
    """Read and parse a YAML file into a dictionary.

    Args:
        file_path: Absolute or relative path to the YAML file.

    Returns:
        Parsed dictionary.

    Raises:
        SemanticFileNotFoundError: If the file does not exist.
        SemanticYamlSyntaxError: If the file is not valid YAML.
    """
    path = Path(file_path)
    if not path.exists():
        raise SemanticFileNotFoundError(f"Semantic layer file not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            content = yaml.safe_load(f)
            if content is None:
                return {}
            if not isinstance(content, dict):
                raise SemanticYamlSyntaxError(
                    f"YAML file must define a mapping/dictionary at root: {path}"
                )
            return content
    except yaml.YAMLError as exc:
        raise SemanticYamlSyntaxError(
            f"Failed to parse YAML syntax in {path}: {exc}"
        ) from exc


def load_data_sources(file_path: Path | str) -> DataSourcesCatalog:
    """Load and validate data_sources.yaml against DataSourcesCatalog schema."""
    raw = load_yaml_file(file_path)
    try:
        return DataSourcesCatalog.model_validate(raw)
    except ValidationError as exc:
        raise SemanticSchemaValidationError(
            f"Schema validation failed for {file_path}:\n{exc}"
        ) from exc


def load_dimensions(file_path: Path | str) -> DimensionsCatalog:
    """Load and validate dimensions.yaml against DimensionsCatalog schema."""
    raw = load_yaml_file(file_path)
    try:
        return DimensionsCatalog.model_validate(raw)
    except ValidationError as exc:
        raise SemanticSchemaValidationError(
            f"Schema validation failed for {file_path}:\n{exc}"
        ) from exc


def load_metrics(file_path: Path | str) -> MetricsCatalog:
    """Load and validate metrics.yaml against MetricsCatalog schema."""
    raw = load_yaml_file(file_path)
    try:
        return MetricsCatalog.model_validate(raw)
    except ValidationError as exc:
        raise SemanticSchemaValidationError(
            f"Schema validation failed for {file_path}:\n{exc}"
        ) from exc


def load_semantic_layer(directory_path: Path | str | None = None) -> SemanticCatalog:
    """Load the complete SemanticCatalog from data_sources.yaml, dimensions.yaml, and metrics.yaml.

    Args:
        directory_path: Directory containing the YAML files. Defaults to current package directory.

    Returns:
        Populated SemanticCatalog instance.
    """
    directory = Path(directory_path) if directory_path else get_default_semantic_directory()

    data_sources_path = directory / "data_sources.yaml"
    dimensions_path = directory / "dimensions.yaml"
    metrics_path = directory / "metrics.yaml"

    data_sources = load_data_sources(data_sources_path)
    dimensions = load_dimensions(dimensions_path)
    metrics = load_metrics(metrics_path)

    return SemanticCatalog(
        data_sources=data_sources,
        dimensions=dimensions,
        metrics=metrics,
    )
