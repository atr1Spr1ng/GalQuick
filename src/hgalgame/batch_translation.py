"""Resumable translation of complete, reachable reader intervals."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from hgalgame.frontend.route_reader import RouteNovelReader
from hgalgame.translation import ParagraphTranslator, TranslationConfig, TranslationError
from hgalgame.translation_library import TranslationLibrary


class WholeGameTranslator:
    MAX_STATES = 10000
    MAX_ACTIONS = 500

    def __init__(self, document: dict, library: TranslationLibrary, translator, progress: Path):
        self.document, self.library, self.translator = document, library, translator
        self.progress = Path(progress)
        self.stop_event = threading.Event()

    def segments(self):
        """Enumerate reader intervals, expanding every choice and skipping scenes.

        Reader gates have no hidden variable state: expand each gate once but
        collect each incoming interval. Ancestors distinguish cycles from merges.
        """
        pending = deque([([], frozenset(), None)])
        expanded, intervals = set(), set()
        links = {}
        count = 0
        while pending:
            actions, ancestors, parent = pending.popleft()
            count += 1
            if count > self.MAX_STATES or len(actions) > self.MAX_ACTIONS:
                raise TranslationError('剧情路线超过批量解析上限，未开始翻译。')
            reader = RouteNovelReader(self.document, actions)
            trace = reader.render()
            if trace.error:
                raise TranslationError('剧情图存在循环或无法解析的位置，未开始翻译。')
            rows = [dict(speaker=p.speaker or '', text=p.text)
                    for p in trace.current_paragraphs if p.kind == 'dialogue']
            if rows:
                digest = self.library.fingerprint(rows)
                if digest not in intervals:
                    intervals.add(digest)
                    yield digest, rows
            gate = trace.gate
            if gate is None:
                continue
            identity = (gate.kind, gate.id, json.dumps(gate.event, sort_keys=True, ensure_ascii=False))
            links.setdefault(identity, set())
            if parent is not None:
                links[parent].add(identity)
            if identity in ancestors:
                raise TranslationError('剧情选项或场景形成循环，未开始翻译。')
            if identity in expanded:
                continue
            expanded.add(identity)
            ancestry = ancestors | {identity}
            if gate.kind == 'choice':
                options = gate.event.get('options', [])
                if not options:
                    raise TranslationError('剧情选项没有可达分支，未开始翻译。')
                for option in options:
                    pending.append((actions + [dict(type='choice', id=gate.id,
                                                   option_id=option['id'])], ancestry, identity))
            elif gate.kind == 'scene':
                pending.append((actions + [dict(type='scene', id=gate.id, status='skipped')], ancestry, identity))
        # A merge can hide a back edge from a single traversal's ancestors.
        # Topological validation detects these cycles without exploring all paths.
        indegree = dict.fromkeys(links, 0)
        for targets in links.values():
            for target in targets:
                indegree[target] += 1
        roots = deque(node for node, degree in indegree.items() if degree == 0)
        visited = 0
        while roots:
            node = roots.popleft()
            visited += 1
            for target in links[node]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    roots.append(target)
        if visited != len(links):
            raise TranslationError('剧情分支合流后形成循环，未开始翻译。')

    def plan(self, target):
        intervals = list(self.segments())
        missing = [(identity, rows) for identity, rows in intervals if not self.library.get(rows, target)]
        return dict(total=len(intervals), completed=len(intervals) - len(missing),
                    missing=len(missing), paragraphs=sum(len(rows) for _, rows in missing),
                    characters=sum(len(row['speaker']) + len(row['text']) for _, rows in missing for row in rows))

    @contextmanager
    def _job_lock(self):
        path = self.library.path.with_suffix(self.library.path.suffix + '.batch.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TranslationError('这个项目已有整部翻译任务正在运行。') from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _persist(self, state):
        self.progress.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.hgal-progress-', dir=self.progress.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.progress)
        finally:
            Path(name).unlink(missing_ok=True)

    def _translate_interval(self, rows, config, key):
        # Bounded calls retain batch caching, while library entries match the
        # entire reader interval, even when it crosses script files.
        result, chunk, size = [], [], 0
        for row in rows:
            length = len(row['speaker']) + len(row['text'])
            if chunk and (len(chunk) >= 48 or size + length > 2400):
                result.extend(self.translator.translate(chunk, config, key, cancelled=self.stop_event.is_set))
                chunk, size = [], 0
            if self.stop_event.is_set():
                raise TranslationError('翻译已暂停。')
            chunk.append(row)
            size += length
        if chunk:
            result.extend(self.translator.translate(chunk, config, key, cancelled=self.stop_event.is_set))
        return ParagraphTranslator.validate_rows(result, len(rows))

    def run(self, config: TranslationConfig, key='', callback=None):
        config.validate()
        with self._job_lock():
            return self._run(config, key, callback)

    def _run(self, config, key, callback):
        # Validate the entire graph before any potentially billed requests.
        all_segments = list(self.segments())
        cached = {identity for identity, rows in all_segments if self.library.get(rows, config.target)}
        state = dict(format='hgal-whole-translation-v1', target=config.target,
                     total=len(all_segments), completed=len(cached), status='running')

        def publish(identity=''):
            self._persist(state)
            if callback:
                callback(state.copy(), identity)

        publish()
        for identity, rows in all_segments:
            if self.stop_event.is_set():
                state['status'] = 'paused'
                break
            if identity in cached:
                continue
            try:
                result = self._translate_interval(rows, config, key)
                self.library.save(rows, config.target, result, reason='whole-game')
            except Exception:
                # Provider exceptions can contain credentials or original text.
                state['status'] = 'paused' if self.stop_event.is_set() else 'error'
                if state['status'] == 'error':
                    state['error'] = '翻译失败；已完成内容已保存，请检查接口配置后继续。'
                break
            cached.add(identity)
            state['completed'] = len(cached)
            publish(identity)
        else:
            state['status'] = 'complete'
        publish()
        return state
