"""扫描器测试：大小聚合、遍历、进度、取消、错误处理、多线程一致性。

v3 起扫描器是多线程的且不再暴露迭代器，测试统一走
「scan_to_snapshot 扫进临时库 → snapshot_map 展开 → 断言」的路径。
"""

import os
import time

import pytest

from core.scanner import ScanCancelled, scan_to_snapshot
from core.snapshot import read_meta


def _scan(root, tmp_path, **kwargs):
    """扫描一棵树到临时快照，返回 (db_path, meta)。"""
    db = os.path.join(tmp_path, "snap.db")
    meta = scan_to_snapshot(root, db, **kwargs)
    return db, meta


def test_file_sizes_recorded(make_tree, tmp_path, snapshot_map):
    root = make_tree({"a.txt": 100, "b.txt": 250})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["a.txt"].size == 100
    assert entries["a.txt"].is_dir is False
    assert entries["b.txt"].size == 250
    assert meta.file_count == 2


def test_directory_size_is_aggregated(make_tree, tmp_path, snapshot_map):
    root = make_tree({"sub": {"a.txt": 100, "b.txt": 50}, "c.txt": 25})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    # 子目录聚合 = 100 + 50
    assert entries["sub"].size == 150
    assert entries["sub"].is_dir is True
    # 根聚合 = 150 + 25
    assert meta.total_size == 175
    assert entries[""].size == 175


def test_mtime_recorded(make_tree, tmp_path, snapshot_map):
    """文件与目录都应带上真实的修改时间戳（刚创建，接近当前时间）。"""
    root = make_tree({"sub": {"a.txt": 100}})
    db, _ = _scan(root, tmp_path)

    entries = snapshot_map(db)
    now = time.time()
    assert abs(entries[os.path.join("sub", "a.txt")].mtime - now) < 300
    assert abs(entries["sub"].mtime - now) < 300
    assert abs(entries[""].mtime - now) < 300


def test_nested_aggregation(make_tree, tmp_path, snapshot_map):
    root = make_tree({"a": {"b": {"c": {"deep.txt": 999}}}})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries[os.path.join("a", "b", "c", "deep.txt")].size == 999
    assert entries[os.path.join("a", "b", "c")].size == 999
    assert entries[os.path.join("a", "b")].size == 999
    assert entries["a"].size == 999
    assert meta.total_size == 999


def test_root_entry_present_with_none_parent(make_tree, tmp_path, snapshot_map):
    root = make_tree({"a.txt": 10})
    db, _ = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert "" in entries
    assert entries[""].is_dir is True
    assert entries[""].parent_id is None
    assert entries[""].size == 10


def test_parent_id_pointers(make_tree, tmp_path, snapshot_map):
    root = make_tree({"sub": {"a.txt": 10}})
    db, _ = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["sub"].parent_id == entries[""].id
    assert entries[os.path.join("sub", "a.txt")].parent_id == entries["sub"].id


def test_empty_directory_has_zero_size(make_tree, tmp_path, snapshot_map):
    root = make_tree({"empty": {}, "a.txt": 5})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["empty"].size == 0
    assert entries["empty"].is_dir is True
    assert meta.dir_count == 2  # root + empty


def test_progress_callback_invoked(make_tree, tmp_path):
    root = make_tree({"a.txt": 1, "b.txt": 1})
    calls = []
    _scan(root, tmp_path, progress=lambda n, d: calls.append((n, d)))
    assert calls  # 至少回报过一次（收尾的最终计数）
    assert calls[-1][0] == 2


def test_cancel_raises(make_tree, tmp_path):
    root = make_tree({"a": {"b": {"c.txt": 1}}})
    with pytest.raises(ScanCancelled):
        _scan(root, tmp_path, cancel=lambda: True)


