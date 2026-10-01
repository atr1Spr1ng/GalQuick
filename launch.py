"""Portable source launcher. Setup never downloads or launches games."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or sys.version_info < (3, 11):
        raise RuntimeError('Windows and Python 3.11+ are required.')
    root = Path(__file__).resolve().parent
    source = root / 'src'
    if not (source / 'hgalgame/cli.py').is_file():
        raise RuntimeError('Extract the whole package before starting.')
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONPATH'] = str(source)
    sys.path.insert(0, str(source))
    import tkinter
    import ctypes
    from hgalgame.project_manager_ui import run
    from hgalgame.frontend.native_overlay import NativeStoryOverlay
    if args.check:
        print(f'OK: Python {sys.version.split()[0]}, Tk {tkinter.TkVersion}, {ctypes.sizeof(ctypes.c_void_p)*8}-bit')
        print('Core imports passed. No game, Hook, network or project data accessed.')
        return
    run(root / 'projects')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
