from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from hgalgame.detection import EngineDetector
from hgalgame.engines.registry import default_engine_adapters
from hgalgame.engines.advhd import (
    AdvHdArcReader,
    AdvHdFormatError,
    AdvHdReportBuilder,
    Ws2Reader,
    Ws2ParseError,
    Ws2Parser,
    AdvHdReplayProfiler,
)
from hgalgame.graph import AdvHdReplaySceneIndexer, AdvHdSceneIndexer
from hgalgame.output import write_json
from hgalgame.runtime import AdvHdContinuousReplayBuilder, AdvHdInPlaceReplayRunner
from hgalgame.frontend import StoryBrowser
from hgalgame.story import EngineAdapterRegistry
from hgalgame.project_manager import GameProjectManager
from hgalgame.translation import ParagraphTranslator, TranslationConfig
from hgalgame.translation_library import TranslationLibrary
from hgalgame.batch_translation import WholeGameTranslator
from hgalgame.translation_tasks import export_task_package, import_result_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hgal",
        description="Inspect Galgame engines and build verified original-engine Scene Replay launchers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect", help="detect the engine of a game directory")
    detect.add_argument("game_dir", type=Path)
    detect.add_argument("--json", action="store_true", help="emit JSON")

    manifest = subparsers.add_parser(
        "arc-list", help="list an AdvHD ARC directory without extracting data"
    )
    manifest.add_argument("archive", type=Path)
    manifest.add_argument("--suffix", help="only include names ending with this suffix")
    manifest.add_argument("--json", action="store_true", help="emit JSON")

    probe = subparsers.add_parser(
        "ws2-probe", help="probe one WS2 entry in memory and emit a format report"
    )
    probe.add_argument("archive", type=Path)
    probe.add_argument("entry_name")
    probe.add_argument("--json", action="store_true", help="emit JSON")

    report = subparsers.add_parser(
        "advhd-report", help="inspect all Rio WS2 entries and emit a read-only report"
    )
    report.add_argument("game_dir", type=Path)
    report.add_argument("--json", action="store_true", help="emit JSON")

    events = subparsers.add_parser(
        "ws2-events", help="parse one story WS2 entry into ordered events"
    )
    events.add_argument("archive", type=Path)
    events.add_argument("entry_name")
    events.add_argument(
        "--type",
        dest="event_types",
        action="append",
        help="only emit this event type; repeat to include multiple types",
    )
    events.add_argument(
        "--summary", action="store_true", help="omit the event array and print counts only"
    )
    events.add_argument(
        "--output", type=Path, help="write UTF-8 JSON to this file instead of stdout"
    )

    index = subparsers.add_parser(
        "advhd-index", help="build a game-level story and event index"
    )
    index.add_argument("game_dir", type=Path)
    index.add_argument(
        "--include-events",
        action="store_true",
        help="include every event in each script record",
    )
    index.add_argument(
        "--summary", action="store_true", help="only emit metadata and aggregate counts"
    )
    index.add_argument(
        "--output", type=Path, help="write UTF-8 JSON to this file instead of stdout"
    )

    scenes = subparsers.add_parser(
        "advhd-scenes",
        help="build exact Scene Review ranges from the game's replay system",
    )
    scenes.add_argument("game_dir", type=Path)
    scenes.add_argument(
        "--no-events",
        action="store_true",
        help="omit the ordered text/audio/image event timeline",
    )
    scenes.add_argument(
        "--summary", action="store_true", help="only emit evidence and aggregate counts"
    )
    scenes.add_argument(
        "--output", type=Path, help="write UTF-8 JSON to this file instead of stdout"
    )

    replay = subparsers.add_parser(
        "advhd-build-replay",
        help="build a verified in-place continuous Scene Replay launcher",
    )
    replay.add_argument("game_dir", type=Path)
    replay.add_argument("output_dir", type=Path)
    selection = replay.add_mutually_exclusive_group()
    selection.add_argument("--scene", type=int, help="play exactly one replay id")
    selection.add_argument(
        "--playlist",
        help="play comma-separated replay ids in this exact order, e.g. 3,8,12",
    )
    selection.add_argument(
        "--from",
        dest="from_scene",
        type=int,
        help="first replay id of an inclusive range; requires --to",
    )
    selection.add_argument(
        "--all", action="store_true", help="play every replay id (the default)"
    )
    replay.add_argument(
        "--to",
        dest="to_scene",
        type=int,
        help="last replay id of an inclusive range; requires --from",
    )

    profile = subparsers.add_parser(
        "advhd-profile",
        help="infer and verify an AdvHD replay profile from engine control flow",
    )
    profile.add_argument("game_dir", type=Path)
    profile.add_argument(
        "--output", type=Path, help="write UTF-8 profile JSON instead of stdout only"
    )

    run_in_place = subparsers.add_parser(
        "advhd-run-in-place",
        help="temporarily install a generated Rio.arc, run AdvHD, then restore it",
    )
    run_in_place.add_argument("manifest", type=Path)

    recover_in_place = subparsers.add_parser(
        "advhd-recover-in-place",
        help="restore the verified original Rio.arc from an in-place replay package",
    )
    recover_in_place.add_argument("manifest", type=Path)

    validate_in_place = subparsers.add_parser(
        "advhd-validate-in-place",
        help="verify in-place replay archives, hashes, and current restore state",
    )
    validate_in_place.add_argument("manifest", type=Path)

    story_build = subparsers.add_parser(
        "story-build",
        help="build the engine-neutral story flow consumed by any frontend",
    )
    story_build.add_argument("game_dir", type=Path)
    story_build.add_argument("output_dir", type=Path)

    story_ui = subparsers.add_parser(
        "story-ui",
        help="open the engine-neutral route novel and original-Scene handoff reader",
    )
    story_ui.add_argument("game_dir", type=Path)
    story_ui.add_argument("output_dir", type=Path)
    story_ui.add_argument("--host", default="127.0.0.1")
    story_ui.add_argument("--port", type=int, default=0)
    story_ui.add_argument(
        "--no-browser", action="store_true", help="start the server without opening a tab"
    )
    project = subparsers.add_parser('project-add', help='add a game path to the index-only project manager')
    project.add_argument('game_dir', type=Path); project.add_argument('projects_dir', type=Path)
    projects = subparsers.add_parser('project-list', help='list indexed game projects')
    projects.add_argument('projects_dir', type=Path)
    whole = subparsers.add_parser('translate-all', help='translate every story segment with resumable progress')
    whole.add_argument('story_flow', type=Path); whole.add_argument('project_dir', type=Path)
    whole.add_argument('--endpoint', required=True); whole.add_argument('--model', required=True)
    whole.add_argument('--target', default='简体中文')
    whole.add_argument('--api-key-env', default='HGAL_TRANSLATION_API_KEY', help='environment variable containing the API key')
    manager = subparsers.add_parser('project-ui', help='open the graphical index-only game project manager')
    manager.add_argument('projects_dir', type=Path)
    task_export = subparsers.add_parser('tasks-export', help='export offline AI translation tasks; never calls an API')
    task_export.add_argument('story_flow', type=Path); task_export.add_argument('project_dir', type=Path)
    task_export.add_argument('--target', default='简体中文'); task_export.add_argument('--output', type=Path, required=True)
    task_import = subparsers.add_parser('tasks-import', help='import validated offline AI translation results')
    task_import.add_argument('story_flow', type=Path); task_import.add_argument('project_dir', type=Path); task_import.add_argument('input', type=Path)
    task_import.add_argument('--target', default='简体中文')
    story_ui.add_argument(
        "--overlay",
        action="store_true",
        help="open a movable no-focus native reader beside one persistent game window (Windows)",
    )
    return parser


