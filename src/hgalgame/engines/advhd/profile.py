from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hgalgame.engines.advhd.archive import AdvHdArcReader
from hgalgame.engines.advhd.replay_catalog import (
    ReplayDispatch,
    extract_replay_dispatches,
    operand_values,
    ws2_stem,
)
from hgalgame.engines.advhd.ws2_parser import Ws2Instruction, Ws2Parser


@dataclass(frozen=True)
class AdvHdParsedScript:
    archive: str
    entry: str
    instructions: list[Ws2Instruction]


@dataclass(frozen=True)
class AdvHdReplayProfile:
    format_version: str
    profile_id: str
    engine: str
    game_dir: str
    executable: str
    executable_sha256: str
    system_archive: str
    system_archive_sha256: str
    story_archive: str
    story_archive_sha256: str
    start_script: str
    main_menu_target: str
    replay_dispatch_script: str
    replay_dispatch_target: str
    replay_return_script: str
    replay_return_target: str
    selector_operator: int
    selector_variable: int
    scene_mode_operator: int
    scene_mode_variable: int
    scene_mode_active_value: int
    scene_mode_inactive_value: int
    event_mode_variable: int | None
    event_mode_active_value: int | None
    event_mode_inactive_value: int | None
    replay_ids: tuple[int, ...]
    replay_dispatches: tuple[dict[str, Any], ...]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["replay_ids"] = list(self.replay_ids)
        value["replay_dispatches"] = list(self.replay_dispatches)
        return value


