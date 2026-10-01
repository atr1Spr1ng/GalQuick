from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from hgalgame.engines.advhd.ws2_reader import Ws2Reader
from hgalgame.models import ScriptParseResult


class Ws2ParseError(ValueError):
    def __init__(self, script_name: str, offset: int, detail: str) -> None:
        super().__init__(f"{script_name}: offset 0x{offset:08X}: {detail}")
        self.script_name = script_name
        self.offset = offset
        self.detail = detail


@dataclass(frozen=True)
class Ws2Operand:
    kind: str
    value: Any
    offset: int


@dataclass(frozen=True)
class Ws2ChoiceTarget:
    offset: int
    opcode: int
    operands: tuple[Ws2Operand, ...]


@dataclass(frozen=True)
class Ws2ChoiceOption:
    option_id: int
    text: str
    unknown_u8: int
    unknown_i16: int
    target: Ws2ChoiceTarget


@dataclass(frozen=True)
class Ws2Instruction:
    offset: int
    end_offset: int
    opcode: int
    name: str
    operands: tuple[Ws2Operand, ...]
    choices: tuple[Ws2ChoiceOption, ...] = field(default_factory=tuple)


# AdvHD WS2 V3 signatures observed across both the story scripts and the
# engine-provided system scripts used by the current samples. Signatures are
# format facts, expressed independently: B=u8, H=i16, I=i32, A=address/i32,
# F=f32, S=CP932 C string, X[]=u8-counted values of kind X.
V3_SIGNATURES: dict[int, tuple[str, ...]] = {
    0x00: (),
    0x01: ("B", "H", "F", "A", "A"),
    0x02: ("A",),
    0x04: ("S",),
    0x05: (),
    0x06: ("A",),
    0x07: ("S",),
    0x08: ("B",),
    0x09: ("B", "H", "F"),
    0x0B: ("H", "B"),
    0x0D: ("H", "H", "F"),
    0x0E: ("H", "H", "B"),
    0x0F: ("B",),  # followed by option records; handled specially
    0x11: ("S", "B", "F"),
    0x12: ("S", "B", "S"),
    0x13: (),
    0x14: ("I", "S", "S", "B"),
    0x15: ("S", "B"),
    0x16: ("B", "B"),
    0x17: (),
    0x1A: ("S",),
    0x1B: ("B",),
    0x1C: ("S", "S", "H", "B"),
    0x1D: ("H",),
    0x1E: ("S", "S", "F", "F", "H", "H", "B", "F"),
    0x1F: ("S", "F"),
    0x28: ("S", "S", "F", "F", "H", "H", "B", "H", "H", "B", "F"),
    0x29: ("S", "F"),
    0x2E: (),
    0x32: ("S",),
    0x33: ("S", "S", "B", "B"),
    0x34: ("S", "S", "B", "B"),
    0x35: ("S", "S", "B", "B", "B"),
    0x37: ("S",),
    0x38: ("S", "B"),
    0x39: ("S", "B", "B", "H[]"),
    0x3A: ("S", "B", "B"),
    0x3D: ("H",),
    0x3E: (),
    # Opcode 0x3F is an engine layer-order declaration: a NUL-separated list
    # that runs until the next opcode. It is handled specially below.
    0x3F: (),
    0x40: ("S", "S", "B"),
    0x42: ("S", "H"),
    0x45: ("S", "H", "F", "F", "F", "F"),
    0x46: ("S", "H", "B", "F", "F", "F", "F"),
    0x47: ("S", "S", "H", "B", "B", "F", "F", "F", "F", "F", "H", "F"),
    0x48: ("S", "S", "H", "B", "B", "S"),
    0x53: ("S", "S"),
    0x57: ("S", "H"),
    0x58: ("S", "S"),
    0x5B: ("S", "H", "B"),
    0x64: ("B",),
    0x65: ("H", "B", "F", "F", "B", "S"),
    0x66: ("S",),
    0x67: ("B", "B", "H", "F", "F", "F", "F", "F", "B"),
    0x68: ("B",),
    0x6E: ("S", "S"),
    0x6F: ("S",),
    0x71: (),
    0x73: ("S", "S", "H"),
    0x75: ("S", "S"),
    0xFB: ("B",),
    0xFC: ("H",),
    0xFD: (),
    0xFF: (),
}

