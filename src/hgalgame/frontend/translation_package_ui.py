"""Local package and paragraph correction dialogs; no network operations."""
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox


class TranslationPackageControls:
    def _open_translation_package(self):
        self._cancel_auto_scene()
        self._awaiting_scene_continue = True
        window = tk.Toplevel(self.root)
        window.title('译文包 · 本地管理')
        window.configure(bg=self.BG)
        window.attributes('-topmost', True)
        target = tk.StringVar(value=self._translation_config['target'])
        tk.Label(window, text='目标语言（需与译文包一致）', bg=self.BG).pack(anchor='w', padx=16, pady=(12, 0))
        tk.Entry(window, textvariable=target, width=38).pack(fill='x', padx=16, pady=6)
        tk.Label(window, text='导入不覆盖已有译文。阅读时按原文指纹匹配，不匹配不应用。\n'
                 '导出只包含译文和指纹，不包含原文正文、密钥或游戏资源。\n'
                 '只分享你有权分享的译文；本地修改记录不随包导出。',
                 bg=self.BG, justify='left', fg=self.MUTED).pack(padx=16, pady=8)

        def language():
            value = target.get().strip()
            if not value:
                raise ValueError('请填写目标语言。')
            if self._translation_busy:
                raise ValueError('请等待当前翻译任务结束后再管理译文包。')
            return value

        def run(action):
            try:
                selected = language()
                if action == 'import':
                    path = filedialog.askopenfilename(parent=window, title='导入译文包', filetypes=[('译文包', '*.json')])
                    if not path:
                        return
                    result = self._translation_library.import_pack(Path(path), selected)
                    self._translation_config['target'] = selected
                    from hgalgame.output import write_json
                    write_json(self.output_dir / 'translation_settings.json', self._translation_config)
                    self._translation_result = None
                    self._render_trace()
                    messagebox.showinfo('导入完成', f"已导入 {result['imported']} 个区间，保留已有 {result['skipped']} 个。\n"
                                        '尚未遇到的区间在阅读时校验；不匹配的条目不会应用。', parent=window)
                elif action == 'export':
                    path = filedialog.asksaveasfilename(parent=window, title='导出译文包', defaultextension='.json',
                                                      initialfile='translations.json', filetypes=[('译文包', '*.json')])
                    if path:
                        count = self._translation_library.export_pack(Path(path), selected)
                        messagebox.showinfo('导出完成', f'已导出 {count} 个区间。', parent=window)
                else:
                    self._edit_translation(window, selected)
            except Exception as exc:
                messagebox.showerror('操作未完成', str(exc), parent=window)
        controls = tk.Frame(window, bg=self.BG)
        controls.pack(padx=16, pady=(6, 16))
        for text, action in [('导入译文包', 'import'), ('导出译文包', 'export'), ('修订当前段', 'edit')]:
            tk.Button(controls, text=text, command=lambda a=action: run(a)).pack(side='left', padx=4)

    def _edit_translation(self, parent, target):
        rows = self._translation_rows()
        if not rows:
            raise ValueError('当前区间没有正文。')
        identity = self._translation_id()
        translated = self._translation_library.get(rows, target)
        if not translated:
            raise ValueError('当前区间没有匹配译文，请先翻译或导入。')
        window = tk.Toplevel(parent)
        window.title('修订当前段 · 原文不变')
        window.attributes('-topmost', True)
        window.geometry('720x600')
        index = [0]
        label = tk.Label(window)
        label.pack(pady=4)
        original = tk.Text(window, height=6, wrap='word')
        original.pack(fill='x', padx=12)
        tk.Label(window, text='译文角色名（旁白留空）').pack(anchor='w', padx=12)
        speaker = tk.Entry(window)
        speaker.pack(fill='x', padx=12)
        text = tk.Text(window, wrap='word', height=10)
        text.pack(fill='both', expand=True, padx=12, pady=6)
        controls = tk.Frame(window)
        controls.pack(fill='x', padx=12, pady=10)

        def capture():
            content = text.get('1.0', 'end-1c')
            if not content.strip():
                raise ValueError('译文不能为空。')
            translated[index[0]] = dict(speaker=speaker.get(), text=content)

        def show():
            i = index[0]
            label.configure(text=f'段落 {i + 1} / {len(rows)} · 上方原文，下方译文')
            original.configure(state='normal')
            original.delete('1.0', 'end')
            original.insert('end', rows[i]['speaker'] + '\n' + rows[i]['text'])
            original.configure(state='disabled')
            speaker.delete(0, 'end')
            speaker.insert(0, translated[i]['speaker'])
            text.delete('1.0', 'end')
            text.insert('end', translated[i]['text'])

        def navigate(step):
            try:
                capture()
                index[0] = max(0, min(len(rows)-1, index[0] + step))
                show()
            except ValueError as exc:
                messagebox.showerror('未切换', str(exc), parent=window)

        def save():
            try:
                capture()
                self._translation_library.save(rows, target, translated, reason='manual', overwrite=True)
                if identity == self._translation_id() and target == self._translation_config['target']:
                    self._translation_result = (identity, translated)
                    self._show_translation = True
                    self._render_trace()
                window.destroy()
            except Exception as exc:
                messagebox.showerror('未保存', str(exc), parent=window)

        def history():
            view = tk.Toplevel(window)
            view.title('本区间修改记录（只读）')
            view.attributes('-topmost', True)
            body = tk.Text(view, wrap='word')
            body.pack(fill='both', expand=True)
            body.insert('end', json.dumps(self._translation_library.history(rows, target), ensure_ascii=False, indent=2))
            body.configure(state='disabled')
        for title, callback in [('上一段', lambda: navigate(-1)), ('下一段', lambda: navigate(1)),
                                ('修改记录', history), ('保存修订', save)]:
            tk.Button(controls, text=title, command=callback).pack(side='left', padx=4)
        show()
