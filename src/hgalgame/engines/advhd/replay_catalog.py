from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hgalgame.engines.advhd.ws2_parser import Ws2Instruction


@dataclass(frozen=True)
class ReplayDispatch:
    replay_id: int
    script: str
    condition_offset: int
    dispatch_offset: int
    selector_variable: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GalleryAsset:
    group_id: int
    asset: str
    unlock_variable: int | None
    function_offset: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def operand_values(instruction: Ws2Instruction) -> list[Any]:
    return [operand.value for operand in instruction.operands]


def extract_replay_dispatches(
    instructions: list[Ws2Instruction],
) -> list[ReplayDispatch]:
    """Read the engine's replay-id to story-script dispatch table.

    The table is represented as an equality condition immediately followed by
    a cross-script jump. This is engine-authored replay metadata, not an asset
    naming heuristic.
    """

    dispatches: list[ReplayDispatch] = []
    for condition, jump in zip(instructions, instructions[1:]):
        if condition.opcode != 0x01 or jump.opcode != 0x07:
            continue
        values = operand_values(condition)
        target = str(operand_values(jump)[0])
        if values[0] != 130 or target.casefold() == "title":
            continue
        replay_value = float(values[2])
        if not replay_value.is_integer() or replay_value <= 0:
            continue
        dispatches.append(
            ReplayDispatch(
                replay_id=int(replay_value),
                script=target,
                condition_offset=condition.offset,
                dispatch_offset=jump.offset,
                selector_variable=int(values[1]),
            )
        )

    ids = [item.replay_id for item in dispatches]
    if not dispatches:
        raise ValueError("script contains no replay dispatches")
    if ids != list(range(1, len(ids) + 1)):
        raise ValueError(f"replay ids are not contiguous from 1: {ids}")
    if len({item.selector_variable for item in dispatches}) != 1:
        raise ValueError("replay dispatch table uses multiple selector variables")
    return dispatches


def extract_gallery_assets(
    instructions: list[Ws2Instruction],
) -> list[GalleryAsset]:
    """Read CG_PAGE openCgBrowser calls and their unlock flags."""

    current_group: int | None = None
    assets: list[GalleryAsset] = []
    for index, instruction in enumerate(instructions):
        values = operand_values(instruction)
        if instruction.opcode == 0x01 and values[0] == 130 and values[1] == 105:
            group_value = float(values[2])
            if group_value.is_integer() and group_value > 0:
                current_group = int(group_value)
            continue
        if (
            instruction.opcode != 0x1C
            or not values
            or str(values[0]).casefold() != "opencgbrowser"
        ):
            continue
        if current_group is None:
            raise ValueError("openCgBrowser appears before a gallery group condition")
        unlock_variable: int | None = None
        if index:
            previous = instructions[index - 1]
            previous_values = operand_values(previous)
            if previous.opcode == 0x01 and previous_values[0] == 2:
                unlock_variable = int(previous_values[1])
        assets.append(
            GalleryAsset(
                group_id=current_group,
                asset=str(values[1]),
                unlock_variable=unlock_variable,
                function_offset=instruction.offset,
            )
        )
    if not assets:
        raise ValueError("CG_PAGE contains no openCgBrowser assets")
    return assets


def group_gallery_assets(assets: list[GalleryAsset]) -> list[dict[str, Any]]:
    groups: dict[int, list[GalleryAsset]] = {}
    for item in assets:
        groups.setdefault(item.group_id, []).append(item)
    return [
        {
            "group_id": group_id,
            "assets": [item.to_dict() for item in groups[group_id]],
        }
        for group_id in sorted(groups)
    ]


def ws2_stem(name: str) -> str:
    return Path(name).stem.casefold()