def _story_browser() -> StoryBrowser:
    return StoryBrowser(EngineAdapterRegistry(default_engine_adapters()))


def _story_build(args: argparse.Namespace) -> int:
    result = _story_browser().build(args.game_dir, args.output_dir)
    print(json.dumps({
        "output": str(args.output_dir.resolve() / "story_flow.json"),
        "game": result["game"],
        "summary": result["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


def _story_ui(args: argparse.Namespace) -> int:
    if not (0 <= args.port <= 65535):
        raise ValueError("--port must be between 0 and 65535")
    if args.overlay:
        _story_browser().run_native_overlay(args.game_dir, args.output_dir)
        return 0
    _story_browser().serve(
        args.game_dir,
        args.output_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        overlay=False,
    )
    return 0

def _project_add(args):
    item=GameProjectManager(args.projects_dir).add(args.game_dir)
    print(json.dumps(item,ensure_ascii=False,indent=2)); return 0

def _project_list(args):
    print(json.dumps(GameProjectManager(args.projects_dir).list(),ensure_ascii=False,indent=2)); return 0

def _translate_all(args):
    document=json.loads(args.story_flow.read_text(encoding='utf-8'))
    library=TranslationLibrary(args.project_dir/'translation_library.sqlite3')
    job=WholeGameTranslator(document,library,ParagraphTranslator(args.project_dir/'translation_cache.sqlite3'),args.project_dir/'translation_progress.json')
    result=job.run(TranslationConfig(args.endpoint,args.model,args.target),os.environ.get(args.api_key_env,''))
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0 if result['status']=='complete' else 3

def _project_ui(args):
    from hgalgame.project_manager_ui import run
    run(args.projects_dir); return 0

def _tasks_export(args):
    document=json.loads(args.story_flow.read_text(encoding='utf-8'))
    result=export_task_package(document, TranslationLibrary(args.project_dir/'translation_library.sqlite3'), args.target, args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0

def _tasks_import(args):
    document=json.loads(args.story_flow.read_text(encoding='utf-8'))
    result=import_result_package(document, TranslationLibrary(args.project_dir/'translation_library.sqlite3'), args.target, args.input)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


def _detect(args: argparse.Namespace) -> int:
    result = EngineDetector().detect(args.game_dir)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"engine: {result.engine}")
        print(f"confidence: {result.confidence:.2f}")
        for item in result.evidence:
            print(f"- {item.kind}: {item.detail}")
    return 0 if result.engine != "unknown" else 2


def _arc_list(args: argparse.Namespace) -> int:
    manifest = AdvHdArcReader().read_manifest(args.archive)
    entries = manifest.entries
    if args.suffix:
        suffix = args.suffix.casefold()
        entries = [entry for entry in entries if entry.name.casefold().endswith(suffix)]

    if args.json:
        payload = manifest.to_dict()
        payload["entries"] = [entry.to_dict() for entry in entries]
        payload["matched_entry_count"] = len(entries)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"archive: {manifest.path}")
        print(f"entries: {manifest.entry_count} (showing {len(entries)})")
        print(f"data_offset: {manifest.data_offset}")
        for entry in entries:
            print(
                f"{entry.name}\tsize={entry.size}\toffset={entry.absolute_offset}"
            )
    return 0


def _ws2_probe(args: argparse.Namespace) -> int:
    payload = AdvHdArcReader().read_entry(args.archive, args.entry_name)
    result = Ws2Reader().probe(args.entry_name, payload)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"entry: {result.entry_name}")
        print(f"size: {result.byte_length}")
        print(f"status: {result.status}")
        print(f"transform: {result.transform}")
        print(f"encrypted: {str(result.encrypted).lower()}")
        print(f"encoding: {result.encoding}")
        print(f"markers: {result.marker_counts}")
        print(f"sha256: {result.sha256}")
    return 0 if result.status == "recognized" else 2


def _advhd_report(args: argparse.Namespace) -> int:
    result = AdvHdReportBuilder().build(args.game_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result["ws2_summary"]
        print(f"game_dir: {result['game_dir']}")
        print(f"read_only: {str(result['read_only']).lower()}")
        print(f"engine: {result['detection']['engine']}")
        print(f"ws2_total: {summary['total']}")
        print(f"statuses: {summary['status_counts']}")
        print(f"categories: {summary['category_counts']}")
        print(f"transforms: {summary['transform_counts']}")
        for entry in summary["undetermined_entries"]:
            print(f"- undetermined: {entry}")
    return 0


def _ws2_events(args: argparse.Namespace) -> int:
    payload = AdvHdArcReader().read_entry(args.archive, args.entry_name)
    result = Ws2Parser().parse(args.entry_name, payload)
    output = result.to_dict(include_events=not args.summary)
    if args.event_types:
        selected = set(args.event_types)
        filtered = [event for event in result.events if event.type in selected]
        output["event_counts"] = {
            event_type: count
            for event_type, count in result.event_counts.items()
            if event_type in selected
        }
        output["event_count"] = len(filtered)
        if not args.summary:
            output["events"] = [event.to_dict() for event in filtered]
    if args.output:
        path = write_json(args.output, output)
        print(f"wrote: {path}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _advhd_index(args: argparse.Namespace) -> int:
    result = AdvHdSceneIndexer().build(args.game_dir, args.include_events)
    if args.summary:
        result = {
            key: result[key]
            for key in (
                "game_dir",
                "read_only",
                "engine",
                "format_version",
                "story_archive",
                "archives",
                "summary",
            )
        }
    if args.output:
        path = write_json(args.output, result)
        print(f"wrote: {path}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _advhd_scenes(args: argparse.Namespace) -> int:
    result = AdvHdReplaySceneIndexer().build(
        args.game_dir, include_events=not args.no_events
    )
    if args.summary:
        result = {
            key: result[key]
            for key in (
                "game_dir",
                "read_only",
                "engine",
                "format_version",
                "system_archive",
                "story_archive",
                "replay_profile",
                "evidence",
                "summary",
            )
        }
    if args.output:
        path = write_json(args.output, result)
        print(f"wrote: {path}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _advhd_build_replay(args: argparse.Namespace) -> int:
    playlist = _replay_playlist_from_args(args)
    result = AdvHdContinuousReplayBuilder().build(
        args.game_dir, args.output_dir, playlist=playlist
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _advhd_profile(args: argparse.Namespace) -> int:
    result = AdvHdReplayProfiler().build(args.game_dir).to_dict()
    if args.output:
        path = write_json(args.output, result)
        print(f"wrote: {path}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _replay_playlist_from_args(args: argparse.Namespace) -> list[int] | None:
    if args.to_scene is not None and args.from_scene is None:
        raise ValueError("--to requires --from")
    if args.from_scene is not None:
        if args.to_scene is None:
            raise ValueError("--from requires --to")
        if args.from_scene > args.to_scene:
            raise ValueError("--from must not be greater than --to")
        return list(range(args.from_scene, args.to_scene + 1))
    if args.scene is not None:
        return [args.scene]
    if args.playlist is not None:
        values = [part.strip() for part in args.playlist.split(",")]
        if not values or any(not value for value in values):
            raise ValueError("--playlist must be a comma-separated list of replay ids")
        try:
            return [int(value) for value in values]
        except ValueError as exc:
            raise ValueError("--playlist contains a non-integer replay id") from exc
    return None


def _advhd_run_in_place(args: argparse.Namespace) -> int:
    result = AdvHdInPlaceReplayRunner().run(args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["game_exit_code"] or 0)


def _advhd_recover_in_place(args: argparse.Namespace) -> int:
    result = AdvHdInPlaceReplayRunner().recover(args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _advhd_validate_in_place(args: argparse.Namespace) -> int:
    result = AdvHdContinuousReplayBuilder().validate_in_place(args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            return _detect(args)
        if args.command == "arc-list":
            return _arc_list(args)
        if args.command == "ws2-probe":
            return _ws2_probe(args)
        if args.command == "advhd-report":
            return _advhd_report(args)
        if args.command == "ws2-events":
            return _ws2_events(args)
        if args.command == "advhd-index":
            return _advhd_index(args)
        if args.command == "advhd-scenes":
            return _advhd_scenes(args)
        if args.command == "advhd-build-replay":
            return _advhd_build_replay(args)
        if args.command == "advhd-profile":
            return _advhd_profile(args)
        if args.command == "advhd-run-in-place":
            return _advhd_run_in_place(args)
        if args.command == "advhd-recover-in-place":
            return _advhd_recover_in_place(args)
        if args.command == "advhd-validate-in-place":
            return _advhd_validate_in_place(args)
        if args.command == "story-build":
            return _story_build(args)
        if args.command == "story-ui":
            return _story_ui(args)
        if args.command == 'project-add': return _project_add(args)
        if args.command == 'project-list': return _project_list(args)
        if args.command == 'translate-all': return _translate_all(args)
        if args.command == 'project-ui': return _project_ui(args)
        if args.command == 'tasks-export': return _tasks_export(args)
        if args.command == 'tasks-import': return _tasks_import(args)
    except (
        FileNotFoundError,
        NotADirectoryError,
        KeyError,
        AdvHdFormatError,
        Ws2ParseError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
