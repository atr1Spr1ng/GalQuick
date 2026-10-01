from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from hgalgame.engines.advhd.archive import AdvHdArcReader
from hgalgame.engines.advhd.archive_writer import AdvHdArcWriter
from hgalgame.engines.advhd.profile import AdvHdReplayProfile, AdvHdReplayProfiler
from hgalgame.engines.advhd.ws2_encoder import Ws2Encoder
from hgalgame.engines.advhd.ws2_parser import Ws2Parser
from hgalgame.output import write_json
from hgalgame.runtime.advhd_inplace_runner import AdvHdInPlaceReplayRunner


class AdvHdContinuousReplayBuilder:
    """Build a verified in-place replay package for the original AdvHD runtime."""

    GENERATED_ARCHIVE = "Rio.arc"

    def __init__(self) -> None:
        self.reader = AdvHdArcReader()
        self.writer = AdvHdArcWriter()
        self.parser = Ws2Parser()
        self.encoder = Ws2Encoder()
        self.profiler = AdvHdReplayProfiler()

    def build(
        self,
        game_dir: Path,
        output_dir: Path,
        playlist: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        """Build a tiny package that swaps only Rio.arc while the game runs."""

        game_dir = game_dir.resolve()
        output_dir = output_dir.resolve()
        if output_dir == game_dir or game_dir in output_dir.parents:
            raise ValueError("output_dir must be outside the original game directory")
        if not game_dir.is_dir():
            raise NotADirectoryError(f"game directory not found: {game_dir}")
        existing_manifest = output_dir / "manifest.json"
        if existing_manifest.is_file():
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if existing.get("runtime_mode") != "in-place":
                raise ValueError(
                    "output_dir already contains a non-in-place replay package"
                )
            if Path(existing.get("game_dir", "")).resolve() != game_dir:
                raise ValueError(
                    "output_dir belongs to a different game directory; choose another output"
                )
            # A previous process may have been terminated while the generated archive
            # was installed. Restore it before using the game archive as source input.
            AdvHdInPlaceReplayRunner().recover(existing_manifest)

        profile = self.profiler.build(game_dir)
        available_ids = tuple(profile.replay_ids)
        selected_ids = self._select_playlist(playlist, available_ids)
        executable = game_dir / profile.executable
        system_archive = game_dir / profile.system_archive
        source_sha256 = self._sha256(system_archive)
        source_metadata = self._file_metadata(system_archive)
        if source_sha256 != profile.system_archive_sha256:
            raise ValueError("system archive changed while its AdvHD profile was built")
        manifest = self.reader.read_manifest(system_archive)
        start_name = profile.start_script
        evret_name = profile.replay_return_script
        replacements = {
            start_name: self.encoder.continuous_replay_start(
                self.reader.read_entry(system_archive, start_name),
                first_replay_id=selected_ids[0],
                selector_variable=profile.selector_variable,
                scene_mode_variable=profile.scene_mode_variable,
                scene_mode_active_value=profile.scene_mode_active_value,
                event_mode_variable=profile.event_mode_variable,
                event_mode_inactive_value=profile.event_mode_inactive_value,
                replay_target=profile.replay_dispatch_target,
                menu_target=profile.main_menu_target,
            ),
            evret_name: self.encoder.continuous_replay_return(
                self.reader.read_entry(system_archive, evret_name),
                selected_ids,
                script_name=evret_name,
                selector_variable=profile.selector_variable,
                scene_mode_variable=profile.scene_mode_variable,
                scene_mode_inactive_value=profile.scene_mode_inactive_value,
                event_mode_variable=profile.event_mode_variable,
                event_mode_inactive_value=profile.event_mode_inactive_value,
                replay_target=profile.replay_dispatch_target,
                menu_target=profile.main_menu_target,
            ),
        }
        archive_payload = self.writer.rebuild(system_archive, replacements)
        generated_sha256 = hashlib.sha256(archive_payload).hexdigest()
        generated_archive = self.writer.write_atomic(
            output_dir / "generated" / self.GENERATED_ARCHIVE, archive_payload
        )

        backup_archive = (
            output_dir
            / "backup"
            / f"{system_archive.stem}.original.{source_sha256[:16]}{system_archive.suffix}"
        )
        if backup_archive.exists():
            if self._sha256(backup_archive) != source_sha256:
                raise ValueError(
                    f"existing original backup failed SHA-256 verification: {backup_archive}"
                )
        else:
            self._copy_atomic(system_archive, backup_archive)
        if self._sha256(backup_archive) != source_sha256:
            raise ValueError("original Rio.arc backup failed SHA-256 verification")

        report: dict[str, Any] = {
            "format_version": "advhd-continuous-replay-in-place-v2",
            "runtime_mode": "in-place",
            "game_dir": str(game_dir),
            "archive_name": system_archive.name,
            "executable": executable.name,
            "executable_sha256": profile.executable_sha256,
            "story_archive": profile.story_archive,
            "story_archive_sha256": profile.story_archive_sha256,
            "source_archive": str(system_archive),
            "source_sha256": source_sha256,
            "source_file_metadata": source_metadata,
            "generated_archive": str(generated_archive.relative_to(output_dir)),
            "generated_sha256": generated_sha256,
            "backup_archive": str(backup_archive.relative_to(output_dir)),
            "state_file": "in_place_state.json",
            "lock_file": "in_place_session.lock",
            "profile": profile.to_dict(),
            "profile_file": "advhd_profile.json",
            "available_scene_count": len(available_ids),
            "available_replay_ids": list(available_ids),
            "scene_count": len(selected_ids),
            "playlist": list(selected_ids),
            "sequence": [
                item
                for item in profile.replay_dispatches
                if int(item["replay_id"]) in set(selected_ids)
            ],
            "replaced_entries": [start_name, evret_name],
            "preserved_entry_count": manifest.entry_count - len(replacements),
            "game_directory_write_scope": [system_archive.name],
            "restore_guarantees": [
                "verify the original backup before every run",
                "atomically replace Rio.arc with a fully written generated archive",
                "restore the verified original after AdvHD exits, including launch errors",
                "recover an interrupted prior session on the next run or recovery command",
                "refuse to overwrite an unrecognized Rio.arc",
            ],
            "warnings": [
                "Power loss can leave the generated Rio.arc installed until recovery runs.",
                "Do not start the original game or update it during an in-place replay session.",
            ],
        }
        sequence_by_id = {
            int(item["replay_id"]): item for item in profile.replay_dispatches
        }
        report["sequence"] = [sequence_by_id[replay_id] for replay_id in selected_ids]
        write_json(output_dir / "advhd_profile.json", profile.to_dict())
        write_json(existing_manifest, report)
        self._write_text_atomic(
            output_dir / "play_continuous_replay_in_place.cmd",
            self._in_place_launcher_text("advhd-run-in-place"),
        )
        self._write_text_atomic(
            output_dir / "restore_original_Rio.cmd",
            self._in_place_launcher_text("advhd-recover-in-place", pause=True),
        )
        self._write_text_atomic(
            output_dir / "check_in_place_replay.cmd",
            self._in_place_launcher_text("advhd-validate-in-place", pause=True),
        )
        return report

    def validate_in_place(self, package: Path) -> dict[str, Any]:
        package = package.resolve()
        manifest_path = package / "manifest.json" if package.is_dir() else package
        result = AdvHdInPlaceReplayRunner().validate(manifest_path)
        report = json.loads(manifest_path.read_text(encoding="utf-8"))
        profile = self._profile_from_manifest(report)
        playlist = tuple(int(value) for value in report["playlist"])
        self._select_playlist(playlist, tuple(profile.replay_ids))
        generated_archive = (manifest_path.parent / report["generated_archive"]).resolve()
        backup_archive = (manifest_path.parent / report["backup_archive"]).resolve()
        arc_manifest = self.reader.read_manifest(generated_archive)
        source_manifest = self.reader.read_manifest(backup_archive)
        source_names = [entry.name for entry in source_manifest.entries]
        generated_names = [entry.name for entry in arc_manifest.entries]
        if generated_names != source_names:
            raise ValueError("generated Rio.arc changed entry names or ordering")

        replaced = set(report["replaced_entries"])
        changed_entries: list[str] = []
        for name in source_names:
            source_payload = self.reader.read_entry(backup_archive, name)
            generated_payload = self.reader.read_entry(generated_archive, name)
            if source_payload != generated_payload:
                changed_entries.append(name)
            elif name in replaced:
                raise ValueError(f"declared replacement did not change payload: {name}")
        if set(changed_entries) != replaced:
            raise ValueError(
                f"generated Rio.arc changed unexpected entries: {changed_entries}"
            )

        names = {entry.name.casefold(): entry.name for entry in arc_manifest.entries}
        start = self._actual_name(names, profile.start_script)
        evret = self._actual_name(names, profile.replay_return_script)
        start_instructions, _, _ = self.parser.parse_instructions(
            start, self.reader.read_entry(generated_archive, start)
        )
        evret_instructions, _, _ = self.parser.parse_instructions(
            evret, self.reader.read_entry(generated_archive, evret)
        )
        start_targets = [
            instruction.operands[0].value
            for instruction in start_instructions
            if instruction.opcode == 0x07
        ]
        evret_targets = [
            instruction.operands[0].value
            for instruction in evret_instructions
            if instruction.opcode == 0x07
        ]
        scene_count = len(playlist)
        expected_conditions = max(0, scene_count - 1)
        condition_count = sum(
            instruction.opcode == 0x01 for instruction in evret_instructions
        )
        valid = (
            start_targets[-1:] == [profile.replay_dispatch_target]
            and evret_targets.count(profile.replay_dispatch_target)
            == expected_conditions
            and evret_targets[-1:] == [profile.main_menu_target]
            and condition_count == expected_conditions
        )
        if not valid:
            raise ValueError("generated continuous replay control flow is invalid")

        transitions = []
        self._validate_start(start_instructions, playlist[0], profile)
        for index, replay_id in enumerate(playlist):
            transition = self._simulate_evret(
                evret_instructions, replay_id, profile
            )
            has_next = index + 1 < len(playlist)
            expected_target = (
                profile.replay_dispatch_target
                if has_next
                else profile.main_menu_target
            )
            expected_next_id = playlist[index + 1] if has_next else 0
            if (
                transition["target"] != expected_target
                or transition["variables"].get(profile.selector_variable)
                != float(expected_next_id)
            ):
                raise ValueError(
                    f"generated return script has invalid transition for replay {replay_id}: "
                    f"{transition}"
                )
            if expected_target == profile.main_menu_target and (
                transition["variables"].get(profile.scene_mode_variable)
                != profile.scene_mode_inactive_value
                or (
                    profile.event_mode_variable is not None
                    and transition["variables"].get(profile.event_mode_variable)
                    != profile.event_mode_inactive_value
                )
            ):
                raise ValueError(
                    "generated return script does not clear replay flags at completion"
                )
            transitions.append(
                {
                    "from": replay_id,
                    "to": (
                        expected_next_id
                        if expected_target == profile.replay_dispatch_target
                        else "menu"
                    ),
                }
            )

        result.update(
            {
                "scene_count": scene_count,
                "available_scene_count": len(profile.replay_ids),
                "playlist": list(playlist),
                "profile_id": profile.profile_id,
                "archive_entry_count": arc_manifest.entry_count,
                "start_target": start_targets[-1],
                "replay_dispatch_count": evret_targets.count(
                    profile.replay_dispatch_target
                ),
                "final_target": evret_targets[-1],
                "changed_entries": changed_entries,
                "preserved_entry_count": len(source_names) - len(changed_entries),
                "transition_count": len(transitions),
                "first_transition": transitions[0],
                "last_transition": transitions[-1],
                "launcher": str(manifest_path.parent / "play_continuous_replay_in_place.cmd"),
                "recovery_launcher": str(manifest_path.parent / "restore_original_Rio.cmd"),
            }
        )
        return result

    def _simulate_evret(
        self,
        instructions,
        replay_id: int,
        profile: AdvHdReplayProfile,
    ) -> dict[str, Any]:
        offsets = {instruction.offset: index for index, instruction in enumerate(instructions)}
        variables: dict[int, float | int] = {
            profile.selector_variable: float(replay_id),
            profile.scene_mode_variable: profile.scene_mode_active_value,
        }
        if profile.event_mode_variable is not None:
            variables[profile.event_mode_variable] = profile.event_mode_inactive_value
        index = 0
        steps = 0
        while steps <= len(instructions) * 2:
            steps += 1
            instruction = instructions[index]
            values = [operand.value for operand in instruction.operands]
            if instruction.opcode == 0x09:
                if int(values[0]) != 0:
                    raise ValueError(
                        "generated return script uses an unverified assignment variant"
                    )
                variables[int(values[1])] = float(values[2])
                index += 1
            elif instruction.opcode == 0x0B:
                variables[int(values[0])] = int(values[1])
                index += 1
            elif instruction.opcode == 0x01:
                if int(values[0]) != 130:
                    raise ValueError(
                        "generated return script uses an unsupported condition operator"
                    )
                matches = variables.get(int(values[1]), 0) == float(values[2])
                target = int(values[3] if matches else values[4])
                if target:
                    if target not in offsets:
                        raise ValueError(
                            f"generated return script jumps to invalid offset {target}"
                        )
                    index = offsets[target]
                else:
                    index += 1
            elif instruction.opcode == 0x07:
                return {
                    "target": str(values[0]),
                    "variables": variables,
                    "step_count": steps,
                }
            elif instruction.opcode == 0xFF:
                raise ValueError(
                    "generated return script reaches file end without a script target"
                )
            else:
                index += 1
        raise ValueError("generated return script control flow did not terminate")

    def _validate_start(
        self,
        instructions,
        first_replay_id: int,
        profile: AdvHdReplayProfile,
    ) -> None:
        assignments: dict[int, float | int] = {}
        for instruction in instructions:
            values = [operand.value for operand in instruction.operands]
            if instruction.opcode == 0x09 and int(values[0]) == 0:
                assignments[int(values[1])] = float(values[2])
            elif instruction.opcode == 0x0B:
                assignments[int(values[0])] = int(values[1])
        expected = {
            profile.selector_variable: float(first_replay_id),
            profile.scene_mode_variable: profile.scene_mode_active_value,
        }
        if profile.event_mode_variable is not None:
            expected[profile.event_mode_variable] = profile.event_mode_inactive_value
        mismatches = {
            variable: {"expected": value, "actual": assignments.get(variable)}
            for variable, value in expected.items()
            if assignments.get(variable) != value
        }
        if mismatches:
            raise ValueError(
                f"generated start script does not initialize replay state: {mismatches}"
            )

    def _profile_from_manifest(self, report: dict[str, Any]) -> AdvHdReplayProfile:
        value = dict(report["profile"])
        value["replay_ids"] = tuple(int(item) for item in value["replay_ids"])
        value["replay_dispatches"] = tuple(value["replay_dispatches"])
        return AdvHdReplayProfile(**value)

    def _actual_name(self, names: dict[str, str], wanted: str) -> str:
        try:
            return names[wanted.casefold()]
        except KeyError as exc:
            raise ValueError(f"generated archive is missing {wanted}") from exc

    def _select_playlist(
        self,
        playlist: list[int] | tuple[int, ...] | None,
        available_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        selected = available_ids if playlist is None else tuple(playlist)
        if not selected:
            raise ValueError("playlist must contain at least one replay id")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in selected):
            raise ValueError("playlist replay ids must be integers")
        if len(set(selected)) != len(selected):
            raise ValueError("playlist cannot contain duplicate replay ids")
        unknown = [value for value in selected if value not in set(available_ids)]
        if unknown:
            raise ValueError(
                f"playlist contains unavailable replay ids {unknown}; "
                f"available ids are {list(available_ids)}"
            )
        return selected

    def _copy_atomic(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.copy.tmp")
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _in_place_launcher_text(self, command: str, pause: bool = False) -> str:
        python = str(Path(sys.executable).resolve())
        source_root = str(Path(__file__).resolve().parents[2])
        lines = [
            "@echo off",
            "setlocal",
            f'set "PYTHONPATH={source_root};%PYTHONPATH%"',
            f'"{python}" -m hgalgame.cli {command} "%~dp0manifest.json"',
            'set "HGAL_EXIT=%ERRORLEVEL%"',
        ]
        if pause:
            lines.append("pause")
        else:
            lines.extend(
                [
                    'if not "%HGAL_EXIT%"=="0" (',
                    "  echo.",
                    "  echo Replay did not finish normally. Run restore_original_Rio.cmd.",
                    "  pause",
                    ")",
                ]
            )
        lines.append("endlocal & exit /b %HGAL_EXIT%")
        return "\r\n".join(lines) + "\r\n"

    def _write_text_atomic(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(value, encoding="utf-8", newline="")
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _file_metadata(self, path: Path) -> dict[str, int]:
        value = path.stat()
        metadata = {
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
            "mode": stat.S_IMODE(value.st_mode),
        }
        attributes = getattr(value, "st_file_attributes", None)
        if attributes is not None:
            metadata["windows_file_attributes"] = int(attributes)
        return metadata
