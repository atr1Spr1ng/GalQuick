from __future__ import annotations

import hashlib
import json
import struct
import uuid
from pathlib import Path
from typing import Any

from hgalgame.output import write_json
from hgalgame.runtime.advhd_bridge import wrap_system_ui
from hgalgame.runtime.advhd_inplace_runner import AdvHdInPlaceReplayRunner
from hgalgame.runtime.advhd_replay_builder import AdvHdContinuousReplayBuilder


class AdvHdSceneSessionBuilder(AdvHdContinuousReplayBuilder):
    """Build the persistent, script-addressable AdvHD controller."""

    CONTROLLER_ENTRY = "HGAL_CTRL.ws2"
    CONTROLLER_TARGET = "HGAL_CTRL"
    PAGE_SIZE = 6

    def build(self, game_dir: Path, output_dir: Path) -> dict[str, Any]:
        game_dir = game_dir.resolve()
        output_dir = output_dir.resolve()
        if output_dir == game_dir or game_dir in output_dir.parents:
            raise ValueError("output_dir must be outside the original game directory")
        if not game_dir.is_dir():
            raise NotADirectoryError(f"game directory not found: {game_dir}")

        manifest_path = output_dir / "manifest.json"
        if manifest_path.is_file():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("format_version") != "advhd-scene-session-v1":
                raise ValueError(
                    "output_dir already contains a different in-place replay package"
                )
            if Path(existing.get("game_dir", "")).resolve() != game_dir:
                raise ValueError(
                    "output_dir belongs to a different game directory; choose another output"
                )
            AdvHdInPlaceReplayRunner().recover(manifest_path)

        profile = self.profiler.build(game_dir)
        replay_ids = tuple(profile.replay_ids)
        if not replay_ids:
            raise ValueError("AdvHD profile contains no replay scenes")
        executable = game_dir / profile.executable
        system_archive = game_dir / profile.system_archive
        source_sha256 = self._sha256(system_archive)
        source_metadata = self._file_metadata(system_archive)
        if source_sha256 != profile.system_archive_sha256:
            raise ValueError("system archive changed while its AdvHD profile was built")

        source_manifest = self.reader.read_manifest(system_archive)
        existing_names = {entry.name.casefold() for entry in source_manifest.entries}
        if self.CONTROLLER_ENTRY.casefold() in existing_names:
            raise ValueError(
                f"system archive already contains reserved entry {self.CONTROLLER_ENTRY}"
            )
        start_name = profile.start_script
        evret_name = profile.replay_return_script
        evret_payload = self.reader.read_entry(system_archive, evret_name)
        replacements = {
            start_name: self.encoder.scene_session_start(
                self.reader.read_entry(system_archive, start_name),
                controller_target=self.CONTROLLER_TARGET,
                menu_target=profile.main_menu_target,
            ),
            evret_name: self.encoder.scene_session_return(
                evret_payload,
                script_name=evret_name,
                selector_variable=profile.selector_variable,
                scene_mode_variable=profile.scene_mode_variable,
                scene_mode_inactive_value=profile.scene_mode_inactive_value,
                event_mode_variable=profile.event_mode_variable,
                event_mode_inactive_value=profile.event_mode_inactive_value,
                controller_target=self.CONTROLLER_TARGET,
            ),
        }
        controller = self.encoder.scene_session_controller(
            evret_payload,
            replay_ids,
            script_name=evret_name,
            page_size=self.PAGE_SIZE,
            selector_variable=profile.selector_variable,
            scene_mode_variable=profile.scene_mode_variable,
            scene_mode_active_value=profile.scene_mode_active_value,
            scene_mode_inactive_value=profile.scene_mode_inactive_value,
            event_mode_variable=profile.event_mode_variable,
            event_mode_inactive_value=profile.event_mode_inactive_value,
            replay_target=profile.replay_dispatch_target,
            menu_target=profile.main_menu_target,
        )
        archive_payload = self.writer.rebuild(
            system_archive,
            replacements,
            additions=[(self.CONTROLLER_ENTRY, controller)],
        )
        generated_sha256 = hashlib.sha256(archive_payload).hexdigest()
        generated_archive = self.writer.write_atomic(
            output_dir / "generated" / self.GENERATED_ARCHIVE,
            archive_payload,
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

        pages = [
            list(replay_ids[index : index + self.PAGE_SIZE])
            for index in range(0, len(replay_ids), self.PAGE_SIZE)
        ]
        token = uuid.uuid4().hex
        ui_archive = game_dir / 'Script.arc'
        ui_source_hash = self._sha256(ui_archive)
        ui_payload = self.writer.rebuild(ui_archive, {
            'LegacyGame.lua': wrap_system_ui(self.reader.read_entry(ui_archive, 'LegacyGame.lua'), token),
        })
        ui_backup = output_dir / 'backup' / f'Script.original.{ui_source_hash[:16]}.arc'
        if not ui_backup.exists():
            self._copy_atomic(ui_archive, ui_backup)
        if self._sha256(ui_backup) != ui_source_hash:
            raise ValueError('original Script.arc backup failed SHA-256 verification')
        ui_generated = self.writer.write_atomic(output_dir / 'generated' / 'Script.arc', ui_payload)
        write_json(output_dir / 'bridge_manifest.json', {
            'runtime_mode': 'in-place', 'game_dir': str(game_dir),
            'archive_name': 'Script.arc', 'executable': executable.name,
            'source_sha256': ui_source_hash,
            'source_file_metadata': self._file_metadata(ui_archive),
            'generated_archive': str(ui_generated.relative_to(output_dir)),
            'generated_sha256': hashlib.sha256(ui_payload).hexdigest(),
            'backup_archive': str(ui_backup.relative_to(output_dir)),
            'state_file': 'bridge_in_place_state.json', 'lock_file': 'bridge_in_place_session.lock',
        })
        report: dict[str, Any] = {
            "format_version": "advhd-scene-session-v1",
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
            "available_scene_count": len(replay_ids),
            "available_replay_ids": list(replay_ids),
            "controller": {
                "entry": self.CONTROLLER_ENTRY,
                "target": self.CONTROLLER_TARGET,
                "page_size": self.PAGE_SIZE,
                "pages": pages,
                "navigation": "lua-file-bridge-v1",
                "bridge_token": token,
            },
            "replaced_entries": [start_name, evret_name],
            "added_entries": [self.CONTROLLER_ENTRY],
            "preserved_entry_count": source_manifest.entry_count
            - len(replacements),
            "companion_manifests": ['bridge_manifest.json'],
            "game_directory_write_scope": [system_archive.name, 'Script.arc',
                                           'hgal-bridge-request.txt', 'hgal-bridge-status.txt'],
            "restore_guarantees": [
                "verify the original backup before every session",
                "atomically install a fully written generated Rio.arc",
                "keep one AdvHD process alive for the complete reading session",
                "restore the verified original after AdvHD exits or startup fails",
                "recover an interrupted prior session on the next launch",
                "refuse to overwrite an unrecognized Rio.arc",
            ],
            "warnings": [
                "Power loss can leave generated Rio.arc installed until recovery runs.",
                "Do not start a second copy of the game during a Scene Review session.",
            ],
        }
        write_json(output_dir / "advhd_profile.json", profile.to_dict())
        write_json(manifest_path, report)
        self._write_text_atomic(
            output_dir / "restore_original_Rio.cmd",
            self._in_place_launcher_text("advhd-recover-in-place", pause=True),
        )
        return report

    def validate(self, package: Path) -> dict[str, Any]:
        manifest_path = package.resolve()
        if manifest_path.is_dir():
            manifest_path = manifest_path / "manifest.json"
        result = AdvHdInPlaceReplayRunner().validate(manifest_path)
        report = json.loads(manifest_path.read_text(encoding="utf-8"))
        if report.get("format_version") != "advhd-scene-session-v1":
            raise ValueError("manifest is not an AdvHD persistent Scene session")
        profile = self._profile_from_manifest(report)
        generated = (manifest_path.parent / report["generated_archive"]).resolve()
        source = (manifest_path.parent / report["backup_archive"]).resolve()
        source_names = [entry.name for entry in self.reader.read_manifest(source).entries]
        generated_names = [
            entry.name for entry in self.reader.read_manifest(generated).entries
        ]
        if generated_names != source_names + [self.CONTROLLER_ENTRY]:
            raise ValueError("generated archive has an unexpected entry layout")

        changed = []
        for name in source_names:
            if self.reader.read_entry(source, name) != self.reader.read_entry(
                generated, name
            ):
                changed.append(name)
        if set(changed) != set(report["replaced_entries"]):
            raise ValueError(f"generated archive changed unexpected entries: {changed}")

        start, _, _ = self.parser.parse_instructions(
            profile.start_script,
            self.reader.read_entry(generated, profile.start_script),
        )
        evret, _, _ = self.parser.parse_instructions(
            profile.replay_return_script,
            self.reader.read_entry(generated, profile.replay_return_script),
        )
        controller, _, _ = self.parser.parse_instructions(
            self.CONTROLLER_ENTRY,
            self.reader.read_entry(generated, self.CONTROLLER_ENTRY),
        )
        controller_payload = self.reader.read_entry(generated, self.CONTROLLER_ENTRY)
        expected_counts = (
            sum(item.opcode == 0x14 for item in controller),
            sum(len(item.choices) for item in controller),
        )
        if struct.unpack('<II', controller_payload[-8:]) != expected_counts:
            raise ValueError('controller allocation counts do not match its records')
        if start[-2].opcode != 0x07 or start[-2].operands[0].value != self.CONTROLLER_TARGET:
            raise ValueError("generated start script does not enter the controller")
        if evret[-2].opcode != 0x07 or evret[-2].operands[0].value != self.CONTROLLER_TARGET:
            raise ValueError("generated return script does not re-enter the controller")
        choice_count = sum(item.opcode == 0x0F for item in controller)
        expected_choices = 1 + len(report["controller"]["pages"])
        if choice_count != expected_choices:
            raise ValueError("generated controller has an invalid page layout")
        replay_values = {
            int(item.operands[2].value)
            for item in controller
            if item.opcode == 0x09
            and item.operands[0].value == 0
            and item.operands[1].value == profile.selector_variable
            and item.operands[2].value > 0
        }
        if replay_values != set(profile.replay_ids):
            raise ValueError("generated controller cannot dispatch every replay id")
        result.update(
            {
                "session_controller_valid": True,
                "controller_entry": self.CONTROLLER_ENTRY,
                "controller_choice_count": choice_count,
                "available_scene_count": len(profile.replay_ids),
                "changed_entries": changed,
                "added_entries": [self.CONTROLLER_ENTRY],
            }
        )
        return result
