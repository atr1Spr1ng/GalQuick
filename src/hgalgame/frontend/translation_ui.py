"""Optional translation controls; never mutate route state or game scripts."""
import hashlib
import json
import threading
import tkinter as tk
from tkinter import messagebox

from hgalgame.output import write_json
from hgalgame.translation import ParagraphTranslator, TranslationConfig, TranslationError
from hgalgame.translation_library import TranslationLibrary
from hgalgame.frontend.translation_package_ui import TranslationPackageControls


class TranslationControls(TranslationPackageControls):
    def _init_translation(self):
        self._translation_config = dict(endpoint='', model='', target='简体中文')
        try:
            stored = json.loads((self.output_dir / 'translation_settings.json').read_text('utf-8'))
            for name in self._translation_config:
                if isinstance(stored.get(name), str):
                    self._translation_config[name] = stored[name]
        except (OSError, ValueError, AttributeError):
            pass
        self._translation_key = ''  # Memory only; never serialize or log credentials.
        self._translation_busy = False
        self._translation_stop = threading.Event()
        self._translation_result = None
        self._show_translation = False
        self._translation_button = None
        self._translator = ParagraphTranslator(self.output_dir / 'translation_cache.sqlite3')
        self._translation_library = TranslationLibrary(self.output_dir / 'translation_library.sqlite3')
        self._batch_translation_window = None

    def _translation_rows(self):
        return [dict(speaker=p.speaker or '', text=p.text) for p in self.trace.current_paragraphs
                if p.kind != 'chapter']

    def _translation_id(self):
        value = [self._translation_rows(), self.reader.actions,
                 self.trace.gate.id if self.trace.gate else None]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    def _translation_toolbar(self, parent):
        row = tk.Frame(parent, bg=self.SURFACE)
        row.pack(fill='x', padx=8, pady=4)
        self._action_button(row, '翻译设置', self._open_translation_settings).pack(side='left')
        self._action_button(row, '翻译本段', self._request_translation).pack(side='left', padx=6)
        self._action_button(row, '整部翻译', self._request_whole_translation).pack(side='left')
        self._translation_button = self._action_button(row, '显示译文', self._toggle_translation)
        self._translation_button.pack(side='left')
        self._action_button(row, '译文包', self._open_translation_package).pack(side='left', padx=6)

    def _open_translation_settings(self):
        self._cancel_auto_scene()
        self._awaiting_scene_continue = True
        window = tk.Toplevel(self.root)
        window.title('翻译设置 · 仅本窗口文字')
        window.attributes('-topmost', True)
        window.configure(bg=self.BG)
        window.resizable(False, False)
        entries = {}
        for index, (name, label) in enumerate([
                ('endpoint', '完整接口地址（以 /chat/completions 结尾）'),
                ('model', '模型名称'), ('target', '目标语言'), ('key', 'API Key（仅本次运行有效）')]):
            tk.Label(window, text=label, bg=self.BG, fg=self.TEXT).grid(row=index*2, column=0, sticky='w', padx=16, pady=(8, 0))
            entry = tk.Entry(window, width=64, show='*' if name == 'key' else '')
            entry.insert(0, self._translation_key if name == 'key' else self._translation_config[name])
            entry.grid(row=index*2+1, column=0, padx=16, pady=3)
            entries[name] = entry
        tk.Label(window, text='仅点击「翻译本段」才发送当前正文和角色名，可能产生 API 费用。\n'
                 '密钥不落盘；配置和译文缓存保存在本地。服务商可能拒绝部分内容。',
                 bg=self.BG, fg=self.MUTED, justify='left').grid(row=8, column=0, padx=16, pady=10)

        def save():
            config = {name: entries[name].get().strip() for name in ('endpoint', 'model', 'target')}
            try:
                TranslationConfig(**config).validate()
                write_json(self.output_dir / 'translation_settings.json', config)
            except (ValueError, OSError) as exc:
                messagebox.showerror('设置未保存', str(exc), parent=window)
                return
            self._translation_config = config
            self._translation_stop.set()  # Discard in-flight results from the old configuration.
            self._translation_key = entries['key'].get().strip()
            self._show_translation = False
            self._translation_result = None
            window.destroy()
            self._render_trace()
            self._set_status('翻译设置已保存；点击「翻译本段」才会请求接口。', self.MUTED)
        tk.Button(window, text='保存设置（不发请求）', command=save).grid(row=9, column=0, padx=16, pady=(0, 14), sticky='e')

    def _request_translation(self):
        if self._translation_busy:
            self._set_status('本段正在翻译，请勿重复提交；切换原文可停止后续批次。', self.MUTED)
            return
        rows = self._translation_rows()
        if not rows:
            self._set_status('当前区间没有可翻译的正文。', self.MUTED)
            return
        cached = self._translation_library.get(rows, self._translation_config['target'])
        if cached:
            self._translation_result = (self._translation_id(), cached)
            self._show_translation = True
            self._render_trace()
            self._set_status('已使用本地匹配译文，没有发送 API 请求。', self.ACCENT)
            return
        if self._batch_translation_window and self._batch_translation_window.busy():
            self._set_status('整部翻译正在进行，请等待或暂停后再翻译本段。', self.MUTED)
            return
        try:
            config = TranslationConfig(**self._translation_config)
            config.validate()
        except TranslationError:
            self._open_translation_settings()
            return
        self._cancel_auto_scene()
        self._awaiting_scene_continue = True
        if not messagebox.askyesno('确认发送当前正文',
                f'将把本段 {len(rows)} 段文字及角色名发送到：\n{config.endpoint}\n模型：{config.model}\n'
                '命中缓存的批次不会重复请求；其余可能产生费用。继续吗？',
                parent=self.root, default='no'):
            return
        identity = self._translation_id()
        key = self._translation_key
        self._translation_busy = True
        self._translation_stop.clear()
        if self._translation_button:
            self._translation_button.configure(text='原文 / 停止翻译')
        self._set_status('正在翻译当前区间；不会自动重试，原文仍可阅读。', self.ACCENT)
        def worker():
            try:
                result = self._translator.translate(rows, config, key, cancelled=self._translation_stop.is_set)
                self._translation_library.save(rows, config.target, result)
                self._events.put(('translation_done', (identity, result, None)))
            except TranslationError as exc:
                self._events.put(('translation_done', (identity, None, str(exc))))
            except Exception:
                self._events.put(('translation_done', (identity, None, '翻译失败，原文保留；请检查接口或本地缓存权限。')))
        threading.Thread(target=worker, daemon=True, name='paragraph-translation').start()

    def _request_whole_translation(self):
        if self._translation_busy:
            self._set_status('请先等待当前段落翻译结束。', self.MUTED)
            return
        if self._batch_translation_window and not self._batch_translation_window.closed:
            self._batch_translation_window.window.lift()
            return
        if self._batch_translation_window and self._batch_translation_window.busy():
            self._set_status('上一项整部翻译正在暂停，请稍后重试。', self.MUTED)
            return
        self._cancel_auto_scene()
        self._awaiting_scene_continue=True
        from hgalgame.frontend.batch_translation_window import BatchTranslationWindow
        self._batch_translation_window=BatchTranslationWindow(
            self.root,self.document,self.output_dir,config=self._translation_config,key=self._translation_key)

    def _translation_finished(self, identity, result, error):
        self._translation_busy = False
        if self._translation_button:
            self._translation_button.configure(text='显示译文')
        if identity != self._translation_id() or self._translation_stop.is_set():
            self._set_status('原区间的翻译任务已结束；已完成批次保留在缓存中。', self.MUTED)
            return
        if error:
            self._set_status(error, self.DANGER)
            return
        self._translation_library.save(self._translation_rows(), self._translation_config['target'], result)
        result = self._translation_library.get(self._translation_rows(), self._translation_config['target'])
        self._translation_result = (identity, result)
        self._show_translation = True
        self._render_trace()
        self._set_status('译文已显示并缓存；点击「显示原文」可对照。', self.ACCENT)

    def _toggle_translation(self):
        if not self._translation_busy and (not self._translation_result or self._translation_result[0] != self._translation_id()):
            cached = self._translation_library.get(self._translation_rows(), self._translation_config['target'])
            if cached:
                self._translation_result = (self._translation_id(), cached)
                self._show_translation = False
        if self._translation_busy:
            self._translation_stop.set()
            self._show_translation = False
        elif not self._translation_result or self._translation_result[0] != self._translation_id():
            self._set_status('当前区间尚无译文，请先点击「翻译本段」。', self.MUTED)
            return
        else:
            self._show_translation = not self._show_translation
        self._render_trace()

    def _visible_translation(self):
        result = self._translation_result
        if not result or result[0] != self._translation_id():
            cached = self._translation_library.get(self._translation_rows(), self._translation_config['target'])
            if cached:
                result = self._translation_result = (self._translation_id(), cached)
                self._show_translation = True
        valid = bool(self._show_translation and result and result[0] == self._translation_id())
        if self._translation_button:
            self._translation_button.configure(text='显示原文' if valid else '显示译文')
        return result[1] if valid else None
