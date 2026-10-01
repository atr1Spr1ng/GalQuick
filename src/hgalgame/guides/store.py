from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hgalgame.output import write_json


class StrategyGuideStore:
    """Keep editable walkthrough annotations separate from extracted story data."""

    FORMAT_VERSION = "hgal-strategy-v1"
    FILE_NAME = "strategy.json"

    def attach(self, document: dict[str, Any], output_dir: Path) -> dict[str, Any]:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / self.FILE_NAME
        scaffold = self.scaffold(document)
        status = "loaded"
        error: str | None = None

        if not path.exists():
            write_json(path, scaffold)
            payload = scaffold
            status = "created"
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._validate(payload, document)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                payload = scaffold
                status = "invalid"
                error = str(exc)

        resolved = self._resolve(document, payload if status != "invalid" else {})
        document["strategy"] = {
            "format_version": self.FORMAT_VERSION,
            "status": status,
            "error": error,
            "choices": resolved,
        }
        return document["strategy"]

    def scaffold(self, document: dict[str, Any]) -> dict[str, Any]:
        choices: dict[str, Any] = {}
        for choice in document.get("graph", {}).get("choices", []):
            choice_id = str(choice.get("id", ""))
            options = {
                str(branch.get("option_id", "")): {
                    "text": str(branch.get("text") or ""),
                    "route": "",
                    "result": "",
                    "ending": "",
                    "recommended": False,
                    "tags": [],
                }
                for branch in choice.get("branches", [])
                if isinstance(branch, dict)
            }
            choices[choice_id] = {"note": "", "options": options}
        game = document.get("game", {})
        return {
            "format_version": self.FORMAT_VERSION,
            "game": {
                "title": str(game.get("title") or ""),
                "fingerprint": str(game.get("fingerprint") or ""),
            },
            "_help": {
                "route": "角色线或路线名称",
                "result": "选择结果说明（显示攻略后可见）",
                "ending": "Good/Bad End 等结局标签（显示攻略后可见）",
                "recommended": "设为 true 后在选项卡上显示攻略推荐",
                "tags": "可填写回收、好感度等简短标签",
            },
            "choices": choices,
        }

    def _validate(
        self,
        payload: Any,
        document: dict[str, Any],
    ) -> None:
        if not isinstance(payload, dict):
            raise ValueError("strategy.json 顶层必须是对象")
        if payload.get("format_version") != self.FORMAT_VERSION:
            raise ValueError("strategy.json 版本不受支持")
        game = payload.get("game")
        if not isinstance(game, dict):
            raise ValueError("strategy.json 缺少 game 信息")
        expected = str(document.get("game", {}).get("fingerprint") or "")
        if str(game.get("fingerprint") or "") != expected:
            raise ValueError("strategy.json 与当前游戏 fingerprint 不一致")
        if not isinstance(payload.get("choices"), dict):
            raise ValueError("strategy.json choices 必须是对象")

    def _resolve(
        self,
        document: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        stored_choices = payload.get("choices", {})
        if not isinstance(stored_choices, dict):
            stored_choices = {}
        result: dict[str, Any] = {}
        for choice in document.get("graph", {}).get("choices", []):
            choice_id = str(choice.get("id", ""))
            stored_choice = stored_choices.get(choice_id, {})
            if not isinstance(stored_choice, dict):
                stored_choice = {}
            stored_options = stored_choice.get("options", {})
            if not isinstance(stored_options, dict):
                stored_options = {}
            options: dict[str, Any] = {}
            for branch in choice.get("branches", []):
                option_id = str(branch.get("option_id", ""))
                stored = stored_options.get(option_id, {})
                if not isinstance(stored, dict):
                    stored = {}
                tags = stored.get("tags", [])
                if not isinstance(tags, list):
                    tags = []
                options[option_id] = {
                    "route": self._text(stored.get("route")),
                    "result": self._text(stored.get("result")),
                    "ending": self._text(stored.get("ending")),
                    "recommended": stored.get("recommended") is True,
                    "tags": [self._text(tag) for tag in tags[:8] if self._text(tag)],
                }
            result[choice_id] = {
                "note": self._text(stored_choice.get("note")),
                "options": options,
            }
        return result

    @staticmethod
    def _text(value: Any) -> str:
        return str(value).strip() if value is not None else ""
