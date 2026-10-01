"""Shared whole-story task window for the manager and the reader."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from hgalgame.batch_translation import WholeGameTranslator
from hgalgame.output import write_json
from hgalgame.translation import ParagraphTranslator, TranslationConfig, TranslationError
from hgalgame.translation_library import TranslationLibrary
from hgalgame.translation_tasks import export_task_package, import_result_package


class BatchTranslationWindow:
    def __init__(self, parent, document, output_dir, *, config=None, key='', translator=None):
        self.document, self.output_dir = document, Path(output_dir)
        self.library = TranslationLibrary(self.output_dir/'translation_library.sqlite3')
        self.translator = translator or ParagraphTranslator(self.output_dir/'translation_cache.sqlite3')
        self.job = None
        self.worker = None
        self.events = queue.Queue()
        self.closed = False
        self.window = tk.Toplevel(parent)
        self.window.title('整部翻译 · 所有可达阅读线路')
        self.window.geometry('740x560')
        self.window.minsize(620, 480)
        self.window.configure(bg='#f4f7f6')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        settings = dict(endpoint='', model='', target='简体中文')
        try:
            stored=json.loads((self.output_dir/'translation_settings.json').read_text('utf-8'))
            settings.update({k:stored[k] for k in settings if isinstance(stored.get(k),str)})
        except (OSError,ValueError,AttributeError):
            pass
        if config:
            settings.update(config)
        tk.Label(self.window,text=document.get('game',{}).get('title','游戏项目'),
                 font=('Microsoft YaHei UI',16,'bold'),bg='#f4f7f6',fg='#263d35').pack(anchor='w',padx=20,pady=14)
        tk.Label(self.window,text='推荐：导出任务包交给任意 AI，再导入结果；不需要 API Key。演出内字幕仍由原游戏／Luna 显示。',
                 bg='#f4f7f6',fg='#64786e',wraplength=570,justify='left').pack(anchor='w',padx=20)
        form=tk.Frame(self.window,bg='#f4f7f6')
        form.pack(fill='x',padx=20,pady=12)
        form.columnconfigure(1,weight=1)
        tabs=ttk.Notebook(self.window)
        tabs.pack(fill='x',padx=20,pady=8)
        offline=tk.Frame(tabs,bg='#f4f7f6')
        advanced=tk.Frame(tabs,bg='#f4f7f6')
        tabs.add(offline,text='离线翻译（推荐）')
        tabs.add(advanced,text='API 高级模式（可选）')
        advanced.columnconfigure(1,weight=1)
        tk.Label(offline,text='导出任务 → 交给 AI → 导入结果\n可以分批导入；已有译文及人工修订不会覆盖。',
                 bg='#f4f7f6',justify='left').pack(anchor='w',padx=8,pady=12)
        self.entries={}
        for row,(name,label) in enumerate([('endpoint','完整接口地址'),('model','模型'),('target','目标语言'),('key','API Key（仅本次窗口）')]):
            container=form if name=='target' else advanced
            tk.Label(container,text=label,bg='#f4f7f6').grid(row=row,column=0,sticky='w',pady=5)
            entry=tk.Entry(container,show='*' if name=='key' else '')
            entry.insert(0,key if name=='key' else settings[name])
            entry.grid(row=row,column=1,sticky='ew',padx=8,pady=5)
            self.entries[name]=entry
        self.progress=ttk.Progressbar(self.window,mode='determinate')
        self.progress.pack(fill='x',padx=20,pady=12)
        self.status=tk.Label(self.window,text='无需接口配置。已有任务包可以继续翻译并直接导入。',
                             bg='#f4f7f6',fg='#263d35',anchor='w',justify='left',wraplength=680)
        self.status.pack(fill='x',padx=20,pady=6)
        bar=tk.Frame(self.window,bg='#f4f7f6')
        bar.pack(fill='x',padx=20,pady=14)
        self.buttons=[]
        for title,callback in [('API 高级模式（可选）',self.start),('导出译文包',self.export_pack),('导入翻译结果',self.import_any)]:
            container=advanced if callback==self.start else bar
            button=tk.Button(container,text='开始 API 翻译' if callback==self.start else title,command=callback)
            if container==advanced:button.grid(row=4,column=1,sticky='w',pady=8)
            else:button.pack(side='left',padx=4)
            self.buttons.append(button)
        for title,callback in [('导出 AI 任务包',self.export_tasks)]:
            button=tk.Button(offline,text=title,command=callback)
            button.pack(side='left',padx=4)
            self.buttons.append(button)
        self.pause_button=tk.Button(advanced,text='暂停',command=self.pause,state='disabled')
        self.pause_button.grid(row=4,column=0,padx=4)
        self.window.after(80,self.poll)

    def busy(self):
        return bool(self.worker and self.worker.is_alive())

    def set_busy(self, busy):
        for button in self.buttons:
            button.configure(state='disabled' if busy else 'normal')
        for entry in self.entries.values():
            entry.configure(state='disabled' if busy else 'normal')
        self.pause_button.configure(state='normal' if busy else 'disabled')

    def start(self):
        if self.busy(): return
        settings={name:self.entries[name].get().strip() for name in ('endpoint','model','target')}
        config=TranslationConfig(**settings)
        try:
            config.validate()
            write_json(self.output_dir/'translation_settings.json',settings)
        except (ValueError,OSError) as exc:
            messagebox.showerror('配置不正确',str(exc),parent=self.window)
            return
        if not messagebox.askyesno('开始整部翻译',
                f'将把未翻译的小说区间发送至：\n{config.endpoint}\n模型：{config.model}\n'
                '可能产生费用。已完成内容自动复用，失败时停止，不自动重试。',
                parent=self.window,default='no'):
            return
        key=self.entries['key'].get().strip()
        self.job=WholeGameTranslator(self.document,self.library,self.translator,self.output_dir/'translation_progress.json')
        self.status.configure(text='正在分析所有可达阅读区间……')
        self.set_busy(True)
        def worker():
            try:
                result=self.job.run(config,key,lambda state,_segment:self.events.put(('progress',state)))
                self.events.put(('done',result))
            except TranslationError as exc:
                self.events.put(('error',str(exc)))
            except Exception:
                self.events.put(('error','任务未完成，请检查剧情索引及本地目录权限；已保存译文仍可继续使用。'))
        # Let an already-paid request finish and persist even if all windows close.
        self.worker=threading.Thread(target=worker,daemon=False,name='whole-story-task')
        self.worker.start()

    def pause(self):
        if self.job:
            self.job.stop_event.set()
            self.status.configure(text='正在暂停；已发出的请求完成后停止后续请求。')

    def poll(self):
        if self.closed:return
        try:
            while True:
                kind,payload=self.events.get_nowait()
                if kind=='error':
                    self.set_busy(False)
                    self.status.configure(text=payload)
                    continue
                total,done=payload.get('total',0),payload.get('completed',0)
                self.progress.configure(maximum=max(1,total),value=done)
                word={'complete':'已完成','paused':'已暂停','failed':'失败','error':'失败','running':'进行中'}.get(payload.get('status'),'进行中')
                detail=payload.get('error') or ''
                self.status.configure(text=f'{word}：{done} / {total} 个阅读区间。\n{detail}')
                if kind=='done':self.set_busy(False)
        except queue.Empty:
            pass
        self.window.after(80,self.poll)

    def import_pack(self):
        if self.busy():return
        path=filedialog.askopenfilename(parent=self.window,filetypes=[('译文包','*.json')])
        if path:
            try:
                result=self.library.import_pack(Path(path),self.entries['target'].get().strip())
                self.status.configure(text=f"导入 {result['imported']} 个区间，保留已有 {result['skipped']} 个；继续任务时自动匹配。")
            except Exception as exc:messagebox.showerror('导入失败',str(exc),parent=self.window)

    def import_any(self):
        """One user-facing import entry; dispatch by the declared format."""
        if self.busy(): return
        path=filedialog.askopenfilename(parent=self.window,title='导入翻译结果（自动识别格式）',
            filetypes=[('翻译结果','*.json *.zip'),('JSON','*.json'),('ZIP','*.zip')])
        if not path:return
        try:
            raw=Path(path).read_bytes() if Path(path).suffix.lower() == '.json' else b''
            if raw:
                import json as _json
                fmt=_json.loads(raw.decode('utf-8-sig')).get('format')
            else:
                fmt='hgal-translation-results-v1'
            if fmt == TranslationLibrary.FORMAT:
                result=self.library.import_pack(Path(path),self.entries['target'].get().strip())
                self.status.configure(text=f'已导入旧版译文包 {result["imported"]} 个区间，保留 {result["skipped"]} 个。')
            elif fmt == 'hgal-translation-tasks-v1':
                raise ValueError('这是待翻译任务包，不是 AI 返回结果；请等待 AI 返回 translations JSON。')
            else:
                result=import_result_package(self.document,self.library,self.entries['target'].get().strip(),Path(path))
                self.progress.configure(maximum=max(1,result['total']),value=result['completed'])
                self.status.configure(text=f'已导入 {result["imported"]} 个翻译任务；累计完成 {result["completed"]}/{result["total"]}。')
        except Exception as exc:messagebox.showerror('导入翻译结果失败',str(exc),parent=self.window)

    def export_tasks(self):
        if self.busy(): return
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.zip',
            initialfile='translation_tasks.zip',filetypes=[('AI 翻译任务包','*.zip')])
        if not path:return
        try:
            result=export_task_package(self.document,self.library,self.entries['target'].get().strip(),Path(path))
            self.status.configure(text=f'已导出 {result["tasks"]} 个任务、{result["paragraphs"]} 段原文。请把 ZIP 和 prompt.txt 交给 AI。')
        except Exception as exc:messagebox.showerror('导出任务失败',str(exc),parent=self.window)

    def import_results(self):
        if self.busy(): return
        path=filedialog.askopenfilename(parent=self.window,title='导入 AI 返回结果',
            filetypes=[('AI 结果','*.json *.zip'),('JSON','*.json'),('ZIP','*.zip')])
        if not path:return
        try:
            result=import_result_package(self.document,self.library,self.entries['target'].get().strip(),Path(path))
            self.progress.configure(maximum=max(1,result['total']),value=result['completed'])
            self.status.configure(text=f'本次导入 {result["imported"]} 个，保留已有 {result["skipped"]} 个；累计完成 {result["completed"]}/{result["total"]}，尚缺 {result["missing"]} 个。')
        except Exception as exc:messagebox.showerror('导入结果失败',str(exc),parent=self.window)

    def export_pack(self):
        if self.busy():return
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.json',initialfile='translations.json',filetypes=[('译文包','*.json')])
        if path:
            try:
                count=self.library.export_pack(Path(path),self.entries['target'].get().strip())
                self.status.configure(text=f'已导出 {count} 个区间；可随时导出部分成果。')
            except Exception as exc:messagebox.showerror('导出失败',str(exc),parent=self.window)

    def close(self):
        self.pause()
        self.closed=True
        self.window.destroy()
