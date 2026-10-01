"""Offline translation task packages; no provider credentials or network."""
from __future__ import annotations
import json, zipfile
from contextlib import closing
from pathlib import Path
from hgalgame.batch_translation import WholeGameTranslator
from hgalgame.translation import ParagraphTranslator, TranslationError
from hgalgame.translation_library import TranslationLibrary

FORMAT = 'hgal-translation-tasks-v1'
RESULT_FORMAT = 'hgal-translation-results-v1'
MAX_RESULT_BYTES = 20_000_000

def _unsplit_tasks(document, library, target):
    job = WholeGameTranslator(document, library, None, Path('.hgal-unused-progress'))
    result = []
    for number, (source_hash, rows) in enumerate(job.segments(), 1):
        result.append(dict(task_id=f'task-{number:05d}', source_hash=source_hash,
            rows=[dict(id=f'line-{number:05d}-{i:04d}', **row) for i, row in enumerate(rows, 1)]))
    # Scene dialogue is indexed separately from route prose.  For scenes whose
    # resume cursor is in the same script, the extracted range is exact; scenes
    # crossing scripts remain explicitly absent rather than guessed.
    segments = {str(s.get('id')): s for s in document.get('segments', [])}
    for scene in document.get('scenes', []):
        start = segments.get(str(scene.get('segment_id')))
        resume = scene.get('resume_cursor') or {}
        if not start or str(resume.get('segment_id', start.get('id'))) != str(start.get('id')):
            continue
        begin = int(scene.get('offset', -1)); end = int(resume.get('offset', 10**18))
        rows = [dict(speaker=e.get('speaker') or '', text=e.get('text') or '')
                for e in start.get('events', [])
                if e.get('kind') == 'dialogue' and begin <= int(e.get('offset', -1)) < end and e.get('text')]
        if not rows:
            continue
        digest = library.fingerprint(rows)
        number = len(result) + 1
        result.append(dict(task_id=f'scene-{scene.get("id", number)}', source_hash=digest,
            scope='scene', scene_id=str(scene.get('id')), rows=[dict(id=f'line-scene-{number:05d}-{i:04d}', **row)
            for i, row in enumerate(rows, 1)]))
    # Choice labels are independent display strings and get their own tasks.
    for segment in document.get('segments', []):
        for event in segment.get('events', []):
            if event.get('kind') != 'choice':
                continue
            for option in event.get('options', []):
                text = str(option.get('text') or '')
                if not text:
                    continue
                rows = [dict(speaker='', text=text)]
                number = len(result) + 1
                result.append(dict(task_id=f'choice-{event.get("id", number)}-{option.get("id", number)}',
                    source_hash=library.fingerprint(rows), scope='choice',
                    choice_id=str(event.get('id')), option_id=str(option.get('id')),
                    rows=[dict(id=f'line-choice-{number:05d}-0001', **rows[0])]))
    return result

def _tasks(document, library, target):
    """Split large route intervals so one AI request stays manageable."""
    result = []
    for source in _unsplit_tasks(document, library, target):
        rows, chunk, size = source['rows'], [], 0
        for row in rows:
            length = len(row['speaker']) + len(row['text'])
            if chunk and (len(chunk) >= 80 or size + length > 8000):
                result.append(dict(source, rows=chunk)); chunk=[]; size=0
            chunk.append(row); size += length
        if chunk: result.append(dict(source, rows=chunk))
    # IDs are regenerated after splitting; source hashes are per chunk.
    for number, task in enumerate(result, 1):
        task['task_id'] = f'{task.get("scope", "task")}-{number:05d}'
        task['source_hash'] = library.fingerprint([dict(speaker=r['speaker'], text=r['text']) for r in task['rows']])
        prefix = 'line-scene' if task.get('scope') == 'scene' else 'line'
        task['rows'] = [dict(r, id=f'{prefix}-{number:05d}-{i:04d}') for i, r in enumerate(task['rows'], 1)]
    return result

