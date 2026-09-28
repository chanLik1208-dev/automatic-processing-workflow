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
- `dashboard.html` — 監控頁：即時動作與思考、執行紀錄、工作流管理
- `skills/*.py` — 工具型 skill，每個檔案一個 `SPEC` + `run()`
- `skills/<名稱>/SKILL.md` — 知識型 skill，agent 用 `use_skill` / `read_skill_file` 按需載入
- `workflows/*.json` — 工作流設定，可以在監控頁上新增、編輯、開關排程
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