# Kept as an import-compatible alias for callers written during phase 2.
V3_STORY_SIGNATURES = V3_SIGNATURES


OPCODE_NAMES: dict[int, str] = {
    0x01: "condition",
    0x02: "jump2",
    0x04: "run_script",
    0x06: "jump",
    0x07: "next_script",
    0x0F: "choice",
    0x14: "message",
    0x15: "display_name",
    0x1C: "function",
    0x1E: "play_bgm",
    0x1F: "stop_bgm",
    0x28: "play_sound",
    0x29: "stop_sound",
    0x33: "set_background",
    0x34: "set_pna",
    0x35: "play_movie",
    0x39: "display_character",
    0x66: "set_mask",
    0xFF: "file_end",
}


class _Cursor:
    def __init__(self, script_name: str, data: bytes, limit: int) -> None:
        self.script_name = script_name
        self.data = data
        self.limit = limit
        self.offset = 0

    def _take(self, length: int) -> tuple[int, bytes]:
        start = self.offset
        if length < 0 or start + length > self.limit:
            raise Ws2ParseError(
                self.script_name,
                start,
                f"need {length} bytes but script body ends at 0x{self.limit:08X}",
            )
        self.offset += length
        return start, self.data[start : start + length]

    def read_operand(self, kind: str) -> Ws2Operand:
        if kind == "B":
            offset, raw = self._take(1)
            return Ws2Operand(kind, raw[0], offset)
        if kind == "H":
            offset, raw = self._take(2)
            return Ws2Operand(kind, struct.unpack("<h", raw)[0], offset)
        if kind in {"I", "A"}:
            offset, raw = self._take(4)
            return Ws2Operand(kind, struct.unpack("<i", raw)[0], offset)
        if kind == "F":
            offset, raw = self._take(4)
            return Ws2Operand(kind, struct.unpack("<f", raw)[0], offset)
        if kind == "S":
            start = self.offset
            end = self.data.find(b"\x00", start, self.limit)
            if end < 0:
                raise Ws2ParseError(self.script_name, start, "unterminated CP932 string")
            raw = self.data[start:end]
            self.offset = end + 1
            try:
                value = raw.decode("cp932")
            except UnicodeDecodeError as exc:
                raise Ws2ParseError(
                    self.script_name, start, f"invalid CP932 string: {exc}"
                ) from exc
            return Ws2Operand(kind, value, start)
        raise Ws2ParseError(self.script_name, self.offset, f"unknown operand kind {kind}")

    def read_operands(self, signature: tuple[str, ...]) -> tuple[Ws2Operand, ...]:
        operands: list[Ws2Operand] = []
        for kind in signature:
            if kind.endswith("[]"):
                element_kind = kind[:-2]
                count = self.read_operand("B")
                values = [self.read_operand(element_kind).value for _ in range(count.value)]
                operands.append(Ws2Operand(kind, values, count.offset))
            else:
                operands.append(self.read_operand(kind))
        return tuple(operands)


