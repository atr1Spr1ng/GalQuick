from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hgalgame.runtime.advhd_inplace_runner import AdvHdInPlaceReplayRunner
from hgalgame.runtime.advhd_bridge import AdvHdBridge
from hgalgame.runtime.advhd_session_builder import AdvHdSceneSessionBuilder
from hgalgame.runtime.windows import WindowsWindowController


class AdvHdSceneSession:
    """Own one AdvHD process for an entire novel/Scene Review session."""

    VISUAL_POLL_SECONDS = 0.25
    VISUAL_PAINT_SECONDS = 0.18
    VISUAL_DEPART_THRESHOLD = 0.16
    VISUAL_RETURN_THRESHOLD = 0.07
    VISUAL_STABLE_SAMPLES = 4

    def __init__(
        self,
        game_dir: Path,
        output_dir: Path,
        *,
        builder: AdvHdSceneSessionBuilder | None = None,
        window_controller: WindowsWindowController | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.game_dir = game_dir.resolve()
        self.output_dir = output_dir.resolve()
        self.builder = builder or AdvHdSceneSessionBuilder()
        self.windows = window_controller or WindowsWindowController()
        self._popen = popen or subprocess.Popen
        self._lock = threading.RLock()
        self._launched = threading.Event()
        self._completed = threading.Event()
        self._worker: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._report: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._phase = "new"
        self._last_scene: int | None = None
        self._dispatch_count = 0
        self._last_controller_path: tuple[int, int] | None = None
        self._monitor_generation = 0
        self._completion_detection = "inactive"
        self._completion_count = 0
        self._visual_difference: float | None = None
        self._visual_stable_samples = 0
        self._visual_sample_count = 0
        self._bridge: AdvHdBridge | None = None

    @property
    def pid(self) -> int | None:
        with self._lock:
            process = self._process
            return int(process.pid) if process and process.poll() is None else None

    def start(self, timeout: float = 20.0) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return self.status()
            self._phase = "preparing"
            self._error = None
            self._result = None
            self._last_scene = None
            self._dispatch_count = 0
            self._last_controller_path = None
            self._monitor_generation += 1
            self._completion_detection = "inactive"
            self._completion_count = 0
            self._launched.clear()
            self._completed.clear()
        report = self.builder.build(self.game_dir, self.output_dir)
        self.builder.validate(self.output_dir / "manifest.json")
        with self._lock:
            self._report = report
            token = report['controller'].get('bridge_token')
            self._bridge = AdvHdBridge(self.game_dir, token) if token else None
            self._phase = "starting"
            self._worker = threading.Thread(
                target=self._run_worker,
                name="advhd-scene-session",
                daemon=True,
            )
            self._worker.start()
        if not self._launched.wait(timeout):
            self.stop()
            raise ValueError("AdvHD did not start within the Scene session timeout")
        with self._lock:
            error = self._error
            process = self._process
        if error:
            raise ValueError(error)
        if process is None or process.poll() is not None:
            raise ValueError("AdvHD exited before the Scene session became ready")
        self._prepare_game_window(process.pid, timeout=max(300.0, timeout))
        return self.status()

    def perform(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        if action != "scene_replay":
            raise ValueError(f"AdvHD Scene session does not support action: {action}")
        try:
            replay_id = int(parameters["replay_key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("scene_replay requires an integer replay_key") from exc
        with self._lock:
            report = self._report
            process = self._process
        if report is None or process is None or process.poll() is not None:
            raise ValueError("the persistent AdvHD Scene session is not running")
        available = tuple(int(value) for value in report["available_replay_ids"])
        if replay_id not in set(available):
            raise ValueError(f"replay id {replay_id} is unavailable")
        page_size = int(report["controller"]["page_size"])
        position = available.index(replay_id)
        page_index, item_index = divmod(position, page_size)
        if self._bridge is not None:
            return self._perform_bridge(replay_id, page_index, item_index, process)
        hwnd = self._wait_for_window(process.pid, timeout=5.0)
        if hwnd is None:
            raise ValueError("could not find the running AdvHD game window")
        signature_method = getattr(self.windows, "visual_signature", None)
        controller_signature = None
        if callable(signature_method):
            # The overlay has just collapsed. Give Windows one paint cycle so
            # its former reader body is not included in the controller sample.
            time.sleep(self.VISUAL_PAINT_SECONDS)
            try:
                controller_signature = signature_method(hwnd)
            except (OSError, ValueError):
                controller_signature = None
        # Root item zero is a no-op guard against lingering fast-forward.
        # Actual Scene page choices therefore start at one.
        self.windows.send_choice_path(hwnd, page_index + 1, item_index)
        with self._lock:
            self._phase = "scene_dispatched"
            self._last_scene = replay_id
            self._dispatch_count += 1
            self._last_controller_path = (page_index, item_index)
            self._monitor_generation += 1
            generation = self._monitor_generation
            self._completion_detection = (
                "waiting_for_departure"
                if controller_signature is not None
                else "unavailable"
            )
        if controller_signature is not None:
            threading.Thread(
                target=self._monitor_scene_completion,
                args=(hwnd, replay_id, controller_signature, generation),
                name=f"advhd-scene-completion-{replay_id}",
                daemon=True,
            ).start()
        return {
            "action": action,
            "replay_key": replay_id,
            "persistent": True,
            "game_pid": process.pid,
            "controller_page": page_index,
            "controller_item": item_index,
            "completion_detection": self._completion_detection,
        }

    def reader_resumed(self) -> dict[str, Any]:
        with self._lock:
            self._monitor_generation += 1
            if self._phase in {"scene_dispatched", "scene_complete"}:
                self._phase = "idle"
            self._completion_detection = "inactive"
        return self.status()

    def _perform_bridge(self, replay_id, page_index, item_index, process) -> dict[str, Any]:
        bridge = self._bridge
        assert bridge is not None
        with self._lock:
            if self._phase not in {'idle', 'scene_complete'}:
                raise ValueError('AdvHD is already dispatching or playing a scene')
            self._phase = 'dispatching'
        try:
            alive = lambda: process.poll() is None and not self._completed.is_set()
            bridge.select('root', page_index + 1, alive=alive)
            acknowledgement = bridge.select(f'page_{page_index}', item_index, alive=alive)
        except Exception as exc:
            with self._lock:
                self._phase = 'failed'
                self._error = str(exc)
                self._completion_detection = 'bridge_error'
            raise
        with self._lock:
            self._phase = 'scene_dispatched'
            self._last_scene = replay_id
            self._dispatch_count += 1
            self._last_controller_path = (page_index, item_index)
            self._monitor_generation += 1
            generation = self._monitor_generation
            self._completion_detection = 'waiting_for_script_return'
        threading.Thread(target=self._monitor_bridge_completion,
                         args=(generation, acknowledgement['sequence']),
                         name='advhd-bridge-completion', daemon=True).start()
        return dict(action='scene_replay', replay_key=replay_id, persistent=True,
                    game_pid=process.pid, controller_page=page_index, controller_item=item_index,
                    completion_detection='waiting_for_script_return')

    def _monitor_bridge_completion(self, generation: int, sequence: int) -> None:
        while not self._completed.wait(.1):
            with self._lock:
                if self._monitor_generation != generation or self._phase != 'scene_dispatched':
                    return
                status = self._bridge.status() if self._bridge else None
                if status and status['state'] == 'error':
                    self._phase = 'failed'
                    self._error = 'AdvHD script bridge reported an error'
                    self._completion_detection = 'bridge_error'
                    return
                if (status and status['state'] == 'ready' and status['page'] == 'root'
                        and status['sequence'] == sequence):
                    self._phase = 'scene_complete'
                    self._completion_detection = 'script_return'
                    self._completion_count += 1
                    return

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = bool(process and process.poll() is None)
            return {
                "kind": "persistent_scene_session",
                "running": running,
                "phase": self._phase,
                "pid": int(process.pid) if running else None,
                "last_scene": self._last_scene,
                "dispatch_count": self._dispatch_count,
                "completion_count": self._completion_count,
                "visual_difference": self._visual_difference,
                "visual_stable_samples": self._visual_stable_samples,
                "visual_sample_count": self._visual_sample_count,
                "completion_detection": self._completion_detection,
                "bridge_status": self._bridge.status() if self._bridge else None,
                "last_controller_path": (
                    list(self._last_controller_path)
                    if self._last_controller_path
                    else None
                ),
                "error": self._error,
                "restored": bool(self._result and self._result.get("restored")),
                "exit_code": self._result.get("game_exit_code") if self._result else None,
            }

    def stop(self, timeout: float = 8.0) -> dict[str, Any]:
        with self._lock:
            process = self._process
            worker = self._worker
            if self._phase not in {"stopped", "failed"}:
                self._phase = "stopping"
            self._monitor_generation += 1
        if process and process.poll() is None:
            hwnd = self.windows.find_main_window(process.pid)
            if hwnd:
                self.windows.close(hwnd)
            try:
                process.wait(timeout=min(timeout, 4.0))
            except subprocess.TimeoutExpired:
                process.terminate()
        if worker and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        if process and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if worker and worker is not threading.current_thread():
                worker.join(timeout=3)
        return self.status()

    def _monitor_scene_completion(
        self,
        hwnd: int,
        replay_id: int,
        controller_signature: tuple[int, ...],
        generation: int,
    ) -> None:
        """Detect a stable return to our generated controller screen."""

        departed = False
        stable_samples = 0
        signature_method = getattr(self.windows, "visual_signature", None)
        if not callable(signature_method):
            return
        while not self._completed.wait(self.VISUAL_POLL_SECONDS):
            with self._lock:
                process = self._process
                current_generation = self._monitor_generation
                phase = self._phase
            if (
                current_generation != generation
                or phase != "scene_dispatched"
                or process is None
                or process.poll() is not None
            ):
                return
            try:
                current = signature_method(hwnd)
            except (OSError, ValueError):
                current = None
            if current is None:
                stable_samples = 0
                continue
            difference = self._signature_difference(controller_signature, current)
            if difference is None:
                stable_samples = 0
                with self._lock:
                    self._visual_difference = None
                    self._visual_stable_samples = 0
                continue
            with self._lock:
                self._visual_difference = difference
                self._visual_sample_count += 1
            if not departed:
                if difference >= self.VISUAL_DEPART_THRESHOLD:
                    departed = True
                    with self._lock:
                        if self._monitor_generation == generation:
                            self._completion_detection = "waiting_for_return"
                continue
            if difference <= self.VISUAL_RETURN_THRESHOLD:
                stable_samples += 1
            else:
                stable_samples = 0
            with self._lock:
                self._visual_stable_samples = stable_samples
            if stable_samples < self.VISUAL_STABLE_SAMPLES:
                continue
            with self._lock:
                if (
                    self._monitor_generation != generation
                    or self._phase != "scene_dispatched"
                    or self._last_scene != replay_id
                ):
                    return
                self._phase = "scene_complete"
                self._completion_detection = "visual_return_stable"
                self._completion_count += 1
            return

    @staticmethod
    def _signature_difference(
        first: tuple[int, ...],
        second: tuple[int, ...],
    ) -> float | None:
        """Compare only mutually visible points; never infer from an overlay."""

        if not first or len(first) != len(second):
            return None
        pairs = [(before, after) for before, after in zip(first, second, strict=True)
                 if before >= 0 and after >= 0]
        if len(pairs) < max(1, (len(first) + 4) // 5):
            return None  # Less than 20% mutually visible: insufficient evidence.
        changed = 0
        for before, after in pairs:
            red = abs((before & 0xFF) - (after & 0xFF))
            green = abs(((before >> 8) & 0xFF) - ((after >> 8) & 0xFF))
            blue = abs(((before >> 16) & 0xFF) - ((after >> 16) & 0xFF))
            if red + green + blue >= 54:
                changed += 1
        return changed / len(pairs)

    def _run_worker(self) -> None:
        try:
            runner = AdvHdInPlaceReplayRunner(self._launch_and_wait)
            result = runner.run(self.output_dir / "manifest.json")
            with self._lock:
                self._result = result
                exit_code = int(result.get("game_exit_code") or 0)
                if exit_code:
                    self._phase = "failed"
                    self._error = (
                        f"AdvHD exited abnormally: 0x{exit_code & 0xFFFFFFFF:08X}; "
                        f"original archive restored: {bool(result.get('restored'))}"
                    )
                else:
                    self._phase = "stopped"
        except BaseException as exc:
            with self._lock:
                self._error = f"persistent AdvHD session failed: {exc}"
                self._phase = "failed"
        finally:
            self._launched.set()
            self._completed.set()

    def _launch_and_wait(self, executable: Path, game_dir: Path) -> int:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = self._popen(
            [str(executable)],
            cwd=str(game_dir),
            creationflags=creationflags,
        )
        with self._lock:
            self._process = process
            self._phase = "idle"
        self._launched.set()
        return int(process.wait())

    def _wait_for_window(self, pid: int, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hwnd = self.windows.find_main_window(pid)
            if hwnd:
                return hwnd
            if self._completed.wait(0.08):
                break
        return None

    def _prepare_game_window(self, pid: int, timeout: float) -> int:
        """Leave AdvHD's output/settings dialog to the user, then await renderer."""

        deadline = time.monotonic() + timeout
        settings_prompt_seen = False
        while time.monotonic() < deadline:
            hwnd = self.windows.find_main_window(pid)
            if hwnd:
                title = self.windows.window_title(hwnd).casefold()
                rect = self.windows.client_rect_on_screen(hwnd)
                looks_like_settings = title.endswith("-settings") or bool(
                    rect and rect.width < 640 and rect.height < 640
                )
                if looks_like_settings:
                    if not settings_prompt_seen:
                        settings_prompt_seen = True
                        with self._lock:
                            self._phase = "waiting_for_output_selection"
                    if self._completed.wait(0.08):
                        break
                    continue
                if not looks_like_settings and rect and rect.width >= 640:
                    with self._lock:
                        self._phase = "idle"
                    return hwnd
            if self._completed.wait(0.08):
                break
        raise ValueError(
            "AdvHD renderer did not start; choose the output device in the "
            "settings window and click OK"
        )
