from __future__ import annotations

import re
from typing import Any

from hgalgame.models import ScriptEvent


def _values(instruction) -> list[Any]:
    return [operand.value for operand in instruction.operands]


def _speaker_name(raw: str) -> str | None:
    if not raw:
        return None
    normalized = re.sub(r"^%L[A-Z]", "", raw)
    return normalized or None


def _message_text(raw: str) -> str:
    # %K (wait for input) and %P (page break) are AdvHD presentation
    # controls, not story text. They can occur separately around choices as
    # well as in the usual %K%P pair.
    text = re.sub(r"(?:%[KP])+$", "", raw)
    return text.replace("\\n", "\n")


def _image_role(asset: str) -> str:
    upper = asset.upper()
    if upper.startswith("EV"):
        return "event_cg_candidate"
    if upper.startswith("BG"):
        return "background"
    if upper.startswith("EF"):
        return "effect"
    return "image"


def _choice_target(option) -> dict[str, Any]:
    target_value = option.target.operands[0].value
    if option.target.opcode == 0x07:
        return {"type": "script", "script": target_value}
    return {"type": "offset", "offset": target_value}


def map_instructions_to_events(instructions) -> tuple[list[ScriptEvent], list[str]]:
    events: list[ScriptEvent] = []
    warnings: list[str] = []
    speaker_raw = ""
    pending_dialogue_voice: dict[str, Any] | None = None

    for instruction in instructions:
        opcode = instruction.opcode
        values = _values(instruction)

        if opcode == 0x15:
            speaker_raw = values[0]
            continue

        if opcode == 0x14:
            raw_text = values[2]
            data: dict[str, Any] = {
                "message_id": values[0],
                "speaker": _speaker_name(speaker_raw),
                "speaker_raw": speaker_raw or None,
                "channel": values[1],
                "text": _message_text(raw_text),
                "text_raw": raw_text,
            }
            if pending_dialogue_voice is not None:
                data["voice"] = pending_dialogue_voice
            events.append(ScriptEvent("dialogue", instruction.offset, data))
            pending_dialogue_voice = None
            continue

        if opcode == 0x0F:
            options = [
                {
                    "id": option.option_id,
                    "text": option.text,
                    "target": _choice_target(option),
                    "metadata": {
                        "unknown_u8": option.unknown_u8,
                        "unknown_i16": option.unknown_i16,
                    },
                }
                for option in instruction.choices
            ]
            events.append(
                ScriptEvent(
                    "choice",
                    instruction.offset,
                    {"option_count": len(options), "options": options},
                )
            )
            continue

        if opcode == 0x28:
            channel, asset = values[:2]
            channel_key = channel.casefold()
            if channel_key.startswith("char"):
                kind = "dialogue_voice"
                event_type = "voice"
            elif channel_key.startswith("bgv"):
                kind = "background_voice"
                event_type = "voice"
            else:
                kind = "sound_effect"
                event_type = "sound_effect"
            data = {"channel": channel, "asset": asset, "kind": kind}
            events.append(ScriptEvent(event_type, instruction.offset, data))
            if kind == "dialogue_voice":
                pending_dialogue_voice = {
                    "asset": asset,
                    "channel": channel,
                    "offset": instruction.offset,
                }
            continue

        if opcode == 0x1E:
            events.append(
                ScriptEvent("bgm", instruction.offset, {"channel": values[0], "asset": values[1]})
            )
            continue
        if opcode == 0x1F:
            events.append(ScriptEvent("bgm_stop", instruction.offset, {"channel": values[0]}))
            continue
        if opcode == 0x29:
            events.append(ScriptEvent("sound_stop", instruction.offset, {"channel": values[0]}))
            continue

        if opcode == 0x33:
            events.append(
                ScriptEvent(
                    "image",
                    instruction.offset,
                    {
                        "layer": values[0],
                        "asset": values[1],
                        "role": _image_role(values[1]),
                    },
                )
            )
            continue
        if opcode == 0x34:
            events.append(
                ScriptEvent(
                    "sprite",
                    instruction.offset,
                    {"layer": values[0], "asset": values[1], "role": "character_sprite"},
                )
            )
            continue
        if opcode == 0x35:
            events.append(
                ScriptEvent("movie", instruction.offset, {"channel": values[0], "asset": values[1]})
            )
            continue
        if opcode == 0x66:
            events.append(ScriptEvent("mask", instruction.offset, {"asset": values[0]}))
            continue

        if opcode == 0x04:
            events.append(ScriptEvent("script_call", instruction.offset, {"target": values[0]}))
            continue
        if opcode == 0x07:
            events.append(ScriptEvent("script_jump", instruction.offset, {"target": values[0]}))
            continue
        if opcode in {0x02, 0x06}:
            events.append(
                ScriptEvent(
                    "jump",
                    instruction.offset,
                    {"target_offset": values[0], "variant": instruction.name},
                )
            )
            continue
        if opcode == 0x01:
            events.append(
                ScriptEvent(
                    "condition",
                    instruction.offset,
                    {
                        "operator": values[0],
                        "variable": values[1],
                        "value": values[2],
                        "targets": [values[3], values[4]],
                    },
                )
            )

    if pending_dialogue_voice is not None:
        warnings.append(
            f"unpaired dialogue voice at 0x{pending_dialogue_voice['offset']:08X}"
        )
    return events, warnings
