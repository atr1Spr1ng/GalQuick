"""Optional read-only bridge to an already running LunaTranslator.

This does not bundle LunaHook or inject a process. LunaTranslator remains the
owner of Hook lifecycle; we only subscribe to its documented local websocket
text output and match exact imported translations.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass
from typing import Callable
from hgalgame.translation_library import TranslationLibrary
from hgalgame.subtitle_matcher import SubtitleMatcher, MatchResult

@dataclass(frozen=True)
class LunaBridgeConfig:
    host: str = '127.0.0.1'
    port: int = 2333
    path: str = '/api/ws/text/origin'

class LocalTranslationMatcher:
    @staticmethod
    def _canonical(text):
        return text.replace('\r\n','\n').replace('\r','\n').strip()
    def __init__(self, library: TranslationLibrary, target: str, document=None):
        self.library, self.target = library, target
        self.document = document
        self._scene_indexes = {}
        self.last_result = MatchResult()
    def match(self, text: str, speaker: str = '', scene_id=None) -> str | None:
        if not isinstance(text, str) or not text.strip(): return None
        if self.document is not None:
            # A Hook message carries no script cursor. Never search other scenes.
            if not scene_id: return None
            signature = (scene_id, self.target, self.library.path.stat().st_mtime_ns if self.library.path.exists() else 0)
            if signature not in self._scene_indexes:
                from hgalgame.translation_tasks import _tasks, _unsplit_tasks
                rows = []
                full = [t for t in _unsplit_tasks(self.document, self.library, self.target) if t.get('scene_id') == scene_id]
                complete = False
                for task in full:
                    source = [dict(speaker=r['speaker'], text=r['text']) for r in task['rows']]
                    value = self.library.get(source, self.target)
                    if value:
                        rows.extend((r['speaker'], r['text'], t['text'], t.get('speaker','')) for r,t in zip(source,value))
                        complete = True
                for task in ([] if complete else _tasks(self.document, self.library, self.target)):
                    if task.get('scene_id') != scene_id: continue
                    source = [dict(speaker=r['speaker'], text=r['text']) for r in task['rows']]
                    value = self.library.get(source, self.target)
                    rows.extend((r['speaker'],r['text'],value[i]['text'] if value else None,
                                 value[i].get('speaker','') if value else '') for i,r in enumerate(source))
                self._scene_indexes = {signature: SubtitleMatcher(rows)}
            self.last_result = self._scene_indexes[signature].match(text, speaker)
            return self.last_result.translation
        rows = [dict(speaker=speaker or '', text=text)]
        value = self.library.get(rows, self.target)
        return value[0]['text'] if value and value[0].get('text') else None

class LunaOriginBridge:
    def __init__(self, config=LunaBridgeConfig(), *, on_text: Callable[[str], None], connector=None, on_status=None):
        self.config, self.on_text = config, on_text
        self.connector = connector
        self.on_status = on_status or (lambda status: None)
        self.socket = None
        self.thread = None
        self.stop_event = threading.Event()

    @property
    def url(self):
        return f'ws://{self.config.host}:{self.config.port}{self.config.path}'

    def start(self):
        if self.thread and self.thread.is_alive(): return
        if self.config.host not in ('127.0.0.1', 'localhost', '::1') or not 1 <= self.config.port <= 65535:
            raise ValueError('Luna 文本桥只允许本机地址和有效端口。')
        if self.connector is None:
            try:
                import websocket
                self.connector = websocket.create_connection
            except ImportError as exc:
                raise RuntimeError('需要安装 websocket-client，或继续使用无 Luna 模式。') from exc
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name='luna-origin-bridge')
        self.thread.start()

    def _run(self):
        try:
            self.socket = self.connector(self.url, timeout=1.0)
            self.on_status('connected')
            while not self.stop_event.is_set():
                try: text = self.socket.recv()
                except Exception as exc:
                    if isinstance(exc, TimeoutError) or type(exc).__name__ == 'WebSocketTimeoutException': continue
                    break
                if text is None or text == '': break
                if isinstance(text, bytes): text = text.decode('utf-8', 'replace')
                if text: self.on_text(text)
        except Exception:
            self.on_status('error')
        finally:
            sock, self.socket = self.socket, None
            try:
                if sock: sock.close()
            except Exception: pass
            self.on_status('disconnected')

    def stop(self):
        self.stop_event.set()
        sock = self.socket
        try:
            if sock: sock.close()
        except Exception: pass