def test_workers_give_identical_result(make_tree, tmp_path, snapshot_map):
    """多 worker 与单 worker 扫描结果必须完全一致（对拍）。"""
    spec = {
        f"dir{i}": {f"f{j}.bin": (i * 7 + j) % 50 + 1 for j in range(8)}
        for i in range(6)
    }
    spec["top.txt"] = 33
    root = make_tree(spec)

    db1 = os.path.join(tmp_path, "one.db")
    db8 = os.path.join(tmp_path, "eight.db")
    meta1 = scan_to_snapshot(root, db1, now=1.0, workers=1)
    meta8 = scan_to_snapshot(root, db8, now=1.0, workers=8)

    assert meta1.total_size == meta8.total_size
    assert meta1.file_count == meta8.file_count
    assert meta1.dir_count == meta8.dir_count

    m1 = {p: (e.size, e.is_dir) for p, e in snapshot_map(db1).items()}
    m8 = {p: (e.size, e.is_dir) for p, e in snapshot_map(db8).items()}
    assert m1 == m8


def test_scan_to_snapshot_writes_readable_meta(make_tree, tmp_path):
    root = make_tree({"sub": {"a.txt": 100}, "b.txt": 50})
    db = os.path.join(tmp_path, "snap.db")

    meta = scan_to_snapshot(root, db, now=123.0)

    assert meta.total_size == 150
    assert meta.scanned_at == 123.0

    reloaded = read_meta(db)
    assert reloaded.total_size == 150
    assert reloaded.root == os.path.abspath(root)
    assert reloaded.file_count == 2


def test_scan_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_to_snapshot(os.path.join(tmp_path, "nope"), os.path.join(tmp_path, "x.db"))


def test_scan_file_as_root_raises(make_tree, tmp_path):
    root = make_tree({"a.txt": 10})
    file_path = os.path.join(root, "a.txt")
    with pytest.raises(NotADirectoryError):
        scan_to_snapshot(file_path, os.path.join(tmp_path, "x.db"))


def test_link_target_strips_prefix(monkeypatch):
    """_link_target 取 readlink 结果并剥掉内核前缀（junction / UNC）。"""
    import core.scanner as scanner

    monkeypatch.setattr(
        scanner.os, "readlink", lambda p: "\\\\?\\D:\\360download\\目标"
    )
    assert scanner._link_target("x") == r"D:\360download\目标"

    monkeypatch.setattr(
        scanner.os, "readlink", lambda p: "\\\\?\\UNC\\server\\share"
    )
    assert scanner._link_target("x") == r"\\server\share"

    def boom(_p):
        raise OSError(5, "access denied")

    monkeypatch.setattr(scanner.os, "readlink", boom)
    assert scanner._link_target("x") == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows junction only")
def test_real_junction_link_target(make_tree, tmp_path, snapshot_map):
    """Windows 实测：_winapi.CreateJunction 建的目录联接，readlink 取到目标。"""
    import _winapi

    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(root, "link")
    try:
        _winapi.CreateJunction(os.path.join(root, "real"), link)
    except (OSError, ValueError) as exc:  # 无权限/非 NTFS 时跳过，不误报
        pytest.skip(f"Cannot create junction: {exc}")

    db, _ = _scan(root, tmp_path)
    entries = snapshot_map(db)
    got = entries[os.path.join("link")]
    assert got.is_dir is True
    assert got.reparse_tag == 0xA0000003
    assert got.link_target == os.path.join(root, "real")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction only")
def test_scan_without_snapshot_reports_links(make_tree, tmp_path):
    """不要快照的扫描（反查的扫盘模式）不写文件，链接照常回调出来。"""
    import _winapi

    root = make_tree({"real": {"a.txt": 10}})
    try:
        _winapi.CreateJunction(os.path.join(root, "real"), os.path.join(root, "link"))
    except (OSError, ValueError) as exc:  # 无权限/非 NTFS 时跳过，不误报
        pytest.skip(f"Cannot create junction: {exc}")

    seen: list[tuple[str, int, str]] = []
    before = set(os.listdir(tmp_path))
    meta = scan_to_snapshot(
        root,
        None,
        on_link=lambda rel, tag, raw: seen.append((rel, tag, raw)),
    )
    assert set(os.listdir(tmp_path)) == before
    assert (meta.file_count, meta.dir_count) == (1, 3)
    assert seen == [("link", 0xA0000003, os.path.join(root, "real"))]


