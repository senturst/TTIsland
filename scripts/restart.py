"""跨平台重启开发服务器。

为什么需要这个脚本：
    uvicorn 的 --reload 会派生子进程，直接 kill 父进程常常留下占着端口的孤儿；
    而端口被占时新进程启动失败（WinError 10048），你却以为"代码改动没生效"，
    实际跑的还是旧代码——这个坑每次都要花十分钟排查。

行为：
    1. 按端口找出监听进程（含 uvicorn 孤儿进程）并结束
    2. 等待端口真正释放
    3. 校验配置与数据库
    4. 后台启动 uvicorn，日志写到 logs/server.log，PID 写到 data/server.pid

用法：
    python scripts/restart.py            # 后台启动
    python scripts/restart.py --fg       # 前台启动（直接看日志）
    python scripts/restart.py --stop     # 只停止
    python scripts/restart.py --reload   # 开启代码热重载
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "data" / "server.pid"
LOG_FILE = ROOT / "logs" / "server.log"
PORT = int(os.environ.get("PORT", "8000"))


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_by_port(port: int) -> int:
    """结束占用端口的进程。返回结束的数量。"""
    killed = 0
    if sys.platform == "win32":
        # Windows 的 netstat 输出用的是系统 ANSI 编码（中文环境为 GBK），
        # 用 text=True 会解码失败并让 stdout 变成 None。按字节读再宽松解码即可：
        # 我们只关心端口与 PID，这两列都是 ASCII。
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True).stdout
        out = raw.decode("utf-8", "replace")
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                pids.add(parts[4])
        for pid in pids:
            r = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
            if r.returncode == 0:
                killed += 1
                print(f"  结束进程 PID {pid}")
    else:
        try:
            raw = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True).stdout
            for pid in raw.decode("utf-8", "replace").split():
                os.kill(int(pid), signal.SIGKILL)
                killed += 1
                print(f"  结束进程 PID {pid}")
        except FileNotFoundError:
            pass
    return killed


def stop() -> None:
    print(f"[1] 停止端口 {PORT} 上的服务 …")
    n = kill_by_port(PORT)
    if PID_FILE.exists():
        PID_FILE.unlink()
    print(f"  已结束 {n} 个进程" if n else "  端口本来就是空闲的")


def wait_free(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not port_busy(port):
            return True
        time.sleep(0.3)
    return not port_busy(port)


def preflight() -> bool:
    """启动前校验配置与数据库——配置有问题就别启动了，省得在运行时才炸。"""
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / ".venv" / "bin" / "python"
    code = "\n".join([
        "from app.data.loader import get_config",
        "from app.db import migrate",
        "c = get_config()",
        "migrate.apply_migrations()",
        "migrate.init_llm_cache_db()",
        "n = len(c.talents_cfg.get('talents') or [])",
        "print('      配置 OK：%d 物品 / %d 怪物 / %d 天赋 / %d 层'",
        "      % (len(c.items), len(c.monsters), n, c.max_level))",
    ])
    r = subprocess.run([str(py), "-c", code], cwd=ROOT, capture_output=True)
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode != 0:
        print(out)
        print(err[-700:])
        return False
    print(out)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true", help="只停止，不启动")
    ap.add_argument("--fg", action="store_true", help="前台运行")
    ap.add_argument("--reload", action="store_true", help="开启 Python 代码热重载")
    args = ap.parse_args()

    stop()
    if args.stop:
        return 0

    if not wait_free(PORT):
        print(f"!! 端口 {PORT} 仍被占用，请手动检查")
        return 1

    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / ".venv" / "bin" / "python"

    print("[2] 校验配置与数据库 …")
    if not preflight():
        print("配置校验失败，已中止启动。")
        return 1

    cmd = [str(py), "-m", "uvicorn", "app.main:app",
           "--host", "0.0.0.0", "--port", str(PORT), "--log-level", "info"]
    if args.reload:
        cmd += ["--reload", "--reload-dir", "app"]

    if args.fg:
        print(f"[3] 前台启动 http://127.0.0.1:{PORT}（Ctrl+C 停止）\n")
        return subprocess.call(cmd, cwd=ROOT)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"[3] 后台启动 http://127.0.0.1:{PORT}")
    print(f"    日志：{LOG_FILE.relative_to(ROOT)}")
    with LOG_FILE.open("wb") as fh:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    for _ in range(30):
        if port_busy(PORT):
            print(f"    已就绪（PID {proc.pid}）")
            return 0
        if proc.poll() is not None:
            print("!! 启动失败，日志尾部：")
            print(LOG_FILE.read_text(encoding="utf-8", errors="replace")[-900:])
            return 1
        time.sleep(0.4)
    print("!! 等待超时，请查看日志")
    return 1


if __name__ == "__main__":
    sys.exit(main())
