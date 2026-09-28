# automatic-processing-workflow

本機跑的 AI 自動化工作流：agent + skill + 排程 + 監控頁。模型走 OpenAI 相容 API，目前接 LM Studio（本機）和 DeepSeek。純 Python 標準庫，不用裝套件。

## 安裝（執行檔）

到 repo 的 **Releases** 下載自己平台的檔案，不需要先裝 Python：

| 平台 | 檔案 | 安裝 |
|---|---|---|
| macOS（Apple Silicon） | `AutoWorkflow-macos-arm64.dmg` | 打開 dmg，把 AutoWorkflow 拖進「應用程式」 |
| macOS（Intel） | `AutoWorkflow-macos-x64.dmg` | 同上 |
| Linux | `AutoWorkflow-linux-x64.tar.gz` | `tar xzf AutoWorkflow-linux-x64.tar.gz && ./AutoWorkflow-linux-x64/AutoWorkflow` |
| Windows | `AutoWorkflow-windows-x64.exe` | 直接執行 |

執行後會開原生視窗。執行檔沒有程式碼簽章，第一次開要多一步：

- **macOS**：第一次開被擋的話，到「系統設定 → 隱私權與安全性」按「仍要開啟」，或執行
  `xattr -dr com.apple.quarantine /Applications/AutoWorkflow.app`
- **Windows**：SmartScreen 出現時按「其他資訊 → 仍要執行」

命令列：macOS 用 `/Applications/AutoWorkflow.app/Contents/MacOS/AutoWorkflow`，Linux / Windows 直接用那個執行檔，
後面接 `run`、`list`、`--headless` 等（`--help` 看全部）。

資料（設定、工作流、執行紀錄、報告）放在：macOS `~/Library/Application Support/AutoWorkflow`、
Windows `%APPDATA%\AutoWorkflow`、Linux `~/.local/share/autoworkflow`（可用環境變數 `AUTOWORKFLOW_HOME` 改位置）。
升級時換掉執行檔即可，資料不會動。

### 模型來源（至少要有一個）

- **LM Studio**（本機，免費）：開啟 LM Studio、載入一個模型、在 Developer 分頁啟動 server
- **Claude 訂閱**（Pro / Max，不需 API key）：安裝 Claude Code 並登入一次；沒裝時監控頁會顯示安裝指令
- **ChatGPT / Gemini 訂閱**：監控頁會顯示 Codex CLI / Gemini CLI 的安裝方法（串接尚未完成）
- **DeepSeek API**：設定環境變數 `DEEPSEEK_API_KEY`

自己編譯：`pip install pyinstaller && python build.py`（Python 3.13 以上；在哪個平台跑就產生哪個平台的版本）。
推 `v*` 標籤時 GitHub Actions 會在四個平台各自編譯、做啟動測試，再發佈到 Releases。

## 從原始碼啟動

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