def test_safe_name_replaces_only_lone_surrogates():
    """孤立代理项替换为 U+FFFD；ASCII、中文、合法增补平面字符原样保留。"""
    import core.scanner as scanner

    assert scanner._safe_name("abc.txt") == "abc.txt"
    assert scanner._safe_name("测试目录") == "测试目录"
    assert scanner._safe_name("文档\U0001F600.txt") == "文档\U0001F600.txt"
    assert scanner._safe_name("x\ud818y") == "x\ufffdy"
    assert scanner._safe_name("\udc00") == "\ufffd"


@pytest.mark.skipif(os.name != "nt", reason="孤立代理项文件名仅 Windows 可创建")
def test_lone_surrogate_name_does_not_break_scan(make_tree, tmp_path, snapshot_map):
    """坏名字文件不再炸扫描：净化为 U+FFFD 入库，大小仍正确聚合。"""
    root = make_tree({"ok.txt": 40})
    bad = os.path.join(root, "bad\ud818name.txt")
    try:
        with open(bad, "wb") as f:
            f.write(b"\0" * 60)
    except (OSError, UnicodeEncodeError) as exc:  # 非 NTFS 等场景跳过，不误报
        pytest.skip(f"Cannot create surrogate-named file: {exc}")

    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["bad\ufffdname.txt"].size == 60
    assert entries["bad\ufffdname.txt"].is_dir is False
    assert entries["ok.txt"].size == 40
    assert meta.file_count == 2
    assert meta.total_size == 100


def test_mac_remote_mount_detection():
    """测试 macOS 下远程/网络文件系统（如 SMB/NAS）的探测。"""
    import sys
    from core.scanner import _is_remote_mount

    if sys.platform != "darwin":
        pytest.skip("macOS only")
    # 本地根不是 remote
    assert _is_remote_mount("/") is False
    # 若存在网络共享测试
    if os.path.exists("/Volumes/苏氏公开"):
        assert _is_remote_mount("/Volumes/苏氏公开") is True


def test_mac_scanner_does_not_descend_into_nas(make_tree, tmp_path, snapshot_map, monkeypatch):
    """测试 macOS 扫描根目录时不会扫进网络挂载目录。"""
    import sys
    import core.scanner as scanner

    root = make_tree({
        "Users": {"myuser": {"a.txt": 100}},
        "Volumes": {"nas": {"huge_file.bin": 9999999}},
    })

    # 模拟 root 为 "/" 扫描场景
    nas_path = os.path.join(root, "Volumes", "nas")

    # monkeypatch 让 scanner 把 root 当作 "/" 且 nas_path 识别为 mount
    orig_init = scanner._Scan.__init__

    def mock_init(self, meta, **kwargs):
        orig_init(self, meta, **kwargs)
        self.is_root_scan = True

    monkeypatch.setattr(scanner._Scan, "__init__", mock_init)
    
    orig_ismount = os.path.ismount
    def mock_ismount(path):
        if path == nas_path:
            return True
        return orig_ismount(path)

    monkeypatch.setattr(scanner.os.path, "ismount", mock_ismount)

    db, meta = _scan(root, tmp_path)
    entries = snapshot_map(db)

    # 普通目录被正常扫描
    assert entries[os.path.join("Users", "myuser", "a.txt")].size == 100
    # nas 目录被记录为挂载点目录（size=0），里面的 huge_file.bin 绝不进入快照
    assert os.path.join("Volumes", "nas") in entries
    assert entries[os.path.join("Volumes", "nas")].size == 0
    assert entries[os.path.join("Volumes", "nas")].is_dir is True
    assert os.path.join("Volumes", "nas", "huge_file.bin") not in entries
    assert meta.total_size == 100  # 仅 100，不含 NAS 上的 9999999


