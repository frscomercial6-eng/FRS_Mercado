import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent

for target in ["FRS_Mercado.exe", "python.exe", "pythonw.exe"]:
    subprocess.run(["taskkill", "/F", "/IM", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

for p in [root / "dist", root / "build", root / "_secure_obf"]:
    if not p.exists():
        continue
    print(f"Removing stale build: {p}")
    for i in range(10):
        try:
            def _on_rm_error(func, path, exc_info):
                try:
                    os.chmod(path, stat.S_IWRITE)
                except Exception:
                    pass
                try:
                    func(path)
                except FileNotFoundError:
                    pass
            shutil.rmtree(p, onerror=_on_rm_error)
            print(f"Removed: {p}")
            break
        except Exception as exc:
            print(f"Retry remove {p} ({i + 1}/10): {exc}")
            time.sleep(1)
    else:
        raise RuntimeError(f"Could not remove stale build directory: {p}")

proc = subprocess.run([sys.executable, 'build_exe.py', '--skip-deploy'], cwd=str(root), capture_output=True, text=True)
print('BUILD_EXIT', proc.returncode)
if proc.stdout:
    print('--- STDOUT ---')
    print(proc.stdout[-2500:])
if proc.stderr:
    print('--- STDERR ---')
    print(proc.stderr[-2500:])
raise SystemExit(proc.returncode)