class Ws2Parser:
    BODY_TRAILER_SIZE = 8
    FORMAT_VERSION = "advhd-ws2-v3"

    def __init__(self) -> None:
        self.reader = Ws2Reader()

    def parse(self, script_name: str, payload: bytes) -> ScriptParseResult:
        instructions, trailer, transform = self.parse_instructions(script_name, payload)

        # Imported lazily so the mapper can use the instruction data classes
        # without making format parsing depend on business event definitions.
        from hgalgame.engines.advhd.event_mapper import map_instructions_to_events

        events, warnings = map_instructions_to_events(instructions)
        return ScriptParseResult(
            script_name=script_name,
            format_version=self.FORMAT_VERSION,
            transform=transform,
            encoding="cp932",
            byte_length=len(payload),
            instruction_count=len(instructions),
            trailer_hex=trailer.hex(" "),
            events=events,
            warnings=warnings,
        )

    def parse_instructions(
        self, script_name: str, payload: bytes
    ) -> tuple[list[Ws2Instruction], bytes, str]:
        """Decode and strictly parse a WS2 payload without mapping events.

        The text-marker probe is normally enough to identify the byte transform,
        but tiny system scripts such as FlagInit contain no useful strings. A
        complete opcode parse is therefore the final transform discriminator.
        """

        probe = self.reader.probe(script_name, payload)
        transforms = [probe.transform]
        transforms.extend(
            name for name, _ in self.reader.TRANSFORMS if name != probe.transform
        )
        failures: list[str] = []
        for transform in transforms:
            decoded = self.reader.decode(payload, transform)
            # The body is transformed; the two allocation counts at EOF are
            # stored as plain little-endian DWORDs in both file variants.
            decoded = decoded[:-self.BODY_TRAILER_SIZE] + payload[-self.BODY_TRAILER_SIZE:]
            try:
                instructions, trailer = self.parse_decoded(script_name, decoded)
            except Ws2ParseError as exc:
                failures.append(f"{transform}: {exc.detail} at 0x{exc.offset:08X}")
                continue
            return instructions, trailer, transform
        raise Ws2ParseError(
            script_name,
            0,
            "no supported WS2 transform produced a valid V3 script ("
            + "; ".join(failures)
            + ")",
        )

    def parse_decoded(
        self, script_name: str, decoded: bytes
    ) -> tuple[list[Ws2Instruction], bytes]:
        if len(decoded) < self.BODY_TRAILER_SIZE + 1:
            raise Ws2ParseError(script_name, 0, "script is too short")
        body_end = len(decoded) - self.BODY_TRAILER_SIZE
        cursor = _Cursor(script_name, decoded, body_end)
        instructions: list[Ws2Instruction] = []

        while cursor.offset < body_end:
            start, raw_opcode = cursor._take(1)
            opcode = raw_opcode[0]
            signature = V3_SIGNATURES.get(opcode)
            if signature is None:
                raise Ws2ParseError(script_name, start, f"unknown V3 opcode 0x{opcode:02X}")
            if opcode == 0x3F:
                operands = self._read_layer_order(cursor)
            else:
                operands = cursor.read_operands(signature)
            choices: tuple[Ws2ChoiceOption, ...] = ()
            if opcode == 0x0F:
                choices = self._read_choices(cursor, operands[0].value)
            instructions.append(
                Ws2Instruction(
                    offset=start,
                    end_offset=cursor.offset,
                    opcode=opcode,
                    name=OPCODE_NAMES.get(opcode, f"op_{opcode:02x}"),
                    operands=operands,
                    choices=choices,
                )
            )

        if cursor.offset != body_end:
            raise Ws2ParseError(
                script_name,
                cursor.offset,
                f"parser did not stop at body end 0x{body_end:08X}",
            )
        if not instructions or instructions[-1].opcode != 0xFF:
            raise Ws2ParseError(script_name, body_end, "body does not end with opcode 0xFF")
        return instructions, decoded[body_end:]

    def _read_layer_order(self, cursor: _Cursor) -> tuple[Ws2Operand, ...]:
        """Read opcode 0x3F's NUL-separated list terminated by opcode 0x05.

        AdvHD writes no explicit element count here. Layer names in this V3
        layout are non-empty CP932 strings, so the standalone 0x05 byte is an
        unambiguous terminator and remains available to the main loop.
        """

        start = cursor.offset
        values: list[str] = []
        while cursor.offset < cursor.limit and cursor.data[cursor.offset] != 0x05:
            values.append(cursor.read_operand("S").value)
        if cursor.offset >= cursor.limit:
            raise Ws2ParseError(cursor.script_name, start, "unterminated layer-order list")
        return (Ws2Operand("S[]", values, start),)

    def _read_choices(
        self, cursor: _Cursor, count: int
    ) -> tuple[Ws2ChoiceOption, ...]:
        choices: list[Ws2ChoiceOption] = []
        for _ in range(count):
            fields = cursor.read_operands(("H", "S", "B", "H"))
            target_offset, raw_opcode = cursor._take(1)
            target_opcode = raw_opcode[0]
            if target_opcode not in {0x06, 0x07}:
                raise Ws2ParseError(
                    cursor.script_name,
                    target_offset,
                    f"unsupported choice target opcode 0x{target_opcode:02X}",
                )
            target_operands = cursor.read_operands(V3_SIGNATURES[target_opcode])
            choices.append(
                Ws2ChoiceOption(
                    option_id=fields[0].value,
                    text=fields[1].value,
                    unknown_u8=fields[2].value,
                    unknown_i16=fields[3].value,
                    target=Ws2ChoiceTarget(
                        offset=target_offset,
                        opcode=target_opcode,
                        operands=target_operands,
                    ),
                )
            )
        return tuple(choices)
