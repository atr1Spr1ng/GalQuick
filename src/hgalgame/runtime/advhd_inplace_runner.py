from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class AdvHdInPlaceReplayRunner:
    """Temporarily swap Rio.arc, run AdvHD, and always restore the original.

    The original archive backup is created and verified by the replay builder before
    this runner is ever invoked.  Every replacement is written to a temporary file
    in the game directory and committed with ``os.replace`` so the engine sees
    either the complete original archive or the complete generated archive.
    """

    ACTIVE_STATES = {
        "swap_pending",
        "swapped",
        "running",
        "restore_pending",
        "restore_failed",
    }

    def __init__(
        self,
        process_launcher: Callable[[Path, Path], int] | None = None,
    ) -> None:
        self._process_launcher = process_launcher or self._launch_and_wait

    def run(self, manifest_path: Path) -> dict[str, Any]:
        paths, manifest = self._load(manifest_path)
        with self._session_lock(paths["lock"]):
            self._verify_artifacts(paths, manifest)
            companions = self._companions(paths, manifest)
            for companion in companions:
                AdvHdInPlaceReplayRunner().recover(companion)
            recovered = self._make_original_current(paths, manifest)
            self._write_state(
                paths["state"], manifest, "swap_pending", recovered=recovered
            )
            exit_code: int | None = None
            launch_error: BaseException | None = None
            try:
                self._replace_verified(
                    paths["generated"],
                    paths["game_archive"],
                    manifest["generated_sha256"],
                )
                self._write_state(paths["state"], manifest, "swapped")
                self._write_state(paths["state"], manifest, "running")
                def launch(index: int) -> int:
                    if index == len(companions):
                        return int(self._process_launcher(paths["executable"], paths["game_dir"]))
                    runner = AdvHdInPlaceReplayRunner(lambda _exe, _cwd: launch(index + 1))
                    return int(runner.run(companions[index])["game_exit_code"])
                exit_code = launch(0)
            except BaseException as exc:
                launch_error = exc
            finally:
                try:
                    self._restore_original(paths, manifest, exit_code=exit_code)
                except BaseException as restore_error:
                    self._write_state_best_effort(
                        paths["state"],
                        manifest,
                        "restore_failed",
                        error=str(restore_error),
                        backup=str(paths["backup"]),
                    )
                    raise ValueError(
                        "failed to restore the original Rio.arc; the verified backup "
                        f"is still available at {paths['backup']}: {restore_error}"
                    ) from restore_error

            if launch_error is not None:
                raise ValueError(
                    f"AdvHD failed to start or exited unexpectedly: {launch_error}"
                ) from launch_error
            return {
                "restored": True,
                "game_exit_code": exit_code,
                "game_archive": str(paths["game_archive"]),
                "backup_archive": str(paths["backup"]),
                "source_sha256": manifest["source_sha256"],
            }

    def recover(self, manifest_path: Path) -> dict[str, Any]:
        paths, manifest = self._load(manifest_path)
        with self._session_lock(paths["lock"]):
            for companion in self._companions(paths, manifest):
                AdvHdInPlaceReplayRunner().recover(companion)
            self._verify_artifacts(
                paths, manifest, require_generated=False, require_executable=False
            )
            before = self._archive_status(paths["game_archive"], manifest)
            self._make_original_current(paths, manifest)
            self._write_state(paths["state"], manifest, "restored", recovered=True)
            return {
                "restored": True,
                "previous_status": before,
                "game_archive": str(paths["game_archive"]),
                "backup_archive": str(paths["backup"]),
                "source_sha256": manifest["source_sha256"],
            }

    def validate(self, manifest_path: Path) -> dict[str, Any]:
        paths, manifest = self._load(manifest_path)
        self._verify_artifacts(paths, manifest)
        for companion in self._companions(paths, manifest):
            AdvHdInPlaceReplayRunner().validate(companion)
        status = self._archive_status(paths["game_archive"], manifest)
        if status == "unknown":
            raise ValueError(
                "the current game Rio.arc matches neither the recorded original nor "
                "the generated replay archive"
            )
        return {
            "valid": True,
            "runtime_mode": "in-place",
            "current_archive_status": status,
            "game_archive": str(paths["game_archive"]),
            "backup_archive": str(paths["backup"]),
            "generated_archive": str(paths["generated"]),
            "source_sha256": manifest["source_sha256"],
            "generated_sha256": manifest["generated_sha256"],
        }

    def _companions(self, paths: dict[str, Path], manifest: dict[str, Any]) -> list[Path]:
        result = []
        archives = {paths['game_archive']}
        for value in manifest.get('companion_manifests', []):
            companion = (paths['manifest'].parent / value).resolve()
            if companion.parent != paths['manifest'].parent or companion == paths['manifest']:
                raise ValueError('companion manifest must be a sibling file')
            child_paths, child = self._load(companion)
            if child.get('companion_manifests') or child_paths['game_dir'] != paths['game_dir']:
                raise ValueError('nested or foreign companion manifest')
            if child_paths['game_archive'] in archives or child_paths['lock'] == paths['lock']:
                raise ValueError('companion archive or lock collision')
            archives.add(child_paths['game_archive'])
            result.append(companion)
        return result

    def _load(self, manifest_path: Path) -> tuple[dict[str, Path], dict[str, Any]]:
        manifest_path = manifest_path.resolve()
        if manifest_path.is_dir():
            manifest_path = manifest_path / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"in-place replay manifest not found: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("runtime_mode") != "in-place":
            raise ValueError("manifest is not for an AdvHD in-place replay")

        base = manifest_path.parent
        game_dir = Path(manifest["game_dir"]).resolve()
        archive_name = str(manifest.get("archive_name", "Rio.arc"))
        game_archive = (game_dir / archive_name).resolve()
        if game_archive.parent != game_dir:
            raise ValueError("archive_name must refer to a file directly in game_dir")
        paths = {
            "manifest": manifest_path,
            "game_dir": game_dir,
            "game_archive": game_archive,
            "generated": self._resolve_relative(base, manifest["generated_archive"]),
            "backup": self._resolve_relative(base, manifest["backup_archive"]),
            "state": self._resolve_relative(
                base, manifest.get("state_file", "in_place_state.json")
            ),
            "lock": self._resolve_relative(
                base, manifest.get("lock_file", "in_place_session.lock")
            ),
            "executable": (game_dir / manifest.get("executable", "AdvHD.exe")).resolve(),
        }
        if manifest.get("story_archive"):
            paths["story_archive"] = (
                game_dir / str(manifest["story_archive"])
            ).resolve()
        if paths["generated"] == game_archive or paths["backup"] == game_archive:
            raise ValueError("generated archive and backup must be outside game_dir")
        return paths, manifest

    def _resolve_relative(self, base: Path, value: str) -> Path:
        path = Path(value)
        return (base / path).resolve() if not path.is_absolute() else path.resolve()

    def _verify_artifacts(
        self,
        paths: dict[str, Path],
        manifest: dict[str, Any],
        *,
        require_generated: bool = True,
        require_executable: bool = True,
    ) -> None:
        if not paths["game_dir"].is_dir():
            raise NotADirectoryError(f"game directory not found: {paths['game_dir']}")
        if require_executable and not paths["executable"].is_file():
            raise FileNotFoundError(f"AdvHD executable not found: {paths['executable']}")
        if require_executable and manifest.get("executable_sha256"):
            self._require_hash(
                paths["executable"],
                manifest["executable_sha256"],
                "AdvHD executable",
            )
        if (
            require_executable
            and "story_archive" in paths
            and manifest.get("story_archive_sha256")
        ):
            self._require_hash(
                paths["story_archive"],
                manifest["story_archive_sha256"],
                "story archive",
            )
        if require_generated:
            self._require_hash(
                paths["generated"],
                manifest["generated_sha256"],
                "generated Rio.arc",
            )
        self._require_hash(paths["backup"], manifest["source_sha256"], "backup Rio.arc")

    def _make_original_current(
        self, paths: dict[str, Path], manifest: dict[str, Any]
    ) -> bool:
        status = self._archive_status(paths["game_archive"], manifest)
        if status == "original":
            return False
        if status == "unknown" and not self._state_is_active(paths["state"]):
            raise ValueError(
                "the game Rio.arc changed outside this replay session; refusing to "
                "overwrite it. Rebuild the replay package for the current game files."
            )
        self._restore_original(paths, manifest, recovered=True)
        return True

    def _restore_original(
        self,
        paths: dict[str, Path],
        manifest: dict[str, Any],
        *,
        exit_code: int | None = None,
        recovered: bool = False,
    ) -> None:
        self._write_state_best_effort(
            paths["state"],
            manifest,
            "restore_pending",
            exit_code=exit_code,
            recovered=recovered,
        )
        status = self._archive_status(paths["game_archive"], manifest)
        preserved: str | None = None
        if status == "unknown":
            preserved_path = self._preserve_unexpected(paths["game_archive"], paths["state"])
            preserved = str(preserved_path)
        if status != "original":
            self._replace_verified(
                paths["backup"],
                paths["game_archive"],
                manifest["source_sha256"],
            )
        self._restore_source_metadata(paths["game_archive"], paths["backup"], manifest)
        self._require_hash(
            paths["game_archive"], manifest["source_sha256"], "restored Rio.arc"
        )
        self._write_state_best_effort(
            paths["state"],
            manifest,
            "restored",
            exit_code=exit_code,
            recovered=recovered,
            preserved_unexpected=preserved,
        )

    def _archive_status(self, path: Path, manifest: dict[str, Any]) -> str:
        if not path.is_file():
            return "missing"
        digest = self._sha256(path)
        if digest == manifest["source_sha256"]:
            return "original"
        if digest == manifest["generated_sha256"]:
            return "generated"
        return "unknown"

    def _state_is_active(self, state_path: Path) -> bool:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        return state.get("status") in self.ACTIVE_STATES

    def _preserve_unexpected(self, source: Path, state_path: Path) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = state_path.parent / f"unexpected_Rio.{stamp}.preserved.arc"
        suffix = 1
        while destination.exists():
            destination = state_path.parent / (
                f"unexpected_Rio.{stamp}.{suffix}.preserved.arc"
            )
            suffix += 1
        self._copy_atomic(source, destination)
        return destination

    def _replace_verified(self, source: Path, destination: Path, digest: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.swap.tmp")
        temporary.unlink(missing_ok=True)
        previous_metadata = (
            self._capture_metadata(destination) if destination.is_file() else None
        )
        try:
            self._copy_atomic(source, temporary)
            self._require_hash(temporary, digest, "temporary Rio.arc")
            self._make_replaceable(destination)
            os.replace(temporary, destination)
            self._require_hash(destination, digest, "installed Rio.arc")
        except BaseException:
            if previous_metadata is not None and destination.is_file():
                self._apply_metadata(destination, previous_metadata)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def _capture_metadata(self, path: Path) -> dict[str, int]:
        value = path.stat()
        metadata = {
            "mtime_ns": value.st_mtime_ns,
            "mode": stat.S_IMODE(value.st_mode),
        }
        attributes = getattr(value, "st_file_attributes", None)
        if attributes is not None:
            metadata["windows_file_attributes"] = int(attributes)
        return metadata

    def _restore_source_metadata(
        self, destination: Path, backup: Path, manifest: dict[str, Any]
    ) -> None:
        metadata = manifest.get("source_file_metadata")
        if not isinstance(metadata, dict):
            metadata = self._capture_metadata(backup)
        if "size" in metadata and destination.stat().st_size != int(metadata["size"]):
            raise ValueError("restored Rio.arc size does not match recorded source metadata")
        self._apply_metadata(destination, metadata)

    def _apply_metadata(self, path: Path, metadata: dict[str, Any]) -> None:
        self._make_replaceable(path)
        if "mtime_ns" in metadata:
            current = path.stat()
            os.utime(
                path,
                ns=(current.st_atime_ns, int(metadata["mtime_ns"])),
            )
        if sys.platform == "win32" and "windows_file_attributes" in metadata:
            self._set_windows_attributes(
                path, int(metadata["windows_file_attributes"])
            )
        elif "mode" in metadata:
            os.chmod(path, int(metadata["mode"]))

    def _make_replaceable(self, path: Path) -> None:
        if not path.exists():
            return
        if sys.platform == "win32":
            attributes = int(
                getattr(path.stat(), "st_file_attributes", 0)
            )
            read_only = 0x1
            if attributes & read_only:
                self._set_windows_attributes(path, attributes & ~read_only)
        else:
            os.chmod(path, path.stat().st_mode | stat.S_IWUSR)

    def _set_windows_attributes(self, path: Path, attributes: int) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.SetFileAttributesW.restype = wintypes.BOOL
        if not kernel32.SetFileAttributesW(str(path), attributes):
            raise ctypes.WinError(ctypes.get_last_error())

    def _copy_atomic(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.copy.tmp")
        temporary.unlink(missing_ok=True)
        try:
            with source.open("rb") as reader, temporary.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _require_hash(self, path: Path, expected: str, label: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
        actual = self._sha256(path)
        if actual != expected:
            raise ValueError(
                f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
            )

    def _write_state(
        self,
        path: Path,
        manifest: dict[str, Any],
        status: str,
        **extra: Any,
    ) -> None:
        payload = {
            "format_version": "advhd-in-place-state-v1",
            "status": status,
            "pid": os.getpid(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "game_dir": manifest["game_dir"],
            "source_sha256": manifest["source_sha256"],
            "generated_sha256": manifest["generated_sha256"],
            **extra,
        }
        self._write_json_atomic(path, payload)

    def _write_state_best_effort(
        self,
        path: Path,
        manifest: dict[str, Any],
        status: str,
        **extra: Any,
    ) -> None:
        try:
            self._write_state(path, manifest, status, **extra)
        except OSError:
            pass

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _session_lock(self, path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                owner = self._lock_owner(path)
                if attempt == 0 and owner is not None and not self._pid_is_alive(owner):
                    path.unlink(missing_ok=True)
                    continue
                raise ValueError(
                    "another replay session may still be active; if it is not, run "
                    f"the recovery launcher: {path}"
                )
        else:  # pragma: no cover - loop always exits or raises
            raise ValueError(f"could not acquire replay session lock: {path}")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            path.unlink(missing_ok=True)

    def _lock_owner(self, path: Path) -> int | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("pid")
            return int(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _pid_is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            # On Windows os.kill(pid, 0) is not the harmless existence probe it is
            # on POSIX, so query the process handle without sending any signal.
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _launch_and_wait(self, executable: Path, game_dir: Path) -> int:
        process = subprocess.Popen([str(executable)], cwd=game_dir)
        return int(process.wait())

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
