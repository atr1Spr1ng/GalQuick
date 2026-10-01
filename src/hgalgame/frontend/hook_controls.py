"""Native Hook settings and event routing, separate from engine parsing."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from hgalgame.output import write_json
from hgalgame.managed_hook import ManagedHook
from hgalgame.hook_worker import bundle_files
from hgalgame.luna_bridge import LocalTranslationMatcher


class HookControls:
    def _init_managed_hook(self):
        self._hook_config={}
        self._hook_streams={}
        self._hook_generation=0
        self._hook_status='未启用'
        self._hook_attempted=False
        self._hook_terminal_error=False
        try:
            value=json.loads((self.output_dir/'hook_settings.json').read_text('utf-8'))
            if isinstance(value,dict):self._hook_config=value
        except (OSError,ValueError):pass

    def _open_hook_settings(self):
        window=tk.Toplevel(self.root)
        window.title('字幕 Hook · 无需启动 LunaTranslator')
        window.geometry('620x340');window.attributes('-topmost',True)
        folder=tk.StringVar(value=self._hook_config.get('root',''))
        tk.Label(window,text='首次选择现有 LunaTranslator 安装目录；仅复用组件，不启动它的界面。',wraplength=570).pack(pady=8)
        tk.Entry(window,textvariable=folder).pack(fill='x',padx=12)
        def browse():
            value=filedialog.askdirectory(parent=window,title='选择有 files/LunaHook 的安装目录')
            if value:folder.set(value)
        tk.Button(window,text='选择组件目录',command=browse).pack(pady=5)
        status=tk.Label(window,text=self._hook_status,wraplength=570,justify='left');status.pack()
        selection=ttk.Combobox(window,state='readonly')
        selection.pack(fill='x',padx=12,pady=6)
        def select(_=None):
            i=selection.current()
            self._hook_config['stream']='' if i==0 else list(self._hook_streams)[i-1]
            write_json(self.output_dir/'hook_settings.json',self._hook_config)
            self._clear_live_subtitle()
        selection.bind('<<ComboboxSelected>>',select)
        def refresh():
            if not window.winfo_exists():return
            values=['自动：只显示当前 Scene 唯一匹配的译文']+[v[:90] for v in self._hook_streams.values()]
            selection['values']=values
            keys=list(self._hook_streams)
            stream=self._hook_config.get('stream','')
            selection.current(keys.index(stream)+1 if stream in keys else 0)
            status.configure(text=self._hook_status)
            window.after(500,refresh)
        def start():
            try:
                root,_=bundle_files(Path(folder.get()))
                self._hook_config.update(root=str(root),enabled=True)
                write_json(self.output_dir/'hook_settings.json',self._hook_config)
                previous=self._luna_bridge
                self._hook_generation+=1
                self._hook_attempted=True
                if previous:previous.stop()
                def restart():
                    if not self.root or not self._hook_config.get('enabled'):return
                    process=getattr(previous,'process',None)
                    if process and process.poll() is None:
                        self.root.after(100,restart);return
                    self._luna_bridge=None
                    self._hook_attempted=False
                    self._tick_managed_hook()
                restart()
            except Exception as exc:messagebox.showerror('Hook 未启动',str(exc),parent=window)
        def stop():
            self._hook_config['enabled']=False
            write_json(self.output_dir/'hook_settings.json',self._hook_config)
            self._hook_generation+=1
            if self._luna_bridge:self._luna_bridge.stop();self._luna_bridge=None
            self._hook_status='已停用';self._clear_live_subtitle()
        bar=tk.Frame(window);bar.pack(pady=8)
        tk.Button(bar,text='启用／重连（以后自动）',command=start).pack(side='left',padx=6)
        tk.Button(bar,text='停用',command=stop).pack(side='left',padx=6)
        tk.Label(window,text='状态为已连接后，游戏显示台词时才有文本流。\n自动模式匹配不到时，可手动选择有正文预览的流。',wraplength=560).pack()
        refresh()

    def _tick_managed_hook(self):
        if not self._hook_config.get('enabled') or self._hook_attempted:return
        pid=self.game_pid()
        if not pid:
            self._hook_status='等待游戏进程'
            return
        self._hook_attempted=True
        self._hook_terminal_error=False
        self._hook_generation+=1
        generation=self._hook_generation
        try:
            game=self.document.get('game',{})
            if game.get('engine')!='advhd':raise ValueError('当前自动连接仅验证了 AdvHD 入口。')
            executable=Path(game['game_dir'])/'AdvHD.exe'
            self._live_matcher=LocalTranslationMatcher(self._translation_library,
                self._translation_config.get('target','简体中文'),self.document)
            def event(value):
                gate=self._active_scene_gate
                self._events.put(('managed_hook',(generation,gate.id if gate else None,value)))
            self._luna_bridge=ManagedHook(self._hook_config['root'],pid,executable,event,
                diagnostic_path=self.output_dir/'hook_diagnostic.jsonl')
            self._luna_bridge.start()
            self._hook_status='连接中'
        except Exception as exc:
            self._hook_status='连接失败：'+str(exc)
        self._set_status('字幕 Hook：'+self._hook_status,self.MUTED)

    def _managed_hook_event(self,generation,scene_id,event):
        if generation!=self._hook_generation:return
        if event.get('type')=='status':
            status=event.get('status')
            if status=='disconnected' and getattr(self,'_hook_terminal_error',False):return
            if status in ('error','game_exited'):self._hook_terminal_error=True
            self._hook_status={'connected':'已连接','waiting':'已请求连接，等待台词',
                'initializing_host':'正在初始化 Host','connecting':'正在连接游戏进程',
                'game_exited':'游戏已退出，请重新打开阅读器',
                'error':'连接失败（组件、权限或版本不兼容）','disconnected':'已断开，可点击字幕 Hook 重连'}.get(status,str(status))
            if status=='error':
                self._hook_status='连接失败：'+str(event.get('detail') or '未知错误')
                code=event.get('exit_code')
                if isinstance(code,int):self._hook_status+=f'（0x{code & 0xffffffff:08X}）'
            self._set_status('字幕 Hook：'+self._hook_status,self.MUTED)
            if status in ('error','disconnected','game_exited'):self._clear_live_subtitle()
            return
        text=event.get('text');stream=event.get('stream')
        if not isinstance(text,str) or not isinstance(stream,str):return
        if stream in self._hook_streams or len(self._hook_streams)<100:
            self._hook_streams[stream]=str(event.get('name',''))+' · '+text[:70]
        selected=self._hook_config.get('stream')
        if selected and selected!=stream:return
        if not self._active_scene_gate or self._active_scene_gate.id!=scene_id:return
        if not selected:
            self._live_matcher.target=self._translation_config.get('target','简体中文')
            if not self._live_matcher.match(text,scene_id=scene_id):
                self._hook_status='已收到台词，未匹配；可手动选择文本流'
                self._render_live_subtitle(scene_id,text)
                return
        self._hook_status='已连接 · 正在接收台词'
        self._render_live_subtitle(scene_id,text)
