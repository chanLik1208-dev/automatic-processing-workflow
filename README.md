# automatic-processing-workflow

本機跑的 AI 自動化工作流：agent + skill + 排程 + 監控頁。模型走 OpenAI 相容 API，目前接 LM Studio（本機）和 DeepSeek。純 Python 標準庫，不用裝套件。

## 啟動

```sh
export DEEPSEEK_API_KEY=sk-...   # 沒有也能跑，只用本機模型
python3 server.py                # 監控頁 http://127.0.0.1:8787
```

## 結構

- `engine.py` — agent 迴圈（串流、備援切換）、排程、SQLite 紀錄
- `server.py` — 監控頁與 API
- `dashboard.html` — 監控頁：即時動作與時間分佈、按輪分組的過程、工作流管理（新增／編輯／開關排程／停止）、
  多格式匯出（Word / PDF / Markdown / HTML / 純文字 / JSON）；動態依 Dynamization 的 token 與配方實作
- `skills/*.py` — 工具型 skill，每個檔案一個 `SPEC` + `run()`
- `skills/<名稱>/SKILL.md` — 知識型 skill，agent 用 `use_skill` / `read_skill_file` 按需載入
- `workflows/*.json` — 工作流設定，可以在監控頁上新增、編輯、開關排程

內建工具型 skill：`web_search`（DuckDuckGo，不需 key）、`fetch_url`（只取正文）、`github_repo`、`read_rss`、
`http_check`、`system_status`、`tail_file`、`save_report`、`notify`、`create_workflow`、`use_skill` / `read_skill_file`。

## 本機模型（LM Studio）的防護

- 每輪有 `max_tokens` 上限（預設 4096，工作流可調），避免模型鬼打牆寫滿 context
- 「停止」會直接切斷串流
- 監看 LM Studio log：模型數值崩潰（NaN token 被 LM Studio 丟掉、沒有任何輸出）時自動中止並卸載模型
- 空回應、串流中途斷線都判定為失敗，不會把半截內容當成功
- 背景錄下 `lms log stream` 的原始輸出，每輪可對照「模型寫了什麼」與「實際收到什麼」
- `config.json` — 模型來源、`readable_paths`（`tail_file` 的讀檔白名單）

## Dynamization skill

`skills/dynamization` 是指向本機 repo 的 symlink，沒有進版控：

```sh
git clone https://github.com/chanLik1208-dev/Dynamization ~/Dynamization-src
ln -s ~/Dynamization-src skills/dynamization
```

## 安全

- 伺服器只綁 `127.0.0.1`；POST 必須是 JSON 且同源，擋掉網頁發出的跨站請求
- `fetch_url` / `http_check` 只接受 http(s)
- `tail_file` 只能讀 `config.json` 裡 `readable_paths` 白名單內的檔案
- 刪除工作流是移到 `workflows/.trash/`，不會真的刪掉
