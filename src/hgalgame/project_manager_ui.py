"""Unified launcher; source game resources stay in their original location."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from hgalgame.project_manager import GameProjectManager


class ProjectManagerWindow:
    def __init__(self, root: tk.Tk, directory: Path):
        self.root, self.manager = root, GameProjectManager(directory)
        self.events=queue.Queue()
        self.busy=False
        self.closed=False
        self.batch_windows={}
        self.readers={}
        root.title('H-Galgame 游戏项目管理器')
        root.geometry('900x560')
        root.minsize(720,480)
        root.configure(bg='#f4f7f6')
        root.protocol('WM_DELETE_WINDOW',self.close)
        tk.Label(root,text='游戏项目',font=('Microsoft YaHei UI',20,'bold'),
                 bg='#f4f7f6',fg='#263d35').pack(anchor='w',padx=20,pady=(18,4))
        tk.Label(root,text='添加游戏目录 → 解析剧情 → 整部翻译／打开阅读器',
                 bg='#f4f7f6',fg='#64786e').pack(anchor='w',padx=20,pady=(0,12))
        self.tree=ttk.Treeview(root,columns=('engine','state'),show='tree headings',selectmode='browse')
        self.tree.heading('#0',text='游戏')
        self.tree.heading('engine',text='引擎')
        self.tree.heading('state',text='解析状态')
        self.tree.column('#0',width=380)
        self.tree.column('engine',width=120)
        self.tree.column('state',width=150)
        self.tree.pack(fill='both',expand=True,padx=20)
        self.tree.bind('<<TreeviewSelect>>',self.show_detail)
        self.detail=tk.Label(root,text='项目只记录原目录及索引，解析不复制游戏资源。',
                             anchor='w',justify='left',bg='#f4f7f6',fg='#64786e',wraplength=850)
        self.detail.pack(fill='x',padx=20,pady=10)
        self.buttons=[]
        for actions in [[('添加游戏',self.add),('导入已有项目',self.import_existing),('重新检测',self.redetect),('重新定位',self.relocate)],
                        [('解析剧情',self.build),('一键翻译整部游戏',self.translate),('打开阅读器',self.open_reader)]]:
            bar=tk.Frame(root,bg='#f4f7f6')
            bar.pack(fill='x',padx=16,pady=4)
            for label,action in actions:
                button=tk.Button(bar,text=label,command=action,padx=10,pady=5)
                button.pack(side='left',padx=4)
                self.buttons.append(button)
        self.status=tk.Label(root,text='请选择或添加游戏。',anchor='w',justify='left',wraplength=850,bg='#f4f7f6',fg='#263d35')
        self.status.pack(fill='x',padx=20,pady=12)
        self.refresh()
        root.after(80,self.poll)

    def refresh(self, selected=None):
        selected=selected or (self.tree.selection()[0] if self.tree.selection() else None)
        self.items={item['id']:item for item in self.manager.list()}
        for child in self.tree.get_children():self.tree.delete(child)
        for item in self.items.values():
            state='已解析' if item.get('parsed_fingerprint') else '待解析'
            if not Path(item['game_dir']).is_dir():state='目录已移动'
            elif item['engine']=='unknown':state='暂不支持'
            elif item.get('parsed_fingerprint') and item['parsed_fingerprint'] != self.manager.fingerprint(Path(item['game_dir'])):
                state='文件已变动，请解析'
            self.tree.insert('', 'end',iid=item['id'],text=item['name'],values=(item['engine'],state))
        if selected in self.items:self.tree.selection_set(selected)

    def selected(self):
        selection=self.tree.selection()
        if not selection:raise ValueError('请先选择一个游戏项目。')
        return self.items[selection[0]]

    def show_detail(self, _event=None):
        if not self.tree.selection():return
        item=self.selected()
        self.detail.configure(text=f"原游戏：{item['game_dir']}\n项目输出：{item['output_dir']}")

    def submit(self, work, message):
        if self.busy:return
        self.busy=True
        for button in self.buttons:button.configure(state='disabled')
        self.status.configure(text=message)
        def worker():
            try:self.events.put(('ok',work()))
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=True,name='game-project-operation').start()

    def poll(self):
        if self.closed:return
        try:
            while True:
                kind,result=self.events.get_nowait()
                self.busy=False
                for button in self.buttons:button.configure(state='normal')
                if kind=='error':
                    self.status.configure(text=result)
                else:
                    self.refresh(result.get('id') if isinstance(result,dict) else None)
                    self.status.configure(text='操作完成。' if isinstance(result,dict) else str(result))
        except queue.Empty:pass
        self.root.after(80,self.poll)

    def add(self):
        path=filedialog.askdirectory(parent=self.root,title='选择原游戏目录')
        if path:self.submit(lambda:self.manager.add(Path(path)),'正在检测引擎……')

    def import_existing(self):
        path=filedialog.askopenfilename(parent=self.root,title='选择现有 story_flow.json',filetypes=[('剧情索引','story_flow.json'),('JSON','*.json')])
        if path:self.submit(lambda:self.manager.import_existing(path),'正在登记已有项目，保留原输出目录……')

    def redetect(self):
        try:item=self.selected()
        except ValueError as exc:self.status.configure(text=str(exc)); return
        self.submit(lambda:self.manager.refresh(item['id']),'正在重新检测……')

    def relocate(self):
        try:item=self.selected()
        except ValueError as exc:self.status.configure(text=str(exc)); return
        path=filedialog.askdirectory(parent=self.root,title='重新定位原游戏目录')
        if path:self.submit(lambda:self.manager.relocate(item['id'],path),'正在校验新目录……')

    def build(self):
        try:item=self.selected()
        except ValueError as exc:self.status.configure(text=str(exc)); return
        if self._active_reader(item['id']) or self._active_translation(item['id']):
            self.status.configure(text='请先关闭此项目的阅读器并暂停翻译，再重新解析。'); return
        self.submit(lambda:f"解析完成：{self.manager.build(item['id'])}",'正在只读解析剧情……')

    def _active_reader(self, project_id):
        child=self.readers.get(project_id)
        return bool(child and child.poll() is None)

    def _active_translation(self, project_id):
        window=self.batch_windows.get(project_id)
        return bool(window and window.busy())

    def translate(self):
        try:
            selected=self.selected()
            # Translations need only the built index, including when the game is moved.
            item=self.manager.get(selected['id'])
            path=Path(item['output_dir'])/'story_flow.json'
            if not path.is_file():raise ValueError('请先解析剧情，生成小说索引。')
            window=self.batch_windows.get(item['id'])
            if window and not window.closed:
                window.window.lift(); return
            if window and window.busy():raise ValueError('上一项翻译任务正在暂停，请稍后再打开。')
            from hgalgame.frontend.batch_translation_window import BatchTranslationWindow
            self.batch_windows[item['id']]=BatchTranslationWindow(self.root,json.loads(path.read_text('utf-8')),path.parent)
        except Exception as exc:self.status.configure(text=str(exc))

    def open_reader(self):
        try:
            item,path=self.manager.require_ready(self.selected()['id'])
            if self._active_reader(item['id']):raise ValueError('此项目阅读器已打开。')
            env=os.environ.copy()
            env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
            env['PYTHONUTF8']='1'
            with (path.parent/'reader_startup.log').open('ab') as log:
                process=subprocess.Popen([sys.executable,'-u','-m','hgalgame.cli','story-ui',item['game_dir'],str(path.parent),'--overlay'],
                                         env=env,stdout=log,stderr=subprocess.STDOUT,
                                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.readers[item['id']]=process
            self.status.configure(text='阅读器正在启动；启动详情记录在项目 reader_startup.log。')
        except Exception as exc:self.status.configure(text=str(exc))

    def close(self):
        if self.busy:
            self.status.configure(text='正在解析或登记项目，请等待本次操作完成后关闭。'); return
        for window in self.batch_windows.values():
            if not window.closed:window.close()
        self.closed=True
        self.root.destroy()


def run(directory: Path):
    root=tk.Tk()
    ProjectManagerWindow(root,directory)
    root.mainloop()
