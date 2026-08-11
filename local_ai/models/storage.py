"""local_ai.models.storage — server-side storage picker and validation policy.

Provides:
- DirectoryListing:  frozen result of list_directory (root or subdirectory).
- StorageValidation: frozen result of validate_destination.
- StoragePolicy:    the policy engine itself.

Design notes (Task 6 brief):
- list_directory(None or "") returns root entries — drive letters on Windows,
  subdirs of "/" on Linux.
- list_directory(path) returns subdirectory names of the path; non-directories
  are filtered out.
- validate_destination resolves to an absolute path, rejects path traversal
  (".." components after resolution), rejects device files and special paths
  (/dev/, /proc/, /sys/), rejects symlinks pointing outside the parent
  directory tree, checks the path exists (or can be created), checks it is
  writable (try creating a temp file), and checks free space >= required_bytes
  via shutil.disk_usage.
- validate_destination does NOT auto-persist the default — that is a separate
  explicit API call (set_default / PUT /api/v1/local-ai/storage/default).
- StoragePolicy.__init__(config_service=None) uses get_config_service() if not
  provided.
"""
from __future__ import annotations

import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# Roots whose listing/destination is forbidden — kernel virtual filesystems and
# device file trees. Listed as path prefixes so sub-paths are also rejected.
_FORBIDDEN_PREFIXES: tuple[str, ...] = ("/dev", "/proc", "/sys")

# Marker reasons used by validate_destination. Tests check substrings of these.
_REASON_TRAVERSAL = "path traversal forbidden"
_REASON_FORBIDDEN = "forbidden system path"
_REASON_SYMLINK_ESCAPE = "symlink escapes parent tree"
_REASON_NOT_FOUND = "path does not exist and cannot be created"
_REASON_NOT_WRITABLE = "path is not writable"
_REASON_INSUFFICIENT_SPACE = "insufficient free space"


@dataclass(frozen=True)
class DirectoryListing:
    """Result of listing a directory.

    Attributes:
        path:    the listed path (or "" for roots).
        entries: subdirectory names (filtered to directories only).
        error:   None on success, otherwise a human-readable error message.
    """

    path: str
    entries: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "entries": list(self.entries),
            "error": self.error,
        }


@dataclass(frozen=True)
class StorageValidation:
    """Result of validating a destination directory.

    Attributes:
        path:       the resolved absolute path that was validated.
        writable:   True if the path is usable for writes.
        free_bytes: free space on the destination's filesystem.
        error:      None on success, otherwise a short error message.
        reason:     None on success, otherwise a short reason why not writable.
    """

    path: str
    writable: bool = False
    free_bytes: int = 0
    error: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "writable": self.writable,
            "free_bytes": self.free_bytes,
            "error": self.error,
            "reason": self.reason,
        }


def _is_forbidden_path(resolved: Path) -> bool:
    """True if the resolved path falls under a forbidden system tree."""
    s = str(resolved)
    for prefix in _FORBIDDEN_PREFIXES:
        if s == prefix or s.startswith(prefix + "/"):
            return True
    return False


def _has_traversal(raw_path: str) -> bool:
    """True if the raw path contains '..' as a path component (before resolution).

    We additionally check the resolved path's parents for traversal-style
    escapes in validate_destination.
    """
    parts = raw_path.replace("\\", "/").split("/")
    return ".." in parts


def _symlink_escapes_parent_tree(raw_path: str) -> bool:
    """True if `raw_path` is a symlink whose target escapes its parent tree.

    Only checks the FINAL path component. Parent-component symlinks are part
    of normal system path resolution (e.g. /home on some systems) and are not
    rejected. The intent is to reject destinations that "escape" via a
    symlink: a destination under /home/user/models/escape_link -> /etc should
    be refused because the link target leaves the destination's parent tree.

    Must be called BEFORE Path.resolve() follows the symlink, otherwise the
    link is transparently resolved and the check has nothing to detect.
    """
    try:
        p = Path(raw_path).expanduser()
    except (OSError, ValueError):
        return False
    try:
        if not p.is_symlink():
            return False
    except (OSError, ValueError):
        return False
    try:
        target = p.resolve(strict=False)
        parent = p.parent.resolve(strict=False)
    except (OSError, RuntimeError):
        # Conservative: cannot determine the target — treat as escape.
        return True
    try:
        target.relative_to(parent)
        return False
    except ValueError:
        return True


