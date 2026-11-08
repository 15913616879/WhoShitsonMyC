"""macOS 打包脚本 —— 产出 .app 与 .dmg：

  dist/WhoShitsOnMyC.app
  dist/WhoShitsOnMyC-v{version}-mac-arm64.dmg
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "WhoShitsOnMyC"

try:
    from version import __version__ as APP_VERSION
except ImportError:
    print("Lost of version info")
    sys.exit(1)

DIST_DIR = os.path.join(ROOT, "dist")

_EXCLUDES = [
    "dev",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.x509",
    "bcrypt",
    "gevent",
    "geventwebsocket",
    "greenlet",
    "tkinter",
    "_tkinter",
    "turtle",
    "setuptools",
    "pkg_resources",
    "wheel",
    "pip",
    "test",
    "unittest",
    "pydoc",
    "pydoc_data",
    "doctest",
    "pdb",
    "lib2to3",
    "xmlrpc",
    # Windows/Linux 平台后端排除
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.edgehtml",
    "webview.platforms.mshtml",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.android",
    "webview.platforms.cef",
]

_AI_EXCLUDES = [
    "modules.ai",
    "modules.ai.config",
    "modules.ai.client",
    "modules.ai.prompts",
    "modules.ai.service",
    "modules.ai.service_pkg",
    "modules.ai.packing",
    "modules.ai.tools",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "sniffio",
    "certifi",
]


def _prepare_web_datas(*, with_ai: bool) -> tuple[str, list[str]]:
    web_src = os.path.join(ROOT, "web")
    if with_ai:
        return web_src, []

    import tempfile

    tmp = tempfile.mkdtemp(prefix="wsmc-web-main-")
    for root, dirs, files in os.walk(web_src):
        rel = os.path.relpath(root, web_src)
        rel_posix = os.path.normpath(rel).replace("\\", "/")
        if rel_posix == "js/ai-vendor" or rel_posix.startswith("js/ai-vendor/"):
            dirs[:] = []
            continue
        if "ai-vendor" in dirs and os.path.basename(root) == "js":
            dirs.remove("ai-vendor")
        dest_dir = tmp if rel == "." else os.path.join(tmp, rel)
        os.makedirs(dest_dir, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(dest_dir, name))
    return tmp, [tmp]


def _cleanup_intermediates() -> None:
    build_dir = os.path.join(ROOT, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
        print("已删除 build/")
    for name in (f"{APP_NAME}.spec", f"{APP_NAME}.spec.bak"):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                print(f"已删除 {name}")
            except OSError:
                pass


def build_mac_app(*, with_ai: bool = False) -> str:
    print(f"==> 开始构建 macOS .app (v{APP_VERSION})...")
    excludes = list(_EXCLUDES)
    if not with_ai:
        excludes.extend(_AI_EXCLUDES)

    web_src, tmp_dirs = _prepare_web_datas(with_ai=with_ai)

    icon_path = os.path.join(ROOT, "logo.icns")
    icon_arg = ["--icon", icon_path] if os.path.isfile(icon_path) else []

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        APP_NAME,
        "--add-data",
        f"{web_src}:web",
        "--add-data",
        f"{icon_path}:." if os.path.isfile(icon_path) else f"{os.path.join(ROOT, 'logo.png')}:.",
        "--osx-bundle-identifier",
        "io.github.kami958.whoshitsonmyc",
        *icon_arg,
    ]
    for mod in excludes:
        cmd.extend(("--exclude-module", mod))

    cmd.append("app.py")

    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    _cleanup_intermediates()

    app_path = os.path.join(DIST_DIR, f"{APP_NAME}.app")
    if not os.path.isdir(app_path):
        sys.exit(f"打包失败，未生成 {app_path}")

    # ad-hoc 签名
    print("==> 正在进行 ad-hoc 本地代码签名...")
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app_path], check=False)
    print(f"==> .app 构建成功: {app_path}")
    return app_path


def build_dmg(app_path: str) -> str:
    dmg_name = f"{APP_NAME}-v{APP_VERSION}-mac-arm64.dmg"
    dmg_path = os.path.join(DIST_DIR, dmg_name)

    if os.path.exists(dmg_path):
        os.remove(dmg_path)

    print(f"==> 正在制作 DMG 安装镜像: {dmg_name}...")
    temp_dmg_dir = os.path.join(DIST_DIR, "dmg_root")
    shutil.rmtree(temp_dmg_dir, ignore_errors=True)
    os.makedirs(temp_dmg_dir, exist_ok=True)

    # 复制 .app 到临时镜像目录
    target_app = os.path.join(temp_dmg_dir, f"{APP_NAME}.app")
    shutil.copytree(app_path, target_app, symlinks=True)

    # 创建 /Applications 快捷方式软链接
    os.symlink("/Applications", os.path.join(temp_dmg_dir, "Applications"))

    # 使用 hdiutil 制作 UDZO 压缩镜像
    cmd = [
        "hdiutil",
        "create",
        "-volname",
        APP_NAME,
        "-srcfolder",
        temp_dmg_dir,
        "-ov",
        "-format",
        "UDZO",
        dmg_path,
    ]
    subprocess.run(cmd, check=True)
    shutil.rmtree(temp_dmg_dir, ignore_errors=True)
    print(f"==> DMG 制作完成: {dmg_path}")
    return dmg_path


def main():
    parser = argparse.ArgumentParser(description="Build WhoShitsOnMyC for macOS")
    parser.add_argument("--with-ai", action="store_true", help="Include AI modules")
    args = parser.parse_args()

    app_path = build_mac_app(with_ai=args.with_ai)
    dmg_path = build_dmg(app_path)
    print("\n🎉 构建交付物已就绪：")
    print(f"  App: {app_path}")
    print(f"  DMG: {dmg_path}")


if __name__ == "__main__":
    main()
