"""Explicit erasure of local campaign state, including legacy import sources.

This operation never follows paths stored in a receipt and never deletes media.
The caller must hold the exclusive campaign-state lease. A private staging
directory is removed before success; interrupted erasure blocks new campaigns
until the operator retries the same reset. No history backup is retained.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from . import config
from .campaign_history import is_library_marker_name

_STAGING = ".campaign-reset-pending"
_FILES = {
    "sent_videos.json", "sent_reset_history.json", "sidecar_migration.json",
    "start_requests.json", "campaign_start_requests.json", "recording_timeline.json",
    "original_capture_reservations.json", "start_requests.lock", ".original_capture.lock",
}
_DIRECTORIES = {"sidecars", "upload-session-leases"}
_BACKUPS = ("campaign-history-before-reset-", "retained-confirmed-before-reset-")


class CampaignResetError(ValueError):
    def __init__(self, code: str, *, partial: bool = False):
        self.code = code
        self.partial = partial
        super().__init__(
            "A limpeza local não terminou. Feche operações que usam os arquivos e tente Reset completo novamente."
            if partial else
            "Não foi possível limpar o estado local. Confira o acesso aos arquivos e tente Reset completo novamente.")


def _roots() -> list[Path]:
    # Resolve the configured roots, never a journal-supplied media path.
    return sorted({Path(config.DATA_DIR).resolve(), Path(config.MEDIA_DATA_DIR).resolve()}, key=str)


def pending() -> bool:
    return any((directory / _STAGING).exists()
               for root in _roots() for directory in (root, root.parent / "backups"))


def _is_link(path: Path) -> bool:
    value = path.lstat()
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _tree_files(path: Path, allowed: Path) -> list[Path]:
    # Preflight the entire deletion tree. Junctions and symlinks never expand
    # the scope to another installation or to a referenced video.
    resolved = path.resolve()
    if resolved == allowed.resolve() or not resolved.is_relative_to(allowed.resolve()):
        raise CampaignResetError("campaign_reset_scope")
    if _is_link(path):
        raise CampaignResetError("campaign_reset_link")
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise CampaignResetError("campaign_reset_scope")
    files = []
    for entry in path.iterdir():
        files.extend(_tree_files(entry, allowed))
    return files


def _history_name(name: str) -> bool:
    lowered = name.casefold()
    plain = lowered.removeprefix(".")
    # Custom history stems may contain dots. A media marker such as
    # campaign_demo.mp4.source.json belongs to the Library, not this reset.
    if is_library_marker_name(plain):
        return False
    history = re.fullmatch(r"campaign_.+\.(?:json|log)(?:\.(?:backup[^/]*|bak|old)|(?:\.[^.]+)*\.tmp)?", plain)
    return (bool(history)
            or any(lowered == value or lowered.startswith(value + ".")
                   or lowered.startswith("." + value + ".") for value in _FILES)
            or plain.startswith("task_rank_cache") and ".pkl" in plain)


def _targets() -> list[tuple[Path, Path]]:
    targets = []
    for root in _roots():
        if root.exists():
            if not root.is_dir():
                raise CampaignResetError("campaign_reset_scope")
            for path in root.iterdir():
                if path.name in _DIRECTORIES or _history_name(path.name) or path.name == _STAGING:
                    targets.append((path, root))
        backups = root.parent / "backups"
        if backups.exists():
            if _is_link(backups) or not backups.is_dir():
                raise CampaignResetError("campaign_reset_link")
            for path in backups.iterdir():
                if path.name.startswith(_BACKUPS) or path.name == _STAGING:
                    targets.append((path, backups))
    # ROOT and MEDIA_DATA_DIR may point at the same installation/backup tree.
    return list({os.path.normcase(str(path)): (path, allowed) for path, allowed in targets}.values())


def _remove_tree(path: Path) -> None:
    def repair(function, name, _error):
        # Existing read-only history copies can still be explicitly erased.
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)
    if path.is_dir():
        shutil.rmtree(path, onerror=repair)
    else:
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink()


def erase_local_campaigns() -> dict:
    from .campaign_state import campaign_state_lease
    with campaign_state_lease(exclusive=True):
        return _erase_local_campaigns()


def _erase_local_campaigns() -> dict:
    """Erase all selected state bytes, or report a retryable incomplete reset.

    Validation accepts even corrupt histories: explicit reset is the escape
    hatch for damaged operational state, not a decoder or network operation.
    All moves are within the source filesystem. A failed staging move is rolled
    back; a failed purge retains a detectable barrier and never reports success.
    """
    try:
        targets = _targets()
        protected = {Path(config.MEDIA_DATA_DIR).resolve(), Path(config.SECRETS_DIR).resolve()}
        protected.update(root / name for root in _roots() for name in ("device_state", "device_anchors"))
        # A configurable Library may be nested beneath a nominal state folder.
        # Reject that overlap rather than deleting its source video recursively.
        if any(any(value.is_relative_to(path.resolve()) for value in protected)
               for path, _allowed in targets):
            raise CampaignResetError("campaign_reset_protected_scope")
        files = [file for path, allowed in targets for file in _tree_files(path, allowed)]
        size = sum(file.stat().st_size for file in files)
    except CampaignResetError:
        raise
    except OSError:
        raise CampaignResetError("campaign_reset_preflight") from None

    staging_dirs = [path for path, _allowed in targets if path.name == _STAGING]
    created_staging = []
    moved = []
    try:
        # Keep any previous interruption barrier until every current target
        # has been staged. A failed retry must never reopen a partial history.
        batch = uuid.uuid4().hex
        for index, (path, allowed) in enumerate(targets):
            if path.name == _STAGING:
                continue
            staging = allowed / _STAGING
            if staging not in staging_dirs:
                staging.mkdir(exist_ok=False)
                staging_dirs.append(staging)
                created_staging.append(staging)
            destination = staging / f"{batch}-{index}"
            path.rename(destination)
            moved.append((path, destination))
    except OSError:
        rollback_failed = False
        for original, staged in reversed(moved):
            try:
                staged.rename(original)
            except OSError:
                rollback_failed = True
        for staging in created_staging:
            try:
                staging.rmdir()
            except OSError:
                rollback_failed = True
        raise CampaignResetError("campaign_reset_move", partial=rollback_failed or pending()) from None

    try:
        for staging in staging_dirs:
            _remove_tree(staging)
        remaining = _targets()
        if remaining:
            raise CampaignResetError("campaign_reset_remaining", partial=True)
    except OSError:
        raise CampaignResetError("campaign_reset_purge", partial=True) from None
    return {"removed_files": len(files), "removed_bytes": size}
