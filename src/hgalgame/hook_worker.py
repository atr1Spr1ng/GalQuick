"""Private LunaHost host process. No GUI, network, auto-elevation or game launch.

ABI inspected in LunaTranslator texthook.py and LunaHostDll.cpp. Components
must come from one user-selected installation; they are not redistributed.
"""
import argparse
import ast
import ctypes as c
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


class ThreadParam(c.Structure):
    _fields_ = [('pid', c.c_uint32), ('addr', c.c_uint64), ('ctx', c.c_uint64), ('ctx2', c.c_uint64)]


def bundle_files(root):
    root = Path(root).resolve()
    host = root / 'files/LunaHook' / ('LunaHost64.dll' if c.sizeof(c.c_void_p) == 8 else 'LunaHost32.dll')
    files = [host] + [root / f'files/LunaHook/LunaHook{bit}.dll' for bit in (32,64)]
    files += [root / f'files/LunaSubprocess{bit}.exe' for bit in (32,64)]
    if not all(p.is_file() for p in files):
        raise ValueError('请选择包含 files/LunaHook 和 LunaSubprocess 的完整 LunaTranslator 目录。')
    return root, host


def settings_arity(root):
    """Inspect the companion wrapper without importing any of its code."""
    wrapper=Path(root)/'LunaTranslator/textio/textsource/texthook.py'
    if not wrapper.is_file():raise ValueError('缺少配套 texthook.py，无法确认组件 ABI。')
    tree=ast.parse(wrapper.read_text('utf-8-sig'))
    for node in ast.walk(tree):
        if not isinstance(node,ast.Assign) or not isinstance(node.value,(ast.Tuple,ast.List)):continue
        for target in node.targets:
            if isinstance(target,ast.Attribute) and target.attr=='argtypes' and isinstance(target.value,ast.Attribute) and target.value.attr=='Luna_Settings':
                names=[x.id if isinstance(x,ast.Name) else '' for x in node.value.elts]
                if names==['c_int','c_bool','c_int','c_int','c_int','c_bool']:return 6
                if names==['c_int','c_int','c_int','c_int','c_bool']:return 5
    raise ValueError('未识别的 LunaHost ABI；请使用完整、同版本组件。')


def check_injector_result(returncode):
    """LunaSubprocess dllinject returns 1 on success, NOT shell-style 0.

    A successful injector exit still requires the separate Host connection
    callback. Reject crashes as well as the injector's explicit failure (0).
    """
    if returncode != 1:
        raise ValueError(f'Hook 注入器失败，返回码 {returncode} (0x{returncode & 0xffffffff:08X})；未自动提权或修改安全设置。')


