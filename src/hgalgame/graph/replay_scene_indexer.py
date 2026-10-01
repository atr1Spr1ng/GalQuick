from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from hgalgame.engines.advhd.archive import AdvHdArcReader
from hgalgame.engines.advhd.event_mapper import map_instructions_to_events
from hgalgame.engines.advhd.profile import AdvHdReplayProfile, AdvHdReplayProfiler
from hgalgame.engines.advhd.replay_catalog import (
    ReplayDispatch,
    extract_gallery_assets,
    extract_replay_dispatches,
    group_gallery_assets,
    operand_values,
    ws2_stem,
)
from hgalgame.engines.advhd.ws2_parser import Ws2Instruction, Ws2Parser


@dataclass(frozen=True)
class _Script:
    archive: str
    entry: str
    instructions: list[Ws2Instruction]


class AdvHdReplaySceneIndexer:
    """Build exact replay-mode scenes from AdvHD's own replay machinery."""

    FORMAT_VERSION = "advhd-replay-scene-index-v2"
    GALLERY_SCRIPT = "CG_PAGE.ws2"

    def __init__(self) -> None:
        self.archive_reader = AdvHdArcReader()
        self.parser = Ws2Parser()
        self.profiler = AdvHdReplayProfiler()

    def build(self, game_dir: Path, include_events: bool = True) -> dict[str, Any]:
        game_dir = game_dir.resolve()
        profile = self.profiler.build(game_dir)
        archive_paths = sorted(game_dir.glob("Rio*.arc"))
        manifests = {
            path.name: self.archive_reader.read_manifest(path) for path in archive_paths
        }
        system_archive = game_dir / profile.system_archive
        story_archive = game_dir / profile.story_archive
        replay_entry = profile.replay_dispatch_script
        replay_instructions, _, replay_transform = self.parser.parse_instructions(
            replay_entry,
            self.archive_reader.read_entry(system_archive, replay_entry),
        )
        dispatches = extract_replay_dispatches(replay_instructions)
        if [item.to_dict() for item in dispatches] != list(profile.replay_dispatches):
            raise ValueError("replay dispatch table changed after profile inference")

        gallery_entry = self._actual_entry(
            manifests[system_archive.name], self.GALLERY_SCRIPT
        )
        gallery_instructions, _, gallery_transform = self.parser.parse_instructions(
            gallery_entry,
            self.archive_reader.read_entry(system_archive, gallery_entry),
        )
        gallery_assets = extract_gallery_assets(gallery_instructions)
        gallery_by_name = {item.asset.casefold(): item.to_dict() for item in gallery_assets}
        gallery_by_family = {
            self._cg_family(item.asset): item.to_dict() for item in gallery_assets
        }

        scripts = self._parse_story_scripts(story_archive, manifests[story_archive.name])
        starts = self._find_replay_starts(scripts, profile)
        exits = self._find_replay_exits(scripts, profile)
        scenes = [
            self._build_scene(
                dispatch,
                scripts,
                starts.get(dispatch.replay_id, []),
                exits.get(dispatch.replay_id, []),
                gallery_by_name,
                gallery_by_family,
                include_events,
                profile,
            )
            for dispatch in dispatches
        ]

        event_totals: Counter[str] = Counter()
        for scene in scenes:
            event_totals.update(scene["event_counts"])
        exact = sum(scene["boundary"]["confidence"] == "exact" for scene in scenes)
        return {
            "game_dir": str(game_dir),
            "read_only": True,
            "engine": "advhd",
            "format_version": self.FORMAT_VERSION,
            "replay_profile": profile.to_dict(),
            "system_archive": system_archive.name,
            "story_archive": story_archive.name,
            "evidence": {
                "replay_catalog": {
                    "archive": system_archive.name,
                    "entry": replay_entry,
                    "transform": replay_transform,
                    "method": "engine replay-id condition followed by script dispatch",
                },
                "gallery_catalog": {
                    "archive": system_archive.name,
                    "entry": gallery_entry,
                    "transform": gallery_transform,
                    "method": "engine openCgBrowser calls grouped by CG_PAGE conditions",
                },
                "boundary_method": (
                    f"inferred selector {profile.selector_variable} chooses a script/offset; "
                    f"inferred full-scene mode {profile.scene_mode_variable} guards the "
                    f"same id's {profile.replay_return_target} exit"
                ),
            },
            "summary": {
                "scene_count": len(scenes),
                "exact_boundary_count": exact,
                "gallery_group_count": len({item.group_id for item in gallery_assets}),
                "gallery_asset_count": len(gallery_assets),
                "scene_event_count": sum(event_totals.values()),
                "scene_event_counts": dict(event_totals),
                "scene_cg_reference_count": sum(
                    len(scene["event_cg_assets"]) for scene in scenes
                ),
                "gallery_exact_match_reference_count": sum(
                    sum(
                        asset["gallery_match"] == "exact"
                        for asset in scene["event_cg_assets"]
                    )
                    for scene in scenes
                ),
                "gallery_family_match_reference_count": sum(
                    sum(
                        asset["gallery_match"] == "family"
                        for asset in scene["event_cg_assets"]
                    )
                    for scene in scenes
                ),
            },
            "replay_dispatches": [item.to_dict() for item in dispatches],
            "gallery": group_gallery_assets(gallery_assets),
            "scenes": scenes,
        }

    def _build_scene(
        self,
        dispatch: ReplayDispatch,
        scripts: dict[str, _Script],
        starts: list[dict[str, Any]],
        exits: list[dict[str, Any]],
        gallery_by_name: dict[str, dict[str, Any]],
        gallery_by_family: dict[str, dict[str, Any]],
        include_events: bool,
        profile: AdvHdReplayProfile,
    ) -> dict[str, Any]:
        if len(starts) != 1:
            raise ValueError(
                f"replay {dispatch.replay_id} has {len(starts)} guarded entry jumps"
            )
        if len(exits) != 1:
            raise ValueError(
                f"replay {dispatch.replay_id} has {len(exits)} guarded return exits"
            )
        start = starts[0]
        exit_record = exits[0]
        if ws2_stem(start["script"]) != dispatch.script.casefold():
            raise ValueError(
                f"replay {dispatch.replay_id} dispatches {dispatch.script}, but its "
                f"guarded start is in {start['script']}"
            )

        path = self._find_script_path(
            scripts,
            start_script=ws2_stem(start["script"]),
            start_offset=start["offset"],
            exit_script=ws2_stem(exit_record["script"]),
            exit_offset=exit_record["offset"],
        )
        reachable, trace = self._trace_replay_mode(
            scripts,
            replay_id=dispatch.replay_id,
            start_script=ws2_stem(start["script"]),
            start_offset=start["offset"],
            exit_script=ws2_stem(exit_record["script"]),
            exit_offset=exit_record["offset"],
            profile=profile,
        )
        event_records: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        cg_assets: dict[str, dict[str, Any]] = {}
        voices: set[str] = set()
        bgm: set[str] = set()
        speakers: set[str] = set()
        for segment in path:
            script = scripts[segment["script_key"]]
            events, _ = map_instructions_to_events(script.instructions)
            selected = [
                event
                for event in events
                if (segment["script_key"], event.offset) in reachable
            ]
            counts.update(event.type for event in selected)
            for event in selected:
                record = event.to_dict()
                record["script"] = script.entry
                event_records.append(record)
                if event.type == "image" and event.data.get("role") == "event_cg_candidate":
                    asset = event.data["asset"]
                    key = asset.casefold()
                    gallery = gallery_by_name.get(key)
                    match = "exact" if gallery else None
                    if gallery is None:
                        gallery = gallery_by_family.get(self._cg_family(asset))
                        match = "family" if gallery else None
                    cg_assets.setdefault(
                        key,
                        {
                            "asset": asset,
                            "gallery_match": match,
                            "family": self._cg_family(asset),
                            "gallery_group_id": gallery["group_id"] if gallery else None,
                            "unlock_variable": gallery["unlock_variable"] if gallery else None,
                        },
                    )
                if event.type == "voice":
                    voices.add(event.data["asset"])
                elif event.type == "bgm":
                    bgm.add(event.data["asset"])
                elif event.type == "dialogue" and event.data.get("speaker"):
                    speakers.add(event.data["speaker"])

        result: dict[str, Any] = {
            "scene_id": f"replay_{dispatch.replay_id:03d}",
            "replay_id": dispatch.replay_id,
            "selector": {
                "variable": dispatch.selector_variable,
                "value": dispatch.replay_id,
            },
            "dispatch": dispatch.to_dict(),
            "boundary": {
                "confidence": "exact",
                "start": start,
                "end": exit_record,
                "evidence": [
                    "inferred replay dispatch",
                    "story replay-id guarded jump",
                    "story replay-mode and replay-id guarded return",
                ],
            },
            "script_path": [
                {key: value for key, value in segment.items() if key != "script_key"}
                for segment in path
            ],
            "control_flow": trace,
            "timeline_semantics": (
                "ordered within each script; union of branches reachable with "
                "replay mode fixed and other game-state variables unknown"
            ),
            "event_counts": dict(counts),
            "event_count": sum(counts.values()),
            "event_cg_assets": list(cg_assets.values()),
            "voice_assets": sorted(voices, key=str.casefold),
            "bgm_assets": sorted(bgm, key=str.casefold),
            "speakers": sorted(speakers),
        }
        if include_events:
            result["events"] = event_records
        return result

    def _trace_replay_mode(
        self,
        scripts: dict[str, _Script],
        replay_id: int,
        start_script: str,
        start_offset: int,
        exit_script: str,
        exit_offset: int,
        profile: AdvHdReplayProfile,
    ) -> tuple[set[tuple[str, int]], dict[str, Any]]:
        """Find instructions reachable under the known Scene Replay state.

        Conditions on unknown story variables conservatively keep both edges;
        the three engine replay variables are fixed. The original engine remains
        responsible for selecting unknown branches during actual playback.
        """

        offsets = {
            key: {instruction.offset: index for index, instruction in enumerate(script.instructions)}
            for key, script in scripts.items()
        }
        fixed_variables: dict[int, float | int | None] = {
            profile.selector_variable: float(replay_id),
            profile.scene_mode_variable: profile.scene_mode_active_value,
        }
        if profile.event_mode_variable is not None:
            fixed_variables[profile.event_mode_variable] = (
                profile.event_mode_inactive_value
            )
        terminal = (exit_script, exit_offset)
        queue = deque([(start_script, start_offset)])
        reachable: set[tuple[str, int]] = set()
        unknown_condition_count = 0
        reached_terminal = False

        while queue:
            script_key, offset = queue.popleft()
            node = (script_key, offset)
            if node in reachable:
                continue
            script = scripts.get(script_key)
            if script is None or offset not in offsets[script_key]:
                raise ValueError(
                    f"replay {replay_id} reaches invalid instruction {script_key}:0x{offset:X}"
                )
            reachable.add(node)
            if node == terminal:
                reached_terminal = True
                continue

            index = offsets[script_key][offset]
            instruction = script.instructions[index]
            values = operand_values(instruction)
            fallthrough = (
                (script_key, script.instructions[index + 1].offset)
                if index + 1 < len(script.instructions)
                else None
            )
            successors: list[tuple[str, int]] = []

            if instruction.opcode in {0x02, 0x06}:
                successors.append((script_key, int(values[0])))
            elif instruction.opcode == 0x01:
                variable = int(values[1])
                targets = [int(values[3]), int(values[4])]
                if variable in fixed_variables and int(values[0]) in {2, 130}:
                    selected = (
                        targets[0]
                        if fixed_variables[variable] == float(values[2])
                        else targets[1]
                    )
                    if selected:
                        successors.append((script_key, selected))
                    elif fallthrough:
                        successors.append(fallthrough)
                else:
                    unknown_condition_count += 1
                    for target in targets:
                        candidate = (script_key, target) if target else fallthrough
                        if candidate and candidate not in successors:
                            successors.append(candidate)
            elif instruction.opcode == 0x07:
                target = str(values[0]).casefold()
                if target in scripts:
                    successors.append((target, scripts[target].instructions[0].offset))
            elif instruction.opcode == 0x0F:
                for option in instruction.choices:
                    target = option.target.operands[0].value
                    if option.target.opcode == 0x06:
                        successors.append((script_key, int(target)))
                    else:
                        target_key = str(target).casefold()
                        if target_key in scripts:
                            successors.append(
                                (target_key, scripts[target_key].instructions[0].offset)
                            )
            elif instruction.opcode != 0xFF and fallthrough:
                successors.append(fallthrough)

            queue.extend(successors)

        if not reached_terminal:
            raise ValueError(
                f"replay {replay_id} cannot reach its guarded return exit"
            )
        reachable_scripts = []
        for key in scripts:
            count = sum(node[0] == key for node in reachable)
            if count:
                reachable_scripts.append(
                    {"script": scripts[key].entry, "reachable_instruction_count": count}
                )
        return reachable, {
            "fixed_variables": {
                "replay_selector": {
                    "variable": profile.selector_variable,
                    "value": replay_id,
                },
                "scene_replay_mode": {
                    "variable": profile.scene_mode_variable,
                    "value": profile.scene_mode_active_value,
                },
                "event_replay_mode": (
                    {
                        "variable": profile.event_mode_variable,
                        "value": profile.event_mode_inactive_value,
                    }
                    if profile.event_mode_variable is not None
                    else None
                ),
            },
            "reachable_instruction_count": len(reachable),
            "unknown_condition_count": unknown_condition_count,
            "terminal_reached": True,
            "scripts": reachable_scripts,
        }

    def _parse_story_scripts(self, archive_path, manifest) -> dict[str, _Script]:
        scripts: dict[str, _Script] = {}
        for entry in manifest.entries:
            if not entry.name.casefold().endswith(".ws2"):
                continue
            instructions, _, _ = self.parser.parse_instructions(
                entry.name, self.archive_reader.read_entry(archive_path, entry.name)
            )
            scripts[ws2_stem(entry.name)] = _Script(
                archive=archive_path.name,
                entry=entry.name,
                instructions=instructions,
            )
        return scripts

    def _find_replay_starts(
        self,
        scripts: dict[str, _Script],
        profile: AdvHdReplayProfile,
    ) -> dict[int, list[dict[str, Any]]]:
        starts: dict[int, list[dict[str, Any]]] = {}
        for script in scripts.values():
            instructions = script.instructions
            for index in range(len(instructions) - 2):
                replay_flag, replay_id, jump = instructions[index : index + 3]
                id_values = operand_values(replay_id)
                if not self._condition(
                    replay_flag,
                    operator=profile.scene_mode_operator,
                    variable=profile.scene_mode_variable,
                    value=profile.scene_mode_active_value,
                ):
                    continue
                if (
                    replay_id.opcode != 0x01
                    or id_values[0] != profile.selector_operator
                    or id_values[1] != profile.selector_variable
                ):
                    continue
                if jump.opcode not in {0x02, 0x06}:
                    continue
                value = float(id_values[2])
                if not value.is_integer() or value <= 0:
                    continue
                starts.setdefault(int(value), []).append(
                    {
                        "archive": script.archive,
                        "script": script.entry,
                        "guard_offset": replay_flag.offset,
                        "offset": int(operand_values(jump)[0]),
                        "jump_instruction_offset": jump.offset,
                    }
                )
        return starts

    def _find_replay_exits(
        self,
        scripts: dict[str, _Script],
        profile: AdvHdReplayProfile,
    ) -> dict[int, list[dict[str, Any]]]:
        exits: dict[int, list[dict[str, Any]]] = {}
        for script in scripts.values():
            instructions = script.instructions
            for index in range(len(instructions) - 2):
                replay_mode, replay_id, jump = instructions[index : index + 3]
                id_values = operand_values(replay_id)
                if not self._condition(
                    replay_mode,
                    operator=profile.scene_mode_operator,
                    variable=profile.scene_mode_variable,
                    value=profile.scene_mode_active_value,
                ):
                    continue
                if (
                    replay_id.opcode != 0x01
                    or id_values[0] != profile.selector_operator
                    or id_values[1] != profile.selector_variable
                ):
                    continue
                if jump.opcode != 0x07:
                    continue
                target = str(operand_values(jump)[0])
                if target.casefold() != profile.replay_return_target.casefold():
                    continue
                value = float(id_values[2])
                if not value.is_integer() or value <= 0:
                    continue
                exits.setdefault(int(value), []).append(
                    {
                        "archive": script.archive,
                        "script": script.entry,
                        "guard_offset": replay_mode.offset,
                        "offset": jump.offset,
                        # The replay-mode guard's unequal edge is the exact point
                        # where ordinary story playback continues after this Scene.
                        # Scene Review can therefore hand control back to a novel
                        # reader without replaying the Scene's own dialogue there.
                        "resume_offset": int(operand_values(replay_mode)[4]),
                        "target": target,
                    }
                )
        return exits

    def _find_script_path(
        self,
        scripts: dict[str, _Script],
        start_script: str,
        start_offset: int,
        exit_script: str,
        exit_offset: int,
    ) -> list[dict[str, Any]]:
        queue = deque([(start_script, [start_script])])
        seen = {start_script}
        found: list[str] | None = None
        while queue:
            key, path = queue.popleft()
            if key == exit_script:
                found = path
                break
            for target in self._story_targets(scripts[key].instructions):
                target_key = target.casefold()
                if target_key not in scripts or target_key in seen:
                    continue
                seen.add(target_key)
                queue.append((target_key, path + [target_key]))
        if found is None:
            raise ValueError(
                f"no story-script path from {start_script} to {exit_script}"
            )
        segments: list[dict[str, Any]] = []
        for index, key in enumerate(found):
            script = scripts[key]
            segment_start = start_offset if index == 0 else 0
            segment_end = (
                exit_offset
                if index + 1 == len(found)
                else script.instructions[-1].end_offset
            )
            segments.append(
                {
                    "script_key": key,
                    "archive": script.archive,
                    "script": script.entry,
                    "start_offset": segment_start,
                    "end_offset": segment_end,
                }
            )
        return segments

    def _story_targets(self, instructions: list[Ws2Instruction]) -> list[str]:
        targets: list[str] = []
        for instruction in instructions:
            if instruction.opcode == 0x07:
                target = str(operand_values(instruction)[0])
                targets.append(target)
            if instruction.opcode == 0x0F:
                for option in instruction.choices:
                    if option.target.opcode == 0x07:
                        targets.append(str(option.target.operands[0].value))
        return targets

    def _condition(
        self, instruction: Ws2Instruction, operator: int, variable: int, value: int
    ) -> bool:
        if instruction.opcode != 0x01:
            return False
        values = operand_values(instruction)
        return (
            values[0] == operator
            and values[1] == variable
            and float(values[2]) == float(value)
        )

    def _cg_family(self, asset: str) -> str:
        """Normalize render variants such as EV1002A and EV1002AL."""

        stem = Path(asset).stem.upper()
        match = re.match(r"^(EV\d+[A-Z])", stem)
        return match.group(1) if match else stem

    def _actual_entry(self, manifest, wanted: str) -> str:
        matches = [
            entry.name for entry in manifest.entries if entry.name.casefold() == wanted.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one {wanted} entry, found {len(matches)}")
        return matches[0]

    def _archive_with_entry(self, archive_paths, manifests, wanted: str) -> Path:
        matches = [
            path
            for path in archive_paths
            if any(
                entry.name.casefold() == wanted.casefold()
                for entry in manifests[path.name].entries
            )
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one Rio archive containing {wanted}")
        return matches[0]

    def _story_archive(self, archive_paths, manifests, replay_targets: list[str]) -> Path:
        target_keys = {target.casefold() for target in replay_targets}
        candidates = []
        for path in archive_paths:
            stems = {
                ws2_stem(entry.name)
                for entry in manifests[path.name].entries
                if entry.name.casefold().endswith(".ws2")
            }
            count = len(stems & target_keys)
            candidates.append((count, path.name.casefold(), path))
        if not candidates or max(candidates)[0] != len(target_keys):
            best = max(candidates)[0] if candidates else 0
            raise ValueError(
                f"no Rio archive resolves all replay targets ({best}/{len(target_keys)})"
            )
        return max(candidates, key=lambda item: (item[0], item[1]))[2]
