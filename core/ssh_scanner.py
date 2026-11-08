"""SSH 远程高速扫描器与远程文件系统操作模块。

针对群晖 NAS (Synology DSM) 等 Linux 远程服务器：
1. 在远端执行轻量纯标准库 Python 扫描脚本，充分利用 NAS 本地 I/O（速度可达数千文件/秒）；
2. 扫描产物直接在远端生成标准 WhoShitsOnMyC SQLite 快照，通过 SSH 流式传回本地；
3. 支持远程安全删除（优先移入群晖各个共享文件夹的 #recycle 回收站，支持彻底删除）。
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import sqlite3
import threading
import time
from typing import Any, Callable

try:
    import paramiko
except ImportError:
    paramiko = None

from . import applog, i18n

# 运行在 NAS / Linux 远端的轻量高性能扫描脚本（纯标准库，无需任何外部依赖）

_CREDENTIALS_CACHE: dict[str, dict[str, Any]] = {}


def cache_credentials(host: str, port: int, user: str, password: str) -> None:
    """在内存中缓存连接凭证，用于后续的快照删除等交互。"""
    key = f"{host.strip()}:{int(port)}:{user.strip()}"
    _CREDENTIALS_CACHE[key] = {
        "host": host.strip(),
        "port": int(port),
        "user": user.strip(),
        "password": password,
    }


def get_cached_credentials(host: str, port: int, user: str = "") -> dict[str, Any] | None:
    """根据 host 和 port 获取缓存的凭证。"""
    h = host.strip()
    p = int(port)
    u = user.strip()
    if u:
        key = f"{h}:{p}:{u}"
        if key in _CREDENTIALS_CACHE:
            return _CREDENTIALS_CACHE[key]
    # 回退按 host:port 匹配
    for k, v in _CREDENTIALS_CACHE.items():
        if v.get("host") == h and v.get("port") == p:
            return v
    # 若整个缓存只有一条，直接返回
    if len(_CREDENTIALS_CACHE) == 1:
        return next(iter(_CREDENTIALS_CACHE.values()))
    return None

_REMOTE_SCANNER_SCRIPT = """
import os
import sys
import stat
import time
import sqlite3
import json

target_root = sys.argv[1]
db_path = sys.argv[2]
host = sys.argv[3]
port = sys.argv[4]
user = sys.argv[5]

target_root = os.path.abspath(target_root)

# 初始化 SQLite 快照库
conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute("PRAGMA synchronous = OFF")
cur.execute("PRAGMA journal_mode = OFF")
cur.execute("PRAGMA temp_store = MEMORY")

cur.execute("DROP TABLE IF EXISTS entries")
cur.execute("DROP TABLE IF EXISTS meta")
cur.execute(
    "CREATE TABLE entries ("
    "  id INTEGER PRIMARY KEY,"
    "  parent_id INTEGER,"
    "  name TEXT NOT NULL,"
    "  size INTEGER NOT NULL,"
    "  is_dir INTEGER NOT NULL,"
    "  mtime INTEGER NOT NULL DEFAULT 0,"
    "  reparse_tag INTEGER NOT NULL DEFAULT 0,"
    "  link_target TEXT NOT NULL DEFAULT ''"
    ")"
)
cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
conn.commit()

next_id = 2
root_id = 1

dir_stack = [(root_id, target_root)]
# 极简轻量结构: (id, parent_id, name, mtime)
dir_nodes = [(root_id, None, os.path.basename(target_root) or target_root, 0)]
dir_sizes = {root_id: 0}

file_count = 0
total_size = 0
last_progress = time.time()
batch_files = []

# 跳过群晖专属缓存与无用系统缩略图碎片目录
SKIPPED_NAMES = frozenset({"@eaDir", "@synoEA", ".DS_Store", "._DAV", "@tmp"})

def safe_str(s):
    try:
        return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except Exception:
        return str(s)

def flush_files():
    global batch_files
    if not batch_files:
        return
    chunk = batch_files
    batch_files = []
    try:
        cur.executemany(
            "INSERT OR REPLACE INTO entries (id, parent_id, name, size, is_dir, mtime, reparse_tag, link_target) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            chunk
        )
    except Exception:
        pass

while dir_stack:
    p_id, cur_path = dir_stack.pop()
    try:
        with os.scandir(cur_path) as it:
            for entry in it:
                raw_name = entry.name
                if raw_name in SKIPPED_NAMES:
                    continue
                name = safe_str(raw_name)
                try:
                    if entry.is_symlink():
                        cur_id = next_id
                        next_id += 1
                        file_count += 1
                        try:
                            link_tgt = safe_str(os.readlink(entry.path))
                        except Exception:
                            link_tgt = ""
                        batch_files.append((cur_id, p_id, name, 0, 0, 0, 0xA000000C, link_tgt))
                        continue
                    
                    if entry.is_dir(follow_symlinks=False):
                        cur_id = next_id
                        next_id += 1
                        dir_sizes[cur_id] = 0
                        dir_nodes.append((cur_id, p_id, name, 0))
                        dir_stack.append((cur_id, entry.path))
                    else:
                        cur_id = next_id
                        next_id += 1
                        st = entry.stat(follow_symlinks=False)
                        sz = st.st_size
                        mt = int(st.st_mtime)
                        batch_files.append((cur_id, p_id, name, sz, 0, mt, 0, ""))
                        dir_sizes[p_id] += sz
                        total_size += sz
                        file_count += 1
                        
                        if len(batch_files) >= 5000:
                            flush_files()
                except Exception:
                    pass
    except Exception:
        pass

    now = time.time()
    if now - last_progress >= 0.15:
        sys.stdout.write(json.dumps({"files": file_count, "current": cur_path}) + "\\n")
        sys.stdout.flush()
        last_progress = now