def export_task_package(document, library, target, path):
    path = Path(path)
    if path.suffix.lower() != '.zip':
        raise TranslationError('翻译任务包必须保存为 .zip 文件。')
    tasks = _tasks(document, library, target)
    title = document.get('game', {}).get('title', 'H-Galgame')
    prompt = ('你是视觉小说本地化译者。只翻译 rows.text，保留 task_id、source_hash、每个 line id 和顺序。\n'
              '不要删句、合并句、添加解释或输出 Markdown。角色名可以翻译到 speaker。\n'
              '严格返回 JSON：{"format":"hgal-translation-results-v1","target":"目标语言",'
              '"tasks":[{"task_id":"...","source_hash":"...","translations":'
              '[{"id":"...","speaker":"...","text":"..."}]}]}')
    manifest = dict(format=FORMAT, target=target, title=title, task_count=len(tasks), result_format=RESULT_FORMAT)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('prompt.txt', prompt)
        archive.writestr('README.txt', '将 tasks/*.json 交给 AI，按 prompt.txt 返回 JSON，再导入返回文件。\n')
        for task in tasks:
            archive.writestr(f'tasks/{task["task_id"]}.json', json.dumps(
                dict(format=FORMAT, target=target, **task), ensure_ascii=False, indent=2))
    return dict(tasks=len(tasks), paragraphs=sum(len(t['rows']) for t in tasks), path=str(path))

def _read_results(path):
    path = Path(path)
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith('.json') and n != 'manifest.json']
            if not names or len(names) > 1000 or len(set(names)) != len(names):
                raise ValueError('empty or duplicate archive entries')
            if sum(archive.getinfo(n).file_size for n in names) > MAX_RESULT_BYTES:
                raise ValueError('results too large')
            return [json.loads(archive.read(n).decode('utf-8-sig')) for n in names]
    with path.open('rb') as source:
        raw = source.read(MAX_RESULT_BYTES + 1)
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError('results too large')
    return [json.loads(raw.decode('utf-8-sig'))]

def import_result_package(document, library, target, path):
    if not isinstance(target, str) or not target.strip():
        raise TranslationError('请填写目标语言。')
    expected = {t['task_id']: t for t in _tasks(document, library, target)}
    legacy = {t['task_id']: t for t in _unsplit_tasks(document, library, target)}
    imported = skipped = 0; seen = set(); pending = []
    try:
        payloads = _read_results(Path(path))
        # Results generated before chunked task export keep the old IDs and
        # hashes. Select that snapshot only when every returned task matches it.
        candidates = [expected, legacy]
        all_items = []
        for payload in payloads:
            if payload.get('format') == RESULT_FORMAT:
                all_items.extend(payload.get('tasks', []))
            elif payload.get('format') == FORMAT:
                all_items.append(payload)
        for candidate in candidates:
            if all(isinstance(item, dict) and item.get('task_id') in candidate and
                   item.get('source_hash') == candidate[item['task_id']]['source_hash'] for item in all_items):
                expected = candidate
                break
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get('format') not in (RESULT_FORMAT, FORMAT) or payload.get('target') != target:
                raise ValueError('format or target')
            items = payload.get('tasks') if payload.get('format') == RESULT_FORMAT else [payload]
            if not isinstance(items, list) or not items:
                raise ValueError('tasks')
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('task_id'), str):
                    raise ValueError('invalid task')
                task = expected.get(item.get('task_id'))
                if not task or item['task_id'] in seen or item.get('source_hash') != task['source_hash']:
                    raise ValueError('task mismatch')
                rows = item.get('translations'); source_ids = [r['id'] for r in task['rows']]
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows) or [r.get('id') for r in rows] != source_ids:
                    raise ValueError('line ids')
                if any(not isinstance(r.get('speaker', ''), str) or not isinstance(r.get('text'), str) or not r['text'].strip() for r in rows):
                    raise ValueError('invalid translated strings')
                value = [dict(speaker=r.get('speaker',''), text=r['text']) for r in rows]
                ParagraphTranslator.validate_rows(value, len(task['rows']))
                original = [dict(speaker=r['speaker'], text=r['text']) for r in task['rows']]
                pending.append((original, value, item['task_id'])); seen.add(item['task_id'])
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, zipfile.BadZipFile):
        raise TranslationError('AI 返回文件格式、任务版本或行 ID 不正确，未导入任何内容。') from None
    # All tasks commit together. Disk/database errors must not leave half a pack.
    with closing(library._open()) as db, db:
        for original, value, _ in pending:
            if library._put(db, library.fingerprint(original), target, value, 'offline-import', False):
                imported += 1
            else:
                skipped += 1
    completed = sum(bool(library.get([dict(speaker=r['speaker'], text=r['text']) for r in task['rows']], target))
                    for task in expected.values())
    return dict(imported=imported, skipped=skipped, completed=completed,
                missing=len(expected)-completed, total=len(expected))
