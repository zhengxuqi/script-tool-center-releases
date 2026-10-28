"""The original Node application is hosted by the hub, not its Python queue."""
import shutil
import subprocess
import sys
from pathlib import Path


def health():
    node = shutil.which('node')
    if not node:
        raise ValueError('拼多多工具需要 Node.js 22 或以上版本。')
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    result = subprocess.run([node, '-e', 'if(Number(process.versions.node.split(".")[0])<22)process.exit(1);require("node:sqlite")'],
                            capture_output=True, timeout=15, creationflags=flags)
    if result.returncode:
        raise ValueError('拼多多工具需要支持 SQLite 的 Node.js 22 或以上版本。')
    for file in (Path(__file__).parent / 'src').glob('*.js'):
        subprocess.run([node, '--check', str(file)], capture_output=True, timeout=15,
                       creationflags=flags, check=True)


if __name__ == '__main__':
    if '--health-check' not in sys.argv:
        raise SystemExit('请在工具中心打开拼多多原采集页面。')
    health()
    print('ok')
