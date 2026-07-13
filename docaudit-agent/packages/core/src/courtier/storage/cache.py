from pathlib import Path


class FileCache:
    def __init__(self, cache_dir: str):
        self._cache_dir = Path(cache_dir).resolve()

    def get(self, key: str) -> Path | None:
        safe_name = Path(key).name  # strip directory traversal
        path = self._cache_dir / safe_name
        resolved = path.resolve()
        if not resolved.is_relative_to(self._cache_dir):
            return None
        return path if path.exists() else None

    def put(self, key: str, data: bytes) -> Path:
        safe_name = Path(key).name  # strip directory traversal
        path = self._cache_dir / safe_name
        resolved = path.resolve()
        if not resolved.is_relative_to(self._cache_dir):
            raise ValueError(f"Path traversal detected for key: {key!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