flush_files()

# 自底向上聚合目录大小
for cur_id, p_id, name, mt in reversed(dir_nodes):
    sub_size = dir_sizes.get(cur_id, 0)
    if p_id is not None and p_id in dir_sizes:
        dir_sizes[p_id] += sub_size

dir_rows = []
for cur_id, p_id, name, mt in dir_nodes:
    dir_rows.append((cur_id, p_id, name, dir_sizes.get(cur_id, 0), 1, mt, 0, ""))
try:
    cur.executemany(
        "INSERT OR REPLACE INTO entries (id, parent_id, name, size, is_dir, mtime, reparse_tag, link_target) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        dir_rows
    )
except Exception:
    pass

cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_link ON entries(reparse_tag, link_target) WHERE reparse_tag <> 0")

free_bytes = 0
try:
    vfs = os.statvfs(target_root)
    free_bytes = vfs.f_bavail * vfs.f_frsize
except Exception:
    pass

display_root = f"ssh://{host}:{port}{target_root}" if str(port) != "22" else f"ssh://{host}{target_root}"

meta_rows = [
    ("root", display_root),
    ("remote_root", target_root),
    ("scanned_at", str(int(time.time()))),
    ("total_size", str(total_size)),
    ("file_count", str(file_count)),
    ("skipped_count", "0"),
    ("free_size", str(free_bytes)),
    ("format_version", "5"),
    ("is_ssh", "1"),
    ("ssh_host", host),
    ("ssh_port", str(port)),
    ("ssh_user", user),
    ("compressed", "0"),
]
cur.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", meta_rows)
conn.commit()
conn.close()

