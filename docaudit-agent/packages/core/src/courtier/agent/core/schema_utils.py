"""Schema extraction and merging for cached tool outputs."""

from __future__ import annotations

from typing import Any


def extract_field_paths(data: Any, _prefix: str = "", _max_array_sample: int = 1) -> dict[str, str]:
    """Recursively extract flat field paths → type from JSON data.

    Arrays are sampled (only first element) to avoid traversing large datasets.
    """
    result: dict[str, str] = {}

    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{_prefix}.{key}" if _prefix else key
            if isinstance(value, dict):
                result.update(extract_field_paths(value, path, _max_array_sample))
            elif isinstance(value, list):
                result.update(_extract_array_paths(value, path, _max_array_sample))
            else:
                result[path] = _infer_type(value)
    elif isinstance(data, list):
        result.update(_extract_array_paths(data, _prefix, _max_array_sample))

    return result


def _extract_array_paths(arr: list, path: str, max_sample: int) -> dict[str, str]:
    """Extract field paths from array elements, sampling only the first max_sample items."""
    result: dict[str, str] = {}
    if not arr:
        return {f"{path}[]": "empty_array"}
    sampled = arr[:max_sample]
    for item in sampled:
        if isinstance(item, dict):
            for key, value in item.items():
                item_path = f"{path}[].{key}"
                if isinstance(value, dict):
                    result.update(extract_field_paths(value, item_path, max_sample))
                elif isinstance(value, list):
                    result.update(_extract_array_paths(value, item_path, max_sample))
                else:
                    result[item_path] = _infer_type(value)
        elif isinstance(item, list):
            result.update(_extract_array_paths(item, f"{path}[]", max_sample))
        else:
            result[f"{path}[]"] = _infer_type(item)
    return result


def _infer_type(value: Any) -> str:
    """Infer the type of a value as a string label.

    IMPORTANT: bool is checked before int because bool is a subclass of int in Python.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def merge_schema(base: dict | None, actual: dict[str, str]) -> dict[str, str]:
    """Merge tool-declared base schema with actual field paths from data.

    Returns merged dict where:
      - Standard fields appear as-is
      - Fields in actual but not in base are prefixed with '+'
    """
    if base is None:
        return dict(actual)

    base_paths: set[str] = set()
    if isinstance(base, dict):
        _collect_schema_paths(base, "", base_paths)

    def _is_in_base(p: str) -> bool:
        """Check if a field path or any of its ancestors is declared in the base schema."""
        if p in base_paths:
            return True
        # Walk up the path to check ancestor paths (e.g., for "metadata.author",
        # check if "metadata" is in base_paths as an object).
        while "." in p:
            p = p.rsplit(".", 1)[0]
            if p in base_paths:
                return True
        return False

    merged: dict[str, str] = {}
    for path, typ in actual.items():
        if _is_in_base(path):
            merged[path] = typ
        else:
            merged[f"+{path}"] = typ

    return merged


def _collect_schema_paths(schema: dict, prefix: str, result: set[str]) -> None:
    """Collect dot-separated field paths from a JSON Schema definition."""
    props = schema.get("properties", {})
    if not props:
        return
    for key, prop in props.items():
        path = f"{prefix}.{key}" if prefix else key
        result.add(path)
        if isinstance(prop, dict):
            prop_type = prop.get("type")
            if prop_type == "object":
                _collect_schema_paths(prop, path, result)
            elif prop_type == "array":
                items = prop.get("items", {})
                if isinstance(items, dict):
                    _collect_schema_paths(items, f"{path}[]", result)