def test_mac_scanner_skips_remote_mount_under_normal_folder(make_tree, tmp_path, snapshot_map, monkeypatch):
    """测试 macOS 扫描普通子目录时，若遇到 NAS/远程挂载点也不会深入。"""
    import core.scanner as scanner

    root = make_tree({
        "docs": {"note.txt": 50},
        "mnt_nas": {"remote.bin": 888888},
    })
    nas_dir = os.path.join(root, "mnt_nas")

    orig_ismount = os.path.ismount
    def mock_ismount(path):
        if path == nas_dir:
            return True
        return orig_ismount(path)

    monkeypatch.setattr(scanner.os.path, "ismount", mock_ismount)
    monkeypatch.setattr(scanner, "_is_remote_mount", lambda p: p == nas_dir)

    db, meta = _scan(root, tmp_path)
    entries = snapshot_map(db)

    assert entries[os.path.join("docs", "note.txt")].size == 50
    assert "mnt_nas" in entries
    assert entries["mnt_nas"].size == 0
    assert os.path.join("mnt_nas", "remote.bin") not in entries
    assert meta.total_size == 50


def test_mac_scanner_skips_system_volumes_firmlink_duplicates(make_tree, tmp_path, snapshot_map):
    """测试 macOS 扫描根目录时跳过 /System/Volumes，避免 Data 卷重复计算翻倍。"""
    root = make_tree({
        "Users": {"alice": {"file.txt": 1000}},
        "System": {
            "Library": {"sys.dylib": 200},
            "Volumes": {
                "Data": {"Users": {"alice": {"file.txt": 1000}}},  # firmlink 镜像
                "VM": {"swapfile": 500},
            },
        },
    })

    db, meta = _scan(root, tmp_path)
    entries = snapshot_map(db)

    # 根层级的 Users 和 System/Library 正常计入
    assert entries[os.path.join("Users", "alice", "file.txt")].size == 1000
    assert entries[os.path.join("System", "Library", "sys.dylib")].size == 200

    # System/Volumes 被记为挂载点目录（size=0），内部 Data/VM 绝不重复扫描
    assert os.path.join("System", "Volumes") in entries
    assert entries[os.path.join("System", "Volumes")].size == 0
    assert os.path.join("System", "Volumes", "Data") not in entries

    # 总大小应为 1000 + 200 = 1200，绝不包含 Volumes 内部的重复数据
    assert meta.total_size == 1200


def test_sparse_file_allocated_size(make_tree, tmp_path, snapshot_map, monkeypatch):
    """测试稀疏文件（如虚拟机镜像）按真实磁盘物理分配块计算，避免名义大小虚高。"""
    import os
    import core.scanner as scanner

    root = make_tree({"sparse.img": 1000000})

    orig_stat = os.DirEntry.stat

    class FakeStat:
        def __init__(self, real):
            self._real = real
            self.st_size = 1000000  # 名义逻辑大小 1,000,000 字节
            self.st_blocks = 4       # 实际物理分配 4 块 * 512 = 2048 字节
            self.st_mtime = real.st_mtime
            self.st_mode = real.st_mode
            self.st_reparse_tag = 0

    def mock_entry_stat(self, follow_symlinks=True):
        st = orig_stat(self, follow_symlinks=follow_symlinks)
        if self.name == "sparse.img":
            return FakeStat(st)
        return st

    monkeypatch.setattr(os.DirEntry, "stat", mock_entry_stat)

    db, meta = _scan(root, tmp_path)
    entries = snapshot_map(db)

    # 实际应取 min(1000000, 4 * 512) = 2048
    assert entries["sparse.img"].size == 2048
    assert meta.total_size == 2048


def test_query_free_size_mac_native():
    """测试 query_free_size 在当前环境能正确返回正数剩余空间。"""
    from core.scanner import query_free_size
    free = query_free_size("/")
    assert free > 0
    # 在本 Mac 上容量应大于 10GB
    assert free > 10 * (1024 ** 3)