sys.stdout.write(f"SCAN_SUCCESS:{db_path}:{file_count}:{total_size}\\n")
sys.stdout.flush()
"""


def _check_paramiko() -> None:
    if paramiko is None:
        raise RuntimeError(
            i18n.t(
                "缺少 SSH 依赖 paramiko，请先运行 pip install paramiko",
                "Missing paramiko dependency, please run: pip install paramiko",
            )
        )


def connect_ssh(
    host: str,
    port: int = 22,
    user: str = "",
    password: str = "",
    timeout: int = 10,
) -> Any:
    """建立 SSH 连接并返回 client 实例。"""
    _check_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host.strip(),
        port=int(port),
        username=user.strip(),
        password=password,
        timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    cache_credentials(host, port, user, password)
    return client


def test_ssh_connection(
    host: str,
    port: int,
    user: str,
    password: str,
) -> dict[str, Any]:
    """测试 SSH 连接并自动探测群晖常见的共享路径。"""
    try:
        ssh = connect_ssh(host, port, user, password, timeout=8)
    except Exception as exc:
        applog.exception("SSH connection failed", exc)
        return {"ok": False, "error": str(exc)}

    shares: list[str] = []
    try:
        cmd = "ls -d /volume*/*/ /volumeUSB*/*/ 2>/dev/null"
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=5)
        for line in stdout:
            p = line.strip().rstrip("/")
            if p and not p.endswith(("/#recycle", "/@eaDir", "/@tmp")):
                # 优先展示非 @ 开头的用户共享文件夹
                parts = p.split("/")
                folder_name = parts[-1] if parts else ""
                if not folder_name.startswith("@"):
                    shares.append(p)
    except Exception as exc:
        applog.warning(f"Failed to list remote directories: {exc}")
    finally:
        try:
            ssh.close()
        except Exception:
            pass

    return {
        "ok": True,
        "shares": sorted(list(set(shares))),
        "message": i18n.t("连接成功", "Connection successful"),
    }


def execute_remote_scan(
    host: str,
    port: int,
    user: str,
    password: str,
    remote_root: str,
    local_db_path: str,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """在 NAS 上高速执行扫描，并把生成的快照 DB 传输回本地 local_db_path。"""
    _check_paramiko()
    clean_root = posixpath.normpath(remote_root.strip())
    if not clean_root or clean_root == ".":
        raise ValueError(i18n.t("远程路径无效", "Invalid remote path"))

    ssh = connect_ssh(host, port, user, password, timeout=12)
    b64_script = base64.b64encode(_REMOTE_SCANNER_SCRIPT.encode("utf-8")).decode("ascii")

    remote_tmp_db = f"/tmp/wsmc_scan_{int(time.time())}_{os.getpid()}.db"
    run_cmd = (
        f'python3 -u -c "import base64; exec(base64.b64decode(\'{b64_script}\').decode(\'utf-8\'))" '
        f'"{clean_root}" "{remote_tmp_db}" "{host}" "{port}" "{user}"'
    )

    t0 = time.time()
    stdin, stdout, stderr = ssh.exec_command(run_cmd)

    success_info: list[str] | None = None
    last_ui_update = 0.0

    try:
        while True:
            if cancel_event and cancel_event.is_set():
                # 中断远端进程
                try:
                    ssh.exec_command(f"rm -f '{remote_tmp_db}'")
                except Exception:
                    pass
                raise RuntimeError(i18n.t("用户取消扫描", "Scan cancelled by user"))

            line = stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue

            if line_str.startswith("SCAN_SUCCESS:"):
                success_info = line_str.split(":")
                break
            elif line_str.startswith("{"):
                try:
                    data = json.loads(line_str)
                    now = time.time()
                    if on_progress and (now - last_ui_update >= 0.15):
                        on_progress(data)
                        last_ui_update = now
                except Exception:
                    pass

        # 检查是否成功
        if not success_info or len(success_info) < 4:
            err_output = stderr.read().decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                i18n.t(
                    f"远程扫描失败：{err_output or '未收到成功状态'}",
                    f"Remote scan failed: {err_output or 'No success status received'}",
                )
            )

        file_count = int(success_info[2])
        total_size = int(success_info[3])

        # 通过 SSH 二进制流快速将远端 DB 传回本地
        fetch_cmd = f"cat '{remote_tmp_db}' && rm -f '{remote_tmp_db}'"
        f_stdin, f_stdout, f_stderr = ssh.exec_command(fetch_cmd)

        os.makedirs(os.path.dirname(os.path.abspath(local_db_path)), exist_ok=True)
        with open(local_db_path, "wb") as f_out:
            while True:
                chunk = f_stdout.read(65536)
                if not chunk:
                    break
                f_out.write(chunk)

        elapsed = time.time() - t0
        return {
            "ok": True,
            "file_count": file_count,
            "total_size": total_size,
            "elapsed_s": round(elapsed, 2),
            "local_path": local_db_path,
        }

    finally:
        try:
            ssh.close()
        except Exception:
            pass


def remote_delete(
    host: str,
    port: int,
    user: str,
    password: str,
    remote_path: str,
    permanent: bool = False,
) -> dict[str, Any]:
    """在远程 NAS 上执行安全删除操作。

    - permanent=False：优先移入群晖各个共享目录下的 `#recycle` 回收站，保留找回能力；
    - permanent=True：执行彻底删除 `rm -rf`。
    """
    _check_paramiko()
    clean = posixpath.normpath(remote_path.strip())
    # 严格禁止删除危险路径或根路径
    if clean in ("", "/", "/volume1", "/volume2", "/volume3", "/root", "/volume1/home"):
        raise ValueError(i18n.t(f"禁止删除系统保护路径：{clean}", f"Prohibited path: {clean}"))

    ssh = connect_ssh(host, port, user, password, timeout=10)
    try:
        # 确认目标存在
        clean_escaped = clean.replace('"', '\\"')
        stdin, stdout, _ = ssh.exec_command(f'test -e "{clean_escaped}" && echo "EXISTS"')
        if stdout.read().decode().strip() != "EXISTS":
            raise FileNotFoundError(i18n.t(f"远程路径不存在：{clean}", f"Remote path not found: {clean}"))

        if permanent:
            stdin, stdout, stderr = ssh.exec_command(f'rm -rf "{clean_escaped}"')
            err = stderr.read().decode().strip()
            if err:
                raise RuntimeError(err)
            return {"ok": True, "action": "permanent_deleted", "path": clean}
        else:
            # 查找同级或上级目录中的 #recycle 回收站
            cur = clean
            recycle_dir = None
            while cur and cur != "/":
                parent = posixpath.dirname(cur)
                candidate = posixpath.join(parent, "#recycle")
                c_escaped = candidate.replace('"', '\\"')
                stdin, stdout, _ = ssh.exec_command(f'test -d "{c_escaped}" && echo "OK"')
                if stdout.read().decode().strip() == "OK":
                    recycle_dir = candidate
                    break
                cur = parent

            if not recycle_dir:
                recycle_dir = "~/.Trash"
                ssh.exec_command(f"mkdir -p {recycle_dir}")

            base_name = posixpath.basename(clean)
            dest = posixpath.join(recycle_dir, base_name)
            dest_escaped = dest.replace('"', '\\"')
            # 时间戳避免重名覆盖
            cmd = (
                f'if [ -e "{dest_escaped}" ]; then '
                f'mv "{clean_escaped}" "{dest_escaped}.$(date +%Y%m%d_%H%M%S)"; '
                f'else mv "{clean_escaped}" "{dest_escaped}"; fi'
            )
            stdin, stdout, stderr = ssh.exec_command(cmd)
            err = stderr.read().decode().strip()
            if err:
                raise RuntimeError(err)
            return {"ok": True, "action": "recycled", "recycle_dir": recycle_dir, "path": clean}
    finally:
        try:
            ssh.close()
        except Exception:
            pass
