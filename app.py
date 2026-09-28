"""執行檔的進入點：第一次執行時建好資料夾，找一個能用的 port，啟動後自動開瀏覽器。"""
import argparse
import json
import socket
import sys
import threading
import urllib.request
import webbrowser

# skill 是執行期才從資料夾載入的 .py，打包工具看不到它們用了哪些標準函式庫，所以在這裡先 import 一次
import base64, fnmatch, glob, html, html.parser, shutil, sqlite3, subprocess, tempfile  # noqa: F401,E401
import urllib.error, urllib.parse, xml.etree.ElementTree  # noqa: F401,E401

import engine
import server


def already_running(host, port):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/providers", timeout=2) as r:
            return isinstance(json.loads(r.read()), dict)
    except Exception:
        return False


def port_free(host, port):
    with socket.socket() as s:
        return s.connect_ex((host, port)) != 0


def main():
    ap = argparse.ArgumentParser(description="自動化工作流")
    ap.add_argument("--port", type=int, help="指定 port（預設用設定檔裡的，被佔用就往後找）")
    ap.add_argument("--no-browser", action="store_true", help="啟動後不要自動開瀏覽器")
    args = ap.parse_args()

    engine.seed_data_dir()
    host = engine.load_config()["server"]["host"]
    port = args.port or engine.load_config()["server"]["port"]
    if not args.port and already_running(host, port):
        print(f"已經在執行了：http://{host}:{port}")
        if not args.no_browser:
            webbrowser.open(f"http://{host}:{port}")
        return
    if not args.port:
        while not port_free(host, port):
            port += 1
    open_it = None if args.no_browser else (lambda url: threading.Timer(0.8, webbrowser.open, [url]).start())
    try:
        server.serve(port, on_ready=open_it)
    except KeyboardInterrupt:
        print("已停止")


if __name__ == "__main__":
    sys.exit(main())
