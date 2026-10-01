"""Open the persistent Windows-managed service without spawning duplicate servers."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'
TASK_NAME = 'CodexLocalLedger'
INSTANCE = hashlib.sha256(str(ROOT).casefold().encode('utf-8')).hexdigest()[:16]


def healthy(url=URL, instance=INSTANCE):
    try:
        with urllib.request.urlopen(url+'/health', timeout=2) as response:
            state = json.load(response)
        return state.get('app') == 'local-ledger' and state.get('instance') == instance
    except (OSError, ValueError, urllib.error.URLError):
        return False


def ensure_running(timeout=25):
    if healthy():
        return URL
    result = subprocess.run(['schtasks.exe', '/Run', '/TN', TASK_NAME],
                            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('后台任务未安装或不可启动。请先运行 install-background.cmd。')
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if healthy():
            return URL
        time.sleep(0.5)
    raise RuntimeError('后台任务未能恢复服务。请检查 data/runtime/service.log 与 Windows 任务计划程序中的 CodexLocalLedger。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        url = ensure_running()
        if not args.no_browser:
            webbrowser.open(url)
        print('服务已就绪：'+url)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