def run(root, pid, expected, emit, stop, *, host_probe=False):
    root, host_path = bundle_files(root)
    arity=settings_arity(root)
    if os.name != 'nt': raise ValueError('Hook 仅支持 Windows。')
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [c.c_uint32,c.c_bool,c.c_uint32]
    kernel.OpenProcess.restype = c.c_void_p
    kernel.CloseHandle.argtypes = [c.c_void_p]
    kernel.QueryFullProcessImageNameW.argtypes = [c.c_void_p,c.c_uint32,c.c_wchar_p,c.POINTER(c.c_uint32)]
    kernel.IsWow64Process.argtypes = [c.c_void_p,c.POINTER(c.c_int)]
    kernel.WaitForSingleObject.argtypes = [c.c_void_p,c.c_uint32]
    handle = kernel.OpenProcess(0x1000 | 0x100000,False,pid)
    if not handle: raise OSError('无法访问游戏进程；不会自动提权。'+str(c.WinError(c.get_last_error())))
    dll = None
    attached = False
    connected=threading.Event()
    try:
        size=c.c_uint32(32768); name=c.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle,0,name,c.byref(size)):
            raise ValueError('无法校验游戏进程。')
        if Path(name.value).resolve() != Path(expected).resolve():
            raise ValueError('游戏进程路径不匹配，未注入。')
        wow=c.c_int()
        if not kernel.IsWow64Process(handle,c.byref(wow)): raise ValueError('无法检测游戏位数。')
        bit=32 if wow.value else 64
        emit(dict(type='status',status='initializing_host'))
        dll=c.CDLL(str(host_path))
        proc=c.CFUNCTYPE(None,c.c_uint32)
        created=c.CFUNCTYPE(None,c.c_wchar_p,c.c_char_p,ThreadParam,c.c_bool)
        removed=c.CFUNCTYPE(None,c.c_wchar_p,c.c_char_p,ThreadParam)
        output=c.CFUNCTYPE(None,c.c_wchar_p,c.c_char_p,ThreadParam,c.c_wchar_p)
        info=c.CFUNCTYPE(None,c.c_int,c.c_wchar_p)
        inserted=c.CFUNCTYPE(None,c.c_uint32,c.c_uint64,c.c_wchar_p)
        embed=c.CFUNCTYPE(None,c.c_wchar_p,ThreadParam)
        i18n=c.CFUNCTYPE(c.c_void_p,c.c_wchar_p)
        emu=c.CFUNCTYPE(None,c.c_wchar_p,c.c_wchar_p,c.c_wchar_p)
        dll.Luna_AllocString.argtypes=[c.c_wchar_p]; dll.Luna_AllocString.restype=c.c_void_p
        dll.Luna_ConnectProcess.argtypes=[c.c_uint32]
        dll.Luna_CheckIfNeedInject.argtypes=[c.c_uint32]; dll.Luna_CheckIfNeedInject.restype=c.c_bool
        dll.Luna_DetachProcess.argtypes=[c.c_uint32]
        dll.Luna_Settings.argtypes=([c.c_int,c.c_bool,c.c_int,c.c_int,c.c_int,c.c_bool] if arity==6
                                    else [c.c_int,c.c_int,c.c_int,c.c_int,c.c_bool])
        dll.Luna_Start.argtypes=[proc,proc,created,removed,output,info,inserted,embed,i18n,emu]
        def received(code, hook_name, tp, text):
            if tp.pid != pid or not text: return
            emit(dict(type='text',text=text[:20000],stream=f'{code}|{tp.ctx:x}|{tp.ctx2:x}',
                      name=(hook_name or b'').decode('utf-8','replace')))
        def on_connect(p):
            if p==pid:
                connected.set()
                emit(dict(type='status',status='connected'))
        callbacks=[proc(on_connect),
            proc(lambda p:stop.set() if p==pid else None), created(lambda *a:None), removed(lambda *a:None),
            output(received),info(lambda *a:None),inserted(lambda *a:None),embed(lambda *a:None),
            i18n(lambda s:dll.Luna_AllocString(s)),emu(lambda *a:None)]
        dll.Luna_Start(*callbacks)
        if arity==6:dll.Luna_Settings(150,False,932,3000,100000,False)
        else:dll.Luna_Settings(150,932,3000,100000,False)
        if host_probe:
            emit(dict(type='status',status='host_ready',injected=False))
            return
        emit(dict(type='status',status='connecting'))
        dll.Luna_ConnectProcess(pid)
        attached=True
        if dll.Luna_CheckIfNeedInject(pid):
            result=subprocess.run([str(root/f'files/LunaSubprocess{bit}.exe'),'dllinject',str(pid),
                str(root/f'files/LunaHook/LunaHook{bit}.dll')],cwd=root,creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
            check_injector_result(result.returncode)
        if not connected.is_set():emit(dict(type='status',status='waiting'))
        deadline=time.monotonic()+20
        while not stop.wait(.2):
            if kernel.WaitForSingleObject(handle,0)==0:
                emit(dict(type='status',status='game_exited'))
                break
            if not connected.is_set() and time.monotonic()>deadline:
                raise TimeoutError('注入请求发出后未收到连接确认，请检查权限、拦截或其他 Hook 主机。')
    finally:
        if dll is not None and attached:
            dll.Luna_DetachProcess(pid)
        kernel.CloseHandle(handle)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True); parser.add_argument('--pid',type=int)
    parser.add_argument('--exe');parser.add_argument('--check',action='store_true')
    parser.add_argument('--check-start',action='store_true',help='initialize Host only, never connect or inject')
    args=parser.parse_args()
    if args.check:
        root,host=bundle_files(args.root)
        arity=settings_arity(root)
        dll=c.CDLL(str(host))
        for name in ('Luna_Start','Luna_Settings','Luna_ConnectProcess','Luna_CheckIfNeedInject','Luna_DetachProcess','Luna_AllocString'):
            getattr(dll,name)
        print(json.dumps(dict(valid=True,settings_arity=arity,host_bits=c.sizeof(c.c_void_p)*8,injected=False)))
        return 0
    if args.check_start:
        run(args.root,os.getpid(),sys.executable,lambda event:print(json.dumps(event),flush=True),threading.Event(),host_probe=True)
        return 0
    if not args.pid or not args.exe:parser.error('--pid and --exe required unless --check')
    stop=threading.Event(); lock=threading.Lock()
    def emit(event):
        with lock:
            print(json.dumps(event,ensure_ascii=True),flush=True)
    def commands():
        for line in sys.stdin:
            if line.strip()=='stop': break
        stop.set()
    threading.Thread(target=commands,daemon=True).start()
    try: run(args.root,args.pid,args.exe,emit,stop)
    except Exception as exc:
        emit(dict(type='status',status='error',detail=f'{type(exc).__name__}: {exc}'[:1000]))
        return 1
    return 0


if __name__=='__main__': sys.exit(main())
