# automatic-processing-workflow

本機跑的 AI 自動化工作流：agent + skill + 排程 + 監控頁。模型走 OpenAI 相容 API，目前接 LM Studio（本機）和 DeepSeek。純 Python 標準庫，不用裝套件。

## 安裝（執行檔）

到 repo 的 **Releases** 下載自己平台的檔案，不需要先裝 Python：

| 平台 | 檔案 | 安裝 |
|---|---|---|
| macOS（Apple Silicon） | `AutoWorkflow-macos-arm64.dmg` | 打開 dmg，雙擊「安裝 AutoWorkflow.command」 |
| macOS（Intel） | `AutoWorkflow-macos-x64.dmg` | 同上 |
| Linux | `AutoWorkflow-linux-x64.tar.gz` | `tar xzf AutoWorkflow-linux-x64.tar.gz && ./AutoWorkflow-linux-x64/AutoWorkflow` |
| Windows | `AutoWorkflow-windows-x64.exe` | 直接執行 |

執行後會開 WebView 視窗（不開任何 port）。想用 Qt 原生介面加 `--qt`。執行檔沒有程式碼簽章，第一次開要多一步：

- **macOS**：打開 dmg 後雙擊「安裝 AutoWorkflow.command」，它會把 app 複製到「應用程式」並移除 macOS 對下載檔加的
  隔離標記，之後直接雙擊 app 就能開。這個安裝檔本身第一次會被擋一次：到「系統設定 → 隱私權與安全性」按「仍要開啟」。
  （也可以自己把 app 拖進「應用程式」後執行 `xattr -dr com.apple.quarantine /Applications/AutoWorkflow.app`。）
- **Windows**：SmartScreen 出現時按「其他資訊 → 仍要執行」

命令列：macOS 用 `/Applications/AutoWorkflow.app/Contents/MacOS/AutoWorkflow`，Linux / Windows 直接用那個執行檔，
後面接 `run`、`list`、`--headless` 等（`--help` 看全部）。

資料（設定、工作流、執行紀錄、報告）放在：macOS `~/Library/Application Support/AutoWorkflow`、
Windows `%APPDATA%\AutoWorkflow`、Linux `~/.local/share/autoworkflow`（可用環境變數 `AUTOWORKFLOW_HOME` 改位置）。
升級時換掉執行檔即可，資料不會動。

### 模型來源（至少要有一個）

- **LM Studio**（本機，免費）：開啟 LM Studio、載入一個模型、在 Developer 分頁啟動 server
- **Claude 訂閱**（Pro / Max，不需 API key）：安裝 Claude Code 並登入一次；沒裝時監控頁會顯示安裝指令
- **ChatGPT 訂閱**：安裝 Codex CLI 並登入一次；沒裝時監控頁會顯示安裝指令
- **Gemini 訂閱**（Google AI Pro / Ultra 或免費帳號）：安裝 Antigravity CLI（`agy`）並登入一次。Google 已在 2026-06-18 停止 Gemini CLI 的個人帳號登入，所以改用 `agy`
- **DeepSeek API**：設定環境變數 `DEEPSEEK_API_KEY`

自己編譯：`pip install pyinstaller && python build.py`（Python 3.13 以上；在哪個平台跑就產生哪個平台的版本）。
合併到 `main` 時，如果 `version.py` 的版本是新的，GitHub Actions 會自動在四個平台各自編譯、做啟動測試，全部通過才發佈到 Releases（不用手動打標籤；推 `v*` 標籤也照樣可以發佈）。

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

**用我的瀏覽器讀網頁**（設定頁，預設關閉）：一般讀法拿不到正文的網頁（要執行 JavaScript、要登入），改用程式專用的 Chrome / Edge 資料夾去讀；先按「打開登入視窗」登入需要的網站。只讀取頁面文字，不會點擊、輸入或付款，也不碰你平常的瀏覽器。部分網站（例如淘寶）的條款禁止自動化存取，建議一次不要讀太多頁。

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
