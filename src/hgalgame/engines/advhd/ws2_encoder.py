from __future__ import annotations

import struct

from hgalgame.engines.advhd.ws2_parser import Ws2Parser


def _rotate_left_2(data: bytes) -> bytes:
    return bytes(((byte << 2) & 0xFF) | (byte >> 6) for byte in data)


class Ws2Encoder:
    """Encode the small, audited WS2 subset used by generated launchers."""

    def continuous_replay_return(
        self,
        original_payload: bytes,
        playlist: list[int] | tuple[int, ...],
        *,
        script_name: str,
        selector_variable: int,
        scene_mode_variable: int,
        scene_mode_inactive_value: int,
        event_mode_variable: int | None,
        event_mode_inactive_value: int | None,
        replay_target: str,
        menu_target: str,
    ) -> bytes:
        """Build the return script for an explicit ordered Scene playlist."""

        sequence = self._validate_playlist(playlist)
        self._validate_variable(selector_variable, "selector_variable")
        self._validate_variable(scene_mode_variable, "scene_mode_variable")
        self._validate_byte(scene_mode_inactive_value, "scene_mode_inactive_value")
        if event_mode_variable is not None:
            self._validate_variable(event_mode_variable, "event_mode_variable")
            if event_mode_inactive_value is None:
                raise ValueError(
                    "event_mode_inactive_value is required with event_mode_variable"
                )
            self._validate_byte(
                event_mode_inactive_value, "event_mode_inactive_value"
            )
        elif event_mode_inactive_value is not None:
            raise ValueError(
                "event_mode_variable is required with event_mode_inactive_value"
            )
        if not replay_target or not menu_target:
            raise ValueError("replay_target and menu_target must be non-empty")

        parser = Ws2Parser()
        instructions, trailer, transform = parser.parse_instructions(
            script_name, original_payload
        )
        route_index = next(
            (
                index
                for index, instruction in enumerate(instructions)
                if instruction.opcode in {0x01, 0x02, 0x06, 0x07, 0x0F, 0xFF}
            ),
            None,
        )
        if route_index is None or route_index == 0:
            raise ValueError(
                f"{script_name} has no preservable cleanup prefix before routing"
            )
        route_offset = instructions[route_index].offset
        decoded = parser.reader.decode(original_payload, transform)
        body = bytearray(decoded[:route_offset])

        # Use only the stock, verified assignment variant (0). Each condition's
        # true edge falls through to an explicit next-id assignment; its false
        # edge advances to the next condition.
        condition_offsets: list[int] = []
        for current, following in zip(sequence, sequence[1:]):
            condition_offsets.append(len(body))
            body.extend(
                self._op(
                    0x01,
                    "BHFAA",
                    130,
                    selector_variable,
                    float(current),
                    0,
                    0,
                )
            )
            body.extend(
                self._op(0x09, "BHF", 0, selector_variable, float(following))
            )
            body.extend(self._op(0x07, "S", replay_target))
        finish_offset = len(body)
        body.extend(
            self._op(
                0x0B,
                "HB",
                scene_mode_variable,
                scene_mode_inactive_value,
            )
        )
        if event_mode_variable is not None:
            body.extend(
                self._op(
                    0x0B,
                    "HB",
                    event_mode_variable,
                    event_mode_inactive_value,
                )
            )
        body.extend(self._op(0x09, "BHF", 0, selector_variable, 0.0))
        body.extend(self._op(0x07, "S", menu_target))
        body.extend(self._op(0xFF, ""))

        for index, condition_offset in enumerate(condition_offsets):
            next_condition = (
                condition_offsets[index + 1]
                if index + 1 < len(condition_offsets)
                else finish_offset
            )
            # Equality true falls through (first address 0); false jumps to the
            # next id check via the second address.
            struct.pack_into("<i", body, condition_offset + 12, next_condition)
        decoded_output = bytes(body) + trailer
        if transform == "rotate_right_2":
            return _rotate_left_2(decoded_output)
        if transform == "none":
            return decoded_output
        raise ValueError(f"unsupported output transform: {transform}")

    def continuous_replay_start(
        self,
        original_payload: bytes,
        *,
        first_replay_id: int,
        selector_variable: int,
        scene_mode_variable: int,
        scene_mode_active_value: int,
        event_mode_variable: int | None,
        event_mode_inactive_value: int | None,
        replay_target: str,
        menu_target: str,
    ) -> bytes:
        """Preserve stock startup, then enter the first selected Scene."""

        if first_replay_id <= 0:
            raise ValueError("first_replay_id must be positive")
        self._validate_variable(selector_variable, "selector_variable")
        self._validate_variable(scene_mode_variable, "scene_mode_variable")
        self._validate_byte(scene_mode_active_value, "scene_mode_active_value")
        if event_mode_variable is not None:
            self._validate_variable(event_mode_variable, "event_mode_variable")
            if event_mode_inactive_value is None:
                raise ValueError(
                    "event_mode_inactive_value is required with event_mode_variable"
                )
            self._validate_byte(
                event_mode_inactive_value, "event_mode_inactive_value"
            )
        elif event_mode_inactive_value is not None:
            raise ValueError(
                "event_mode_variable is required with event_mode_inactive_value"
            )
        if not replay_target or not menu_target:
            raise ValueError("replay_target and menu_target must be non-empty")

        parser = Ws2Parser()
        instructions, trailer, transform = parser.parse_instructions(
            "start.ws2", original_payload
        )
        candidates = [
            (index, instruction)
            for index, instruction in enumerate(instructions)
            if instruction.opcode == 0x07
            and str(instruction.operands[0].value).casefold()
            == menu_target.casefold()
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"start.ws2 must contain one {menu_target} jump, found {len(candidates)}"
            )
        index, jump = candidates[0]
        if index + 1 != len(instructions) - 1 or instructions[-1].opcode != 0xFF:
            raise ValueError(
                f"start.ws2 {menu_target} jump is not immediately before file end"
            )

        decoded = parser.reader.decode(original_payload, transform)
        body = bytearray(decoded[: jump.offset])
        body.extend(
            self._op(
                0x0B,
                "HB",
                scene_mode_variable,
                scene_mode_active_value,
            )
        )
        if event_mode_variable is not None:
            body.extend(
                self._op(
                    0x0B,
                    "HB",
                    event_mode_variable,
                    event_mode_inactive_value,
                )
            )
        body.extend(
            self._op(0x09, "BHF", 0, selector_variable, float(first_replay_id))
        )
        body.extend(self._op(0x07, "S", replay_target))
        body.extend(self._op(0xFF, ""))
        decoded_output = bytes(body) + trailer
        if transform == "rotate_right_2":
            return _rotate_left_2(decoded_output)
        if transform == "none":
            return decoded_output
        raise ValueError(f"unsupported output transform: {transform}")

    def scene_session_start(
        self,
        original_payload: bytes,
        *,
        controller_target: str,
        menu_target: str,
    ) -> bytes:
        """Preserve stock bootstrap and enter the generated idle controller."""

        if not controller_target or not menu_target:
            raise ValueError("controller_target and menu_target must be non-empty")
        parser = Ws2Parser()
        instructions, trailer, transform = parser.parse_instructions(
            "start.ws2", original_payload
        )
        candidates = [
            (index, instruction)
            for index, instruction in enumerate(instructions)
            if instruction.opcode == 0x07
            and str(instruction.operands[0].value).casefold()
            == menu_target.casefold()
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"start.ws2 must contain one {menu_target} jump, found {len(candidates)}"
            )
        index, jump = candidates[0]
        if index + 1 != len(instructions) - 1 or instructions[-1].opcode != 0xFF:
            raise ValueError(
                f"start.ws2 {menu_target} jump is not immediately before file end"
            )
        decoded = parser.reader.decode(original_payload, transform)
        body = bytearray(decoded[: jump.offset])
        body.extend(self._op(0x07, "S", controller_target))
        body.extend(self._op(0xFF, ""))
        return self._encode_like(bytes(body) + trailer, transform)

    def scene_session_return(
        self,
        original_payload: bytes,
        *,
        script_name: str,
        selector_variable: int,
        scene_mode_variable: int,
        scene_mode_inactive_value: int,
        event_mode_variable: int | None,
        event_mode_inactive_value: int | None,
        controller_target: str,
    ) -> bytes:
        """Preserve stock Scene cleanup and return to the idle controller."""

        self._validate_variable(selector_variable, "selector_variable")
        self._validate_variable(scene_mode_variable, "scene_mode_variable")
        self._validate_byte(scene_mode_inactive_value, "scene_mode_inactive_value")
        if event_mode_variable is not None:
            self._validate_variable(event_mode_variable, "event_mode_variable")
            if event_mode_inactive_value is None:
                raise ValueError(
                    "event_mode_inactive_value is required with event_mode_variable"
                )
            self._validate_byte(
                event_mode_inactive_value, "event_mode_inactive_value"
            )
        elif event_mode_inactive_value is not None:
            raise ValueError(
                "event_mode_variable is required with event_mode_inactive_value"
            )
        if not controller_target:
            raise ValueError("controller_target must be non-empty")

        parser = Ws2Parser()
        instructions, trailer, transform = parser.parse_instructions(
            script_name, original_payload
        )
        route_index = next(
            (
                index
                for index, instruction in enumerate(instructions)
                if instruction.opcode in {0x01, 0x02, 0x06, 0x07, 0x0F, 0xFF}
            ),
            None,
        )
        if route_index is None or route_index == 0:
            raise ValueError(
                f"{script_name} has no preservable cleanup prefix before routing"
            )
        route_offset = instructions[route_index].offset
        decoded = parser.reader.decode(original_payload, transform)
        body = bytearray(decoded[:route_offset])
        body.extend(
            self._op(0x0B, "HB", scene_mode_variable, scene_mode_inactive_value)
        )
        if event_mode_variable is not None:
            body.extend(
                self._op(
                    0x0B,
                    "HB",
                    event_mode_variable,
                    event_mode_inactive_value,
                )
            )
        body.extend(self._op(0x09, "BHF", 0, selector_variable, 0.0))
        body.extend(self._op(0x07, "S", controller_target))
        body.extend(self._op(0xFF, ""))
        return self._encode_like(bytes(body) + trailer, transform)

    def scene_session_controller(
        self,
        template_payload: bytes,
        replay_ids: list[int] | tuple[int, ...],
        *,
        script_name: str,
        page_size: int,
        selector_variable: int,
        scene_mode_variable: int,
        scene_mode_active_value: int,
        scene_mode_inactive_value: int,
        event_mode_variable: int | None,
        event_mode_inactive_value: int | None,
        replay_target: str,
        menu_target: str,
    ) -> bytes:
        """Create a keyboard-driven idle menu for a persistent Scene session."""

        sequence = self._validate_playlist(replay_ids)
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 9:
            raise ValueError("page_size must be between 1 and 9")
        self._validate_variable(selector_variable, "selector_variable")
        self._validate_variable(scene_mode_variable, "scene_mode_variable")
        self._validate_byte(scene_mode_active_value, "scene_mode_active_value")
        self._validate_byte(scene_mode_inactive_value, "scene_mode_inactive_value")
        if event_mode_variable is not None:
            self._validate_variable(event_mode_variable, "event_mode_variable")
            if event_mode_inactive_value is None:
                raise ValueError(
                    "event_mode_inactive_value is required with event_mode_variable"
                )
            self._validate_byte(
                event_mode_inactive_value, "event_mode_inactive_value"
            )
        elif event_mode_inactive_value is not None:
            raise ValueError(
                "event_mode_variable is required with event_mode_inactive_value"
            )
        if not replay_target or not menu_target:
            raise ValueError("replay_target and menu_target must be non-empty")

        parser = Ws2Parser()
        _, trailer, transform = parser.parse_instructions(
            script_name, template_payload
        )
        body = bytearray()
        labels: dict[str, int] = {}
        patches: list[tuple[int, str]] = []

        def mark(label: str) -> None:
            if label in labels:
                raise ValueError(f"duplicate generated WS2 label: {label}")
            labels[label] = len(body)

        def local_choice(options: list[tuple[str, str]], tag: str) -> None:
            # This UI gates mouse confirmation on MenuInf.enabled, unlike
            # keyboard confirmation (select_enabled). A standalone menu can
            # otherwise highlight options but ignore clicks after the message
            # window has been closed. Re-enable it on every menu entry.
            body.extend(self._op(0x16, "BB", 1, 0))
            body.extend(self._op(0x1C, "SSHB", "HGalControl", tag, 0, 1))
            body.extend(self._op(0x0E, "HHB", 11, len(options), 1))
            body.extend(self._op(0x0F, "B", len(options)))
            for index, (text, target_label) in enumerate(options):
                body.extend(struct.pack("<h", index + 1))
                body.extend(text.encode("cp932") + b"\x00")
                body.extend(struct.pack("<Bh", 0, 11 + index))
                body.append(0x06)
                patches.append((len(body), target_label))
                body.extend(struct.pack("<i", 0))

        pages = [
            sequence[index : index + page_size]
            for index in range(0, len(sequence), page_size)
        ]
        mark("root")
        # Fast-forward can remain active briefly after a Scene returns. Keep
        # the default item harmless so it cannot replay Scene 01 on its own.
        root_options = [("HGal Waiting...", "root")]
        root_options.extend(
            (f"Scene {page[0]:02d}-{page[-1]:02d}", f"page_{index}")
            for index, page in enumerate(pages)
        )
        root_options.append(("Exit Scene Review", "exit"))
        local_choice(root_options, "root")

        for page_index, page in enumerate(pages):
            mark(f"page_{page_index}")
            options = [
                (f"Scene {replay_id:02d}", f"play_{replay_id}")
                for replay_id in page
            ]
            options.append(("Back", "root"))
            local_choice(options, f"page_{page_index}")

        for replay_id in sequence:
            mark(f"play_{replay_id}")
            body.extend(
                self._op(
                    0x0B,
                    "HB",
                    scene_mode_variable,
                    scene_mode_active_value,
                )
            )
            if event_mode_variable is not None:
                body.extend(
                    self._op(
                        0x0B,
                        "HB",
                        event_mode_variable,
                        event_mode_inactive_value,
                    )
                )
            body.extend(
                self._op(0x09, "BHF", 0, selector_variable, float(replay_id))
            )
            body.extend(self._op(0x07, "S", replay_target))

        mark("exit")
        body.extend(
            self._op(0x0B, "HB", scene_mode_variable, scene_mode_inactive_value)
        )
        if event_mode_variable is not None:
            body.extend(
                self._op(
                    0x0B,
                    "HB",
                    event_mode_variable,
                    event_mode_inactive_value,
                )
            )
        body.extend(self._op(0x09, "BHF", 0, selector_variable, 0.0))
        body.extend(self._op(0x07, "S", menu_target))
        body.extend(self._op(0xFF, ""))

        for offset, label in patches:
            if label not in labels:
                raise ValueError(f"unknown generated WS2 label: {label}")
            struct.pack_into("<i", body, offset, labels[label])
        return self._encode_like(bytes(body) + trailer, transform)

    def _validate_playlist(
        self, playlist: list[int] | tuple[int, ...]
    ) -> tuple[int, ...]:
        sequence = tuple(playlist)
        if not sequence:
            raise ValueError("playlist must contain at least one replay id")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in sequence):
            raise ValueError("playlist replay ids must be integers")
        if any(value <= 0 for value in sequence):
            raise ValueError("playlist replay ids must be positive")
        if len(set(sequence)) != len(sequence):
            raise ValueError("playlist cannot contain duplicate replay ids")
        return sequence

    def _validate_variable(self, value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not -32768 <= value <= 32767:
            raise ValueError(f"{label} must fit an AdvHD signed 16-bit variable id")

    def _validate_byte(self, value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError(f"{label} must fit an AdvHD byte value")

    def _encode_like(self, decoded_output: bytes, transform: str) -> bytes:
        # The last two DWORDs are unrotated allocation counts, not padding.
        # Reusing an empty template's zero counts leaves the engine's read
        # history bitsets unallocated even when the body contains choices.
        instructions, _ = Ws2Parser().parse_decoded("generated.ws2", decoded_output)
        dialogue_count = sum(item.opcode == 0x14 for item in instructions)
        choice_count = sum(len(item.choices) for item in instructions)
        trailer = struct.pack("<II", dialogue_count, choice_count)
        body = decoded_output[:-8]
        if transform == "rotate_right_2":
            return _rotate_left_2(body) + trailer
        if transform == "none":
            return body + trailer
        raise ValueError(f"unsupported output transform: {transform}")

    def _op(self, opcode: int, signature: str, *values) -> bytes:
        if len(signature) != len(values):
            raise ValueError("signature/value length mismatch")
        output = bytearray([opcode])
        for kind, value in zip(signature, values, strict=True):
            if kind == "B":
                output.extend(struct.pack("<B", value))
            elif kind == "H":
                output.extend(struct.pack("<h", value))
            elif kind == "F":
                output.extend(struct.pack("<f", value))
            elif kind == "A":
                output.extend(struct.pack("<i", value))
            elif kind == "S":
                output.extend(str(value).encode("cp932") + b"\x00")
            else:
                raise ValueError(f"unsupported encoder operand kind: {kind}")
        return bytes(output)