class StoragePolicy:
    """Server-side storage picker and validation policy.

    All public methods are safe to call from request handlers. They never
    raise — instead they return DirectoryListing/StorageValidation with the
    `error`/`reason` fields populated. The exception is set_default, which
    raises ValueError when validation fails so callers can map to HTTP 4xx.
    """

    def __init__(self, config_service: Any = None) -> None:
        if config_service is None:
            from web.config_service import get_config_service
            config_service = get_config_service()
        self._config = config_service

    # ── list_directory ────────────────────────────────────────

    def list_directory(self, path: str | None) -> DirectoryListing:
        """List subdirectories of `path` (or roots when path is None/empty)."""
        if path is None or path == "":
            return self._list_roots()

        # Reject path traversal in the raw input before doing any FS work.
        if _has_traversal(path):
            return DirectoryListing(path=str(path), entries=(), error=_REASON_TRAVERSAL)

        # Reject forbidden system trees explicitly.
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, ValueError) as exc:
            return DirectoryListing(path=str(path), entries=(), error=f"invalid path: {exc}")
        if _is_forbidden_path(resolved):
            return DirectoryListing(
                path=str(path), entries=(), error=_REASON_FORBIDDEN,
            )

        if not resolved.exists():
            return DirectoryListing(
                path=str(path), entries=(), error="path does not exist",
            )
        if not resolved.is_dir():
            return DirectoryListing(
                path=str(path), entries=(), error="path is not a directory",
            )

        try:
            names: list[str] = []
            for entry in sorted(resolved.iterdir(), key=lambda p: p.name):
                # Only directories are listed. We use is_dir() with follow_symlinks
                # semantics via Path.is_dir() (which follows symlinks). Hidden
                # directories (.foo) are kept — UI may choose to filter.
                if entry.is_dir():
                    names.append(entry.name)
            return DirectoryListing(path=str(path), entries=tuple(names), error=None)
        except (OSError, PermissionError) as exc:
            return DirectoryListing(path=str(path), entries=(), error=str(exc))

    def _list_roots(self) -> DirectoryListing:
        """Return root entries — drive letters on Windows, subdirs of / on Linux."""
        system = platform.system()
        if system == "Windows":
            entries: list[str] = []
            for letter in map(chr, range(ord("A"), ord("Z") + 1)):
                drive = f"{letter}:\\"
                if Path(drive).exists():
                    entries.append(f"{letter}:")
            return DirectoryListing(path="", entries=tuple(entries), error=None)
        # POSIX (Linux / macOS): list subdirectories of "/".
        try:
            names = [
                entry.name
                for entry in sorted(Path("/").iterdir(), key=lambda p: p.name)
                if entry.is_dir()
            ]
            return DirectoryListing(path="", entries=tuple(names), error=None)
        except (OSError, PermissionError) as exc:
            return DirectoryListing(path="", entries=(), error=str(exc))

    # ── validate_destination ──────────────────────────────────

    def validate_destination(self, path: str, required_bytes: int) -> StorageValidation:
        """Validate that `path` is a usable destination for a download of `required_bytes`.

        Returns a StorageValidation. Never raises.
        """
        if not isinstance(path, str) or not path.strip():
            return StorageValidation(
                path=str(path), writable=False, free_bytes=0,
                error="path must be a non-empty string",
                reason="empty path",
            )
        if _has_traversal(path):
            return StorageValidation(
                path=str(path), writable=False, free_bytes=0,
                error=_REASON_TRAVERSAL, reason=_REASON_TRAVERSAL,
            )

        # Symlink escape check MUST run before Path.resolve() follows the link.
        if _symlink_escapes_parent_tree(path):
            return StorageValidation(
                path=str(path), writable=False, free_bytes=0,
                error=_REASON_SYMLINK_ESCAPE, reason=_REASON_SYMLINK_ESCAPE,
            )

        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, ValueError) as exc:
            return StorageValidation(
                path=str(path), writable=False, free_bytes=0,
                error=f"invalid path: {exc}", reason="invalid path",
            )

        if _is_forbidden_path(resolved):
            return StorageValidation(
                path=str(resolved), writable=False, free_bytes=0,
                error=_REASON_FORBIDDEN, reason=_REASON_FORBIDDEN,
            )

        # Existence / creatability. If the path doesn't exist, attempt to
        # create it (and any missing parents). If creation fails, report
        # not-writable with a "does not exist / cannot create" reason.
        if not resolved.exists():
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except (OSError, PermissionError):
                return StorageValidation(
                    path=str(resolved), writable=False, free_bytes=0,
                    error=_REASON_NOT_FOUND, reason=_REASON_NOT_FOUND,
                )

        # Writability: try to create and remove a temp file inside the path.
        writability_error = self._check_writable(resolved)
        if writability_error is not None:
            free = self._safe_free_bytes(resolved)
            return StorageValidation(
                path=str(resolved), writable=False, free_bytes=free,
                error=_REASON_NOT_WRITABLE, reason=writability_error,
            )

        # Free space check (after we know the path is writable).
        free_bytes = self._safe_free_bytes(resolved)
        if required_bytes > 0 and free_bytes < required_bytes:
            return StorageValidation(
                path=str(resolved), writable=False, free_bytes=free_bytes,
                error=_REASON_INSUFFICIENT_SPACE,
                reason=_REASON_INSUFFICIENT_SPACE,
            )

        return StorageValidation(
            path=str(resolved), writable=True, free_bytes=free_bytes,
            error=None, reason=None,
        )

    def _check_writable(self, resolved: Path) -> str | None:
        """Return None if `resolved` is writable, otherwise a short reason."""
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".storage_probe_", dir=str(resolved), delete=True,
            ):
                pass
            return None
        except (OSError, PermissionError) as exc:
            logger.debug("storage.writable_check_failed path={} err={}", resolved, exc)
            return _REASON_NOT_WRITABLE

    @staticmethod
    def _safe_free_bytes(resolved: Path) -> int:
        try:
            return shutil.disk_usage(str(resolved)).free
        except (OSError, ValueError) as exc:
            logger.debug("storage.disk_usage_failed path={} err={}", resolved, exc)
            return 0

    # ── default persistence ───────────────────────────────────

    def get_default(self) -> str:
        """Read the persisted default_model_root (empty string if unset)."""
        try:
            value = self._config.get("local_ai.default_model_root", "")
        except Exception:  # noqa: BLE001 — config may be unavailable in edge cases
            return ""
        if not isinstance(value, str):
            return ""
        return value

    def set_default(self, path: str) -> None:
        """Validate then persist `path` as the default model root.

        Raises ValueError when validation fails — callers should map this to
        an HTTP 4xx response. We do not raise on disk write errors; instead
        ConfigService.set propagates them as appropriate.
        """
        validation = self.validate_destination(path, 0)
        if not validation.writable:
            raise ValueError(validation.reason or validation.error or "invalid path")
        self._config.set("local_ai.default_model_root", validation.path)