class AdvHdReplayProfiler:
    """Infer the replay protocol from engine-authored WS2 control flow."""

    FORMAT_VERSION = "advhd-replay-profile-v1"

    def __init__(self) -> None:
        self.reader = AdvHdArcReader()
        self.parser = Ws2Parser()

    def build(self, game_dir: Path) -> AdvHdReplayProfile:
        game_dir = game_dir.resolve()
        if not game_dir.is_dir():
            raise NotADirectoryError(f"game directory not found: {game_dir}")
        executable = self._executable(game_dir)
        archive_paths = sorted(game_dir.glob("Rio*.arc"), key=lambda path: path.name.casefold())
        if not archive_paths:
            raise ValueError("game directory contains no Rio ARC archives")
        manifests = {path.name: self.reader.read_manifest(path) for path in archive_paths}

        parsed: dict[tuple[str, str], AdvHdParsedScript] = {}
        dispatch_candidates: list[
            tuple[Path, str, list[ReplayDispatch], AdvHdParsedScript]
        ] = []
        parse_failures: list[dict[str, str]] = []
        for archive in archive_paths:
            for entry in manifests[archive.name].entries:
                if not entry.name.casefold().endswith(".ws2"):
                    continue
                try:
                    instructions, _, _ = self.parser.parse_instructions(
                        entry.name, self.reader.read_entry(archive, entry.name)
                    )
                except ValueError as exc:
                    parse_failures.append(
                        {"archive": archive.name, "entry": entry.name, "error": str(exc)}
                    )
                    continue
                script = AdvHdParsedScript(archive.name, entry.name, instructions)
                parsed[(archive.name, entry.name)] = script
                try:
                    dispatches = extract_replay_dispatches(instructions)
                except ValueError:
                    continue
                dispatch_candidates.append((archive, entry.name, dispatches, script))

        if not dispatch_candidates:
            raise ValueError("no structurally valid AdvHD replay dispatch table found")
        largest = max(len(item[2]) for item in dispatch_candidates)
        largest_candidates = [item for item in dispatch_candidates if len(item[2]) == largest]
        if len(largest_candidates) != 1:
            labels = [f"{item[0].name}:{item[1]}" for item in largest_candidates]
            raise ValueError(f"ambiguous replay dispatch tables: {labels}")
        system_archive, dispatch_entry, dispatches, _ = largest_candidates[0]

        story_archive = self._story_archive(archive_paths, manifests, dispatches)
        story_scripts: dict[str, AdvHdParsedScript] = {}
        for entry in manifests[story_archive.name].entries:
            if not entry.name.casefold().endswith(".ws2"):
                continue
            script = parsed.get((story_archive.name, entry.name))
            if script is None:
                instructions, _, _ = self.parser.parse_instructions(
                    entry.name, self.reader.read_entry(story_archive, entry.name)
                )
                script = AdvHdParsedScript(story_archive.name, entry.name, instructions)
            key = ws2_stem(entry.name)
            if key in story_scripts:
                raise ValueError(f"duplicate story script stem: {entry.name}")
            story_scripts[key] = script

        control = infer_replay_control(dispatches, story_scripts)
        system_manifest = manifests[system_archive.name]
        return_entry = self._entry_by_stem(
            system_manifest, str(control["replay_return_target"])
        )
        start_entry = self._entry_by_stem(system_manifest, "start")
        start_script = parsed.get((system_archive.name, start_entry))
        if start_script is None:
            instructions, _, _ = self.parser.parse_instructions(
                start_entry, self.reader.read_entry(system_archive, start_entry)
            )
            start_script = AdvHdParsedScript(
                system_archive.name, start_entry, instructions
            )
        main_menu_target = self._final_script_target(start_script)
        menu_clear_evidence = self._menu_clear_evidence(
            parsed,
            system_archive.name,
            main_menu_target,
            int(control["scene_mode_variable"]),
            control["event_mode_variable"],
            int(control["scene_mode_inactive_value"]),
            control["event_mode_inactive_value"],
        )

        source_hash = self._sha256(system_archive)
        profile_id = source_hash[:16]
        return AdvHdReplayProfile(
            format_version=self.FORMAT_VERSION,
            profile_id=profile_id,
            engine="advhd",
            game_dir=str(game_dir),
            executable=executable.name,
            executable_sha256=self._sha256(executable),
            system_archive=system_archive.name,
            system_archive_sha256=source_hash,
            story_archive=story_archive.name,
            story_archive_sha256=self._sha256(story_archive),
            start_script=start_entry,
            main_menu_target=main_menu_target,
            replay_dispatch_script=dispatch_entry,
            replay_dispatch_target=Path(dispatch_entry).stem,
            replay_return_script=return_entry,
            replay_return_target=str(control["replay_return_target"]),
            selector_operator=int(control["selector_operator"]),
            selector_variable=int(control["selector_variable"]),
            scene_mode_operator=int(control["scene_mode_operator"]),
            scene_mode_variable=int(control["scene_mode_variable"]),
            scene_mode_active_value=int(control["scene_mode_active_value"]),
            scene_mode_inactive_value=int(control["scene_mode_inactive_value"]),
            event_mode_variable=control["event_mode_variable"],
            event_mode_active_value=control["event_mode_active_value"],
            event_mode_inactive_value=control["event_mode_inactive_value"],
            replay_ids=tuple(item.replay_id for item in dispatches),
            replay_dispatches=tuple(item.to_dict() for item in dispatches),
            evidence={
                "method": (
                    "largest contiguous replay-id dispatch table plus exactly one "
                    "mode-guarded local entry and return exit for every replay id"
                ),
                "dispatch_candidate_count": len(dispatch_candidates),
                "dispatch_candidates": [
                    {
                        "archive": archive.name,
                        "entry": entry,
                        "replay_count": len(items),
                        "selector_variable": items[0].selector_variable,
                    }
                    for archive, entry, items, _ in dispatch_candidates
                ],
                "parse_failure_count": len(parse_failures),
                "control": control["evidence"],
                "start_script_method": (
                    "AdvHD bootstrap entry named start.ws2, verified to end in one "
                    "cross-script menu target"
                ),
                "return_script_resolved": return_entry,
                "menu_clear_assignments": menu_clear_evidence,
            },
        )

    def _story_archive(self, archive_paths, manifests, dispatches) -> Path:
        targets = {item.script.casefold() for item in dispatches}
        complete: list[Path] = []
        coverage: list[tuple[int, str]] = []
        for archive in archive_paths:
            stems = {
                ws2_stem(entry.name)
                for entry in manifests[archive.name].entries
                if entry.name.casefold().endswith(".ws2")
            }
            count = len(stems & targets)
            coverage.append((count, archive.name))
            if count == len(targets):
                complete.append(archive)
        if len(complete) != 1:
            raise ValueError(
                "expected exactly one story archive resolving all replay targets; "
                f"coverage={coverage}"
            )
        return complete[0]

    def _entry_by_stem(self, manifest, wanted_stem: str) -> str:
        wanted = wanted_stem.casefold()
        matches = [
            entry.name
            for entry in manifest.entries
            if entry.name.casefold().endswith(".ws2") and ws2_stem(entry.name) == wanted
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected one system script with stem {wanted_stem!r}, found {matches}"
            )
        return matches[0]

    def _final_script_target(self, script: AdvHdParsedScript) -> str:
        instructions = script.instructions
        if len(instructions) < 2 or instructions[-1].opcode != 0xFF:
            raise ValueError(f"bootstrap script does not end normally: {script.entry}")
        jump = instructions[-2]
        if jump.opcode != 0x07:
            raise ValueError(
                f"bootstrap script does not end in a cross-script jump: {script.entry}"
            )
        return str(operand_values(jump)[0])

    def _menu_clear_evidence(
        self,
        parsed: dict[tuple[str, str], AdvHdParsedScript],
        system_archive: str,
        menu_target: str,
        scene_variable: int,
        event_variable: int | None,
        scene_inactive: int,
        event_inactive: int | None,
    ) -> list[dict[str, Any]]:
        entry = None
        for (archive, name), script in parsed.items():
            if archive == system_archive and ws2_stem(name) == menu_target.casefold():
                entry = script
                break
        if entry is None:
            raise ValueError(
                f"main menu target {menu_target!r} does not resolve in {system_archive}"
            )
        expected = {scene_variable: scene_inactive}
        if event_variable is not None and event_inactive is not None:
            expected[event_variable] = event_inactive
        evidence: list[dict[str, Any]] = []
        seen: set[int] = set()
        for instruction in entry.instructions:
            values = operand_values(instruction)
            if instruction.opcode == 0x0B:
                variable, value = int(values[0]), int(values[1])
            elif instruction.opcode == 0x09 and int(values[0]) == 0:
                variable, raw = int(values[1]), float(values[2])
                if not raw.is_integer():
                    continue
                value = int(raw)
            else:
                continue
            if variable in expected and value == expected[variable]:
                seen.add(variable)
                evidence.append(
                    {
                        "script": entry.entry,
                        "offset": instruction.offset,
                        "variable": variable,
                        "value": value,
                    }
                )
        missing = set(expected) - seen
        if missing:
            raise ValueError(
                f"main menu does not prove inactive replay state for variables {sorted(missing)}"
            )
        return evidence

    def _executable(self, game_dir: Path) -> Path:
        matches = [
            path
            for path in game_dir.iterdir()
            if path.is_file() and path.name.casefold() == "advhd.exe"
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one AdvHD.exe, found {len(matches)}")
        return matches[0]

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


def infer_replay_control(
    dispatches: list[ReplayDispatch],
    scripts: dict[str, AdvHdParsedScript],
) -> dict[str, Any]:
    """Infer selector/mode variables only when every replay closes the same loop."""

    if not dispatches:
        raise ValueError("cannot infer replay control without dispatches")
    selector_variables = {item.selector_variable for item in dispatches}
    if len(selector_variables) != 1:
        raise ValueError("replay dispatches do not share one selector variable")
    selector_variable = next(iter(selector_variables))
    expected_ids = {item.replay_id for item in dispatches}
    dispatch_scripts = {item.replay_id: item.script.casefold() for item in dispatches}

    starts: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    exits: dict[tuple[int, int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for script_key, script in scripts.items():
        instructions = script.instructions
        for mode_condition, id_condition, action in zip(
            instructions, instructions[1:], instructions[2:]
        ):
            if mode_condition.opcode != 0x01 or id_condition.opcode != 0x01:
                continue
            mode_values = operand_values(mode_condition)
            id_values = operand_values(id_condition)
            if (
                len(mode_values) != 5
                or len(id_values) != 5
                or int(id_values[0]) != 130
                or int(id_values[1]) != selector_variable
            ):
                continue
            replay_value = float(id_values[2])
            mode_value = float(mode_values[2])
            if (
                not replay_value.is_integer()
                or replay_value <= 0
                or not mode_value.is_integer()
                or not 0 <= mode_value <= 255
            ):
                continue
            record = {
                "replay_id": int(replay_value),
                "script_key": script_key,
                "script": script.entry,
                "guard_offset": mode_condition.offset,
                "action_offset": action.offset,
            }
            mode_key = (
                int(mode_values[0]),
                int(mode_values[1]),
                int(mode_value),
            )
            if action.opcode in {0x02, 0x06}:
                record["target_offset"] = int(operand_values(action)[0])
                starts[mode_key].append(record)
            elif action.opcode == 0x07:
                target = str(operand_values(action)[0])
                record["target"] = target
                exits[(*mode_key, target.casefold())].append(record)

    scene_candidates: list[dict[str, Any]] = []
    for mode_key, start_records in starts.items():
        if not _exactly_once_per_id(start_records, expected_ids):
            continue
        if any(
            dispatch_scripts[record["replay_id"]] != record["script_key"]
            for record in start_records
        ):
            continue
        matching_exits = [
            (exit_key, records)
            for exit_key, records in exits.items()
            if exit_key[:3] == mode_key and _exactly_once_per_id(records, expected_ids)
        ]
        for exit_key, exit_records in matching_exits:
            scene_candidates.append(
                {
                    "mode_key": mode_key,
                    "return_target": exit_records[0]["target"],
                    "start_records": start_records,
                    "exit_records": exit_records,
                }
            )
    if len(scene_candidates) != 1:
        labels = [
            {
                "mode": item["mode_key"],
                "target": item["return_target"],
                "count": len(item["start_records"]),
            }
            for item in scene_candidates
        ]
        raise ValueError(f"expected one exact full-scene replay guard, found {labels}")

    scene = scene_candidates[0]
    mode_operator, scene_mode_variable, scene_active = scene["mode_key"]
    if scene_mode_variable == selector_variable:
        raise ValueError("scene mode and replay selector unexpectedly share one variable")
    scene_inactive = 0 if scene_active != 0 else 1
    return_key = str(scene["return_target"]).casefold()

    event_candidates: list[dict[str, Any]] = []
    for exit_key, records in exits.items():
        operator, variable, active, target = exit_key
        ids = {record["replay_id"] for record in records}
        if (
            target != return_key
            or variable in {selector_variable, scene_mode_variable}
            or not expected_ids <= ids
            or len(records) <= len(expected_ids)
        ):
            continue
        event_candidates.append(
            {
                "operator": operator,
                "variable": variable,
                "active": active,
                "record_count": len(records),
            }
        )
    event_candidates.sort(key=lambda item: item["record_count"], reverse=True)
    if len(event_candidates) > 1 and (
        event_candidates[0]["record_count"] == event_candidates[1]["record_count"]
    ):
        raise ValueError(f"ambiguous event replay mode candidates: {event_candidates}")
    event = event_candidates[0] if event_candidates else None

    return {
        "selector_operator": 130,
        "selector_variable": selector_variable,
        "scene_mode_operator": mode_operator,
        "scene_mode_variable": scene_mode_variable,
        "scene_mode_active_value": scene_active,
        "scene_mode_inactive_value": scene_inactive,
        "event_mode_variable": event["variable"] if event else None,
        "event_mode_active_value": event["active"] if event else None,
        "event_mode_inactive_value": (
            0 if event and event["active"] != 0 else (1 if event else None)
        ),
        "replay_return_target": scene["return_target"],
        "evidence": {
            "expected_replay_ids": sorted(expected_ids),
            "full_scene_start_count": len(scene["start_records"]),
            "full_scene_exit_count": len(scene["exit_records"]),
            "full_scene_start_scripts_match_dispatches": True,
            "scene_mode_candidate_count": len(scene_candidates),
            "event_mode_candidates": event_candidates,
        },
    }


def _exactly_once_per_id(records: list[dict[str, Any]], expected: set[int]) -> bool:
    counts = Counter(record["replay_id"] for record in records)
    return set(counts) == expected and all(count == 1 for count in counts.values())
