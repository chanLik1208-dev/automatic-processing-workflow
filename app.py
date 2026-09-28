"""進入點。

  AutoWorkflow                       開原生視窗（不開 port、不需要瀏覽器）
  AutoWorkflow --browser             改用瀏覽器開（會在本機開一個 Web 伺服器）
  AutoWorkflow --headless            沒有介面：只在背景跑排程，並開 Web 伺服器提供 API / 監控頁（伺服器、開機自動啟動用）
  AutoWorkflow --headless --no-web   連 API 都不開，只跑排程
  AutoWorkflow run <工作流> [--input 文字] [--json]
                                     在終端機跑一次，印出進度和結果；成功回傳 0、失敗回傳 1
  AutoWorkflow run --skill <skill> --input 文字
                                     用某個 skill 跑一次（同「試用」）
  AutoWorkflow list                  列出工作流和 skill
"""
import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

# skill 是執行期才從資料夾載入的 .py，打包工具看不到它們用了哪些標準函式庫，所以在這裡先 import 一次
import base64, fnmatch, glob, html, html.parser, shutil, sqlite3, subprocess, tempfile  # noqa: F401,E401
import urllib.error, urllib.parse, xml.etree.ElementTree  # noqa: F401,E401

import engine
import server
import skill_admin

# Windows 主控台預設不是 UTF-8（例如 cp1252），印中文會直接當掉；統一改成 UTF-8，印不出來的字元用替代符號
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def already_running(host, port):
    """那個 port 上是不是「用同一個資料夾」的本程式。別的資料夾的、或別的程式都不算。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/instance", timeout=2) as r:
            return json.loads(r.read()).get("data_dir") == str(engine.ROOT.resolve())
    except Exception:
        return False


def port_free(host, port):
    with socket.socket() as s:
        return s.connect_ex((host, port)) != 0


def cmd_native(args):
    try:
        import native
    except Exception as e:                       # 沒有可用的 WebView（例如 Linux 沒裝 GTK/Qt 的 WebKit）
        print(f"這台電腦開不了原生視窗（{e}），改用瀏覽器開啟。")
        return cmd_serve(args)
    native.run()
    return 0


def cmd_serve(args):
    host = engine.cfg("server.host", "127.0.0.1")
    port = args.port or engine.cfg("server.port", 8787)
    if not args.port and not args.no_web and already_running(host, port):
        print(f"已經在執行了：http://{host}:{port}")
        if not args.headless:
            webbrowser.open(f"http://{host}:{port}")
        return 0
    if args.headless and args.no_web:
        engine.init_db()
        print(f"headless：只跑排程（每 {engine.cfg('tick_seconds', 30)} 秒檢查一次），資料夾：{engine.ROOT}。Ctrl+C 結束")
        try:
            engine.scheduler_loop()
        except KeyboardInterrupt:
            print("已停止")
        return 0
    if not args.port:
        while not port_free(host, port):
            port += 1
    open_it = None if args.headless else (lambda url: threading.Timer(0.8, webbrowser.open, [url]).start())
    if args.headless:
        print("headless：不開瀏覽器，排程照常執行。")
    try:
        server.serve(port, on_ready=open_it)
    except KeyboardInterrupt:
        print("已停止")
    return 0


def _progress(run_id, quiet):
    """邊跑邊把新的步驟印出來。"""
    seen, last_phase = 0, None
    while True:
        with engine.db() as c:
            run = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            steps = c.execute("SELECT kind, name, input, output, ms FROM steps WHERE run_id=? ORDER BY idx, id", (run_id,)).fetchall()
        if not quiet:
            for s in steps[seen:]:
                if s["kind"] == "tool":
                    out = (s["output"] or "").strip().splitlines()
                    print(f"  ✓ {s['name']} {(s['input'] or '')[:80]}  →  {(out[0] if out else '')[:80]}", file=sys.stderr)
                elif s["kind"] == "error":
                    print(f"  ✕ {(s['output'] or '').splitlines()[0][:120]}", file=sys.stderr)
            live = next((v for v in engine.LIVE.values() if v["run_id"] == run_id), None)
            ph = live and (live.get("phase"), live.get("round"))
            if ph and ph != last_phase:
                print(f"  … 第 {ph[1]} 輪：{ {'waiting': '讀資料', 'thinking': '思考', 'writing': '寫結果', 'deciding': '決定下一步', 'tool': '用工具'}.get(ph[0], ph[0]) }", file=sys.stderr)
                last_phase = ph
        seen = len(steps)
        if run and run["status"] != "running":
            return
        time.sleep(0.5)


def cmd_run(args):
    engine.init_db()
    if args.skill:
        wf = skill_admin.adhoc_workflow(args.skill, args.provider or "claude", args.model or "")
        name = f"skill:{args.skill}"
    else:
        wfs = engine.load_workflows()
        if args.workflow not in wfs:
            print(f"找不到工作流 {args.workflow}。可用的有：{', '.join(wfs)}", file=sys.stderr)
            return 2
        name, wf = args.workflow, dict(wfs[args.workflow])
        if args.provider:
            wf["provider"] = args.provider
        if args.model:
            wf["model"] = args.model
    result = {}
    t = threading.Thread(target=lambda: result.update(id=engine.run_workflow(name, "cli", args.input or "", wf)), daemon=True)
    t.start()
    while "id" not in result and not engine.LIVE:
        time.sleep(0.1)
    run_id = result.get("id") or next(iter(engine.LIVE.values()))["run_id"]
    try:
        _progress(run_id, args.json or args.quiet)
    except KeyboardInterrupt:
        engine.cancel(name)
        print("\n已要求停止…", file=sys.stderr)
    t.join()
    with engine.db() as c:
        r = dict(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    if args.json:
        print(json.dumps({k: r[k] for k in ("id", "workflow", "status", "output", "error", "provider", "model",
                                           "tokens_in", "tokens_out", "started", "finished")}, ensure_ascii=False, indent=2))
    elif r["status"] == "success":
        print(r["output"])
    else:
        print(f"沒有完成（{r['status']}）：{r['error']}", file=sys.stderr)
    return 0 if r["status"] == "success" else 1


def cmd_list(args):
    engine.seed_data_dir()
    print("工作流：")
    for n, w in engine.load_workflows().items():
        s = w.get("schedule") or {}
        when = f"每天 {s['daily']}" if s.get("daily") else f"每 {s['every_minutes']} 分鐘" if s.get("every_minutes") else "手動"
        print(f"  {n:22s} {w.get('title', n)}（{when}{'' if w.get('enabled', True) else '，排程已停用'}）")
    print("skill：")
    for s in skill_admin.list_skills():
        print(f"  {s['name']:22s} {'知識' if s['kind'] == 'knowledge' else '工具'}  {s['description'][:60]}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="AutoWorkflow", description="自動化工作流",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--port", type=int, help="指定 port（預設用設定裡的，被佔用就往後找）")
    ap.add_argument("--headless", action="store_true", help="沒有介面，只在背景跑排程和 API")
    ap.add_argument("--browser", action="store_true", help="用瀏覽器開，而不是原生視窗")
    ap.add_argument("--no-web", action="store_true", help="搭配 --headless：連 API / 監控頁都不開，只跑排程")
    ap.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)   # 舊參數，等同 --headless
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="在終端機跑一次工作流或 skill")
    r.add_argument("workflow", nargs="?")
    r.add_argument("--skill", help="改用這個 skill 跑（同監控頁的「試用」）")
    r.add_argument("--input", "-i", help="額外輸入（手動執行時的那段文字）")
    r.add_argument("--provider", help="這次改用哪個模型來源（例如 claude、lmstudio）")
    r.add_argument("--model", help="這次改用哪個模型")
    r.add_argument("--json", action="store_true", help="結果用 JSON 輸出（方便接其他程式）")
    r.add_argument("--quiet", "-q", action="store_true", help="不印進度，只印結果")
    sub.add_parser("list", help="列出工作流和 skill")
    args = ap.parse_args()
    args.headless = args.headless or args.no_browser
    engine.seed_data_dir()
    if args.cmd == "run":
        if not args.workflow and not args.skill:
            ap.error("run 需要工作流名稱，或 --skill")
        return cmd_run(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.headless or args.browser:
        return cmd_serve(args)
    return cmd_native(args)


if __name__ == "__main__":
    sys.exit(main())
