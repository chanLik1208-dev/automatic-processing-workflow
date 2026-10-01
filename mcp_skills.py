"""把 skills 包成 MCP 伺服器（stdio），給 ChatGPT 訂閱（codex）用。

為什麼：codex 是會自己跑工具的 agent，內建指示會跟 GPT 說「你只有內建的這些工具」。用文字格式叫它輸出工具 JSON，
GPT 常常回「這個環境沒有提供這個工具」而放棄（實測約兩到四成）。改成 MCP，GPT 用的是它原生的工具呼叫，就穩了。

codex 會用 `AutoWorkflow --mcp-skills` 把這個伺服器開起來（原始碼執行時是 python app.py --mcp-skills），
這次執行的資訊放在環境變數：
  AW_MCP_RUN    執行紀錄 id：每次工具呼叫都寫成一個步驟，監控頁照樣看得到過程
  AW_MCP_SKILLS 開放哪些 skill（逗號分隔）
  AW_MCP_MAX    最多呼叫幾次工具（對應工作流的 max_steps）
  AW_PERMS      這次執行允許的權限（逗號分隔，見 engine.PERMISSIONS）
stdout 只能輸出 JSON-RPC，其他訊息一律不能印。"""
import json
import os
import sys
import time


def serve():
    import engine
    run_id = int(os.environ["AW_MCP_RUN"])
    names = [n for n in os.environ.get("AW_MCP_SKILLS", "").split(",") if n]
    max_calls = int(os.environ.get("AW_MCP_MAX", "12"))
    skills = engine.load_skills()
    if "AW_PERMS" in os.environ:                                # 這次執行允許的權限（例如用使用者的瀏覽器）
        engine._ctx.perms = {k for k in os.environ["AW_PERMS"].split(",") if k}
    allowed = [n for n in names if n in skills]
    engine._ctx.tools = set(allowed)
    calls = fails = 0
    viewed = []
    out = sys.stdout
    sys.stdout = sys.stderr                                     # skill 裡的 print 不能混進 JSON-RPC

    def send(obj):
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()

    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        mid, meth, params = m.get("id"), m.get("method"), m.get("params") or {}
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "autoworkflow", "version": "1"}}})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": n, "description": skills[n].SPEC.get("description", ""),
                 "inputSchema": skills[n].SPEC.get("parameters") or {"type": "object", "properties": {}}}
                for n in allowed]}})
        elif meth == "tools/call":
            fn, args = params.get("name", ""), params.get("arguments") or {}
            calls += 1
            if calls > max_calls:
                text, failed = (f"[系統] 已經用了 {max_calls} 次工具，達到這個工作流的上限。"
                                "不要再呼叫工具，直接用目前拿到的資料給出最後結果。"), True
            else:
                t0 = time.time()
                text = engine.exec_tool(skills, allowed, fn, args)
                text, img = engine.take_image(fn, text, viewed)
                if img:
                    # codex 不會把 MCP 工具回傳的圖片交給 GPT（openai/codex#4819，只顯示 <image content>）；
                    # 它自己的 view_image 會（實測送出 input_image）。所以給路徑，請它用內建的 view_image 打開
                    viewed.append(img[0])
                    text = (f"圖片已下載到本機：{img[0]}\n（來源：{img[1]}）\n"
                            "請用你內建的 view_image 工具打開這個路徑來看圖片內容。")
                engine.add_step(run_id, engine.next_idx(run_id), "tool", fn, json.dumps(args, ensure_ascii=False),
                                text[:20000], int((time.time() - t0) * 1000))
                failed = engine.tool_failed(text)
                fails = fails + 1 if failed else 0
                if fails >= engine.cfg("limits.fail_streak", 3):
                    text += (f"\n\n[系統] 已經連續失敗 {fails} 次。不要再猜網址或路徑，"
                             "直接回報你缺什麼資訊、哪裡失敗，然後結束。")
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text[:40000]}],
                                                          "isError": failed}})
        elif meth == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:                                   # 不認得的要求（通知沒有 id，不用回）
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支援 {meth}"}})
