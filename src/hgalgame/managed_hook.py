"""Own the lifetime of a hidden LunaHost worker, not LunaTranslator.exe."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from datetime import datetime, timezone

from hgalgame.hook_worker import bundle_files, settings_arity


class ManagedHook:
    def __init__(self, root, pid, executable, on_event, launcher=subprocess.Popen, diagnostic_path=None):
        self.root,self.pid,self.executable=Path(root),pid,Path(executable)
        self.on_event,self.launcher=on_event,launcher
        self.process=None
        self.stopping=False
        self.diagnostic_path=Path(diagnostic_path) if diagnostic_path else None

    def _publish(self,event):
        if self.diagnostic_path and event.get('type')=='status':
            try:
                self.diagnostic_path.parent.mkdir(parents=True,exist_ok=True)
                with self.diagnostic_path.open('a',encoding='utf-8') as log:
                    log.write(json.dumps(dict(event,time=datetime.now(timezone.utc).isoformat(),game_pid=self.pid),ensure_ascii=False)+'\n')
            except OSError:pass
        self.on_event(event)

    def start(self):
        bundle_files(self.root)
        settings_arity(self.root)
        if not isinstance(self.pid,int) or self.pid<=0: raise ValueError('游戏进程尚未就绪。')
        if self.process and self.process.poll() is None:return
        env=os.environ.copy(); env['PYTHONUTF8']='1'
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
        self.process=self.launcher([sys.executable,'-u','-m','hgalgame.hook_worker','--root',str(self.root),
            '--pid',str(self.pid),'--exe',str(self.executable)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',env=env,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        threading.Thread(target=self._read,daemon=True,name='hook-output').start()

    def _read(self):
        failure=None
        try:
            for line in self.process.stdout:
                if len(line)>200000: continue
                try:event=json.loads(line)
                except ValueError:continue
                if isinstance(event,dict) and event.get('type') in ('status','text'):
                    if event.get('status')=='error':failure=event
                    self._publish(event)
        except (OSError,UnicodeError) as exc:
            failure=dict(type='status',status='error',detail='Hook 通信失败：'+str(exc))
        finally:
            self.process.stdout.close()
            code=self.process.wait()
            if not self.stopping and (failure or isinstance(code,int) and code!=0):
                failure=failure or dict(type='status',status='error',detail='Hook 后台异常退出')
                self._publish(dict(failure,exit_code=code if isinstance(code,int) else None))
            else:self._publish(dict(type='status',status='disconnected'))

    def stop(self):
        if self.stopping:return
        self.stopping=True
        process=self.process
        if not process:return
        try:
            process.stdin.write('stop\n');process.stdin.flush();process.stdin.close()
        except (OSError,ValueError):pass
        def reap():
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate();process.wait()
        threading.Thread(target=reap,daemon=True,name='hook-stop').start()
