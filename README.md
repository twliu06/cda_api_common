# cda_api_common

逢甲大學資料分析中心 開放資料 API 的共用基礎模組。

各 API（`sdg_api`、`smart_meter_api`、`research_api`……）原本各自帶一份逐字相同的
`utils/`，改一個地方要改 N 次，而且已經開始出現分歧。這個套件把那些程式碼收成單一來源。

## 安裝

在各 API 的 `requirements.txt` 寫：

```
cda-api-common @ https://github.com/twliu06/cda_api_common/archive/refs/tags/v1.1.0.tar.gz
```

**用 tarball 網址，不要用 `git+https://`。** `python:3.13-slim` 裡沒有 `git`，
從 `git+` 安裝會失敗（`ERROR: Cannot find command 'git'`）。要嘛在 Dockerfile 裡
`apt-get install git`，讓每支 API 的 image 都多約 50MB，要嘛用 tarball —— 後者
不需要 git，一樣釘在 tag 上。

**一定要釘版本，不要指向 main。** 指向 main 的話，這裡推一個有問題的 commit，
下次任何一支 API 重新 build 就會裝到，而且是無聲的。釘 tag 才有辦法分批升級、
先在一支上確認再推廣。

升級時（PowerShell）：

```powershell
Get-ChildItem -Directory | ForEach-Object {
  $f = Join-Path $_.FullName "requirements.txt"
  if (Test-Path $f) {
    (Get-Content $f) -replace "v1\.1\.0", "v1.2.0" | Set-Content $f
  }
}
```

本機開發可以改成可編輯安裝，改套件立刻生效：

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ..\cda_api_common
```

**本機請用 Python 3.13**，跟 Docker image 一致。用 3.10 會裝到完全不同版本的
pandas（3.0 需要 3.11 以上，3.10 只能裝到 2.x），本機測過的行為不代表正式環境
的行為。

## 使用

```python
from cda_api_common import create_app
from routers import meters, measurements

app = create_app(
    title="智慧電表 API",
    root_path="/api/smart-meter",
    description="本 API 提供智慧電表相關資料服務……",
)

app.include_router(meters.router, prefix="/v1")
app.include_router(measurements.router, prefix="/v1")
```

`create_app()` 已經接好，不需要在 main.py 重複：

- 限流（`rate_limit_middleware`）
- CORS
- 存取紀錄（`access_log_middleware`）
- 三個全域 exception handler（信封統一為 `{"message": ..., "data": null}`）
- `GET /` 健康檢查

**middleware 的註冊順序是這個套件存在的主要理由。** 順序錯了不會報錯，只會在
特定情況下產生很難追查的症狀 —— 例如限流被擋下的請求，若順序不對，瀏覽器只會
看到 CORS 錯誤，看不到真正的 429。

## 資料庫

`db` 刻意不從套件根匯入，因為它在載入時就會讀 `DB_*` 環境變數。需要時各自匯入：

```python
from cda_api_common.db import get_conn_direct
```

## 環境變數

## 相依版本

`pyproject.toml` 精確釘選了 fastapi、starlette、pydantic、uvicorn、pandas、numpy、
psycopg2-binary、python-dotenv、pytz。這組數字取自正式環境實際跑的版本
（`docker exec research_api pip freeze`），所以釘選本身沒有改變任何現有行為。

要升級某個套件時：改 `pyproject.toml` → 跑 `tests/test_smoke.py` → 發新 tag →
先把一支 API 指過去確認 → 再推廣到其他支。這是釘版本才有的能力。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `SERVICE_NAME` | `api` | log 檔名前綴，例如 `smart_meter_api_2026-09-08.log`。**各 API 唯一必須不同的值** |
| `DB_HOST` / `DB_PORT` / `DB_STG_NAME` / `DB_USER` / `DB_PASSWORD` | `DB_PORT` 為 `5432` | 資料庫連線 |
| `API_KEYS` | 無 | 呼叫端識別對照表，格式 `key:名稱,key:名稱`。**不做驗證**，只影響 access log 顯示 |
| `ACCESS_LOG_DIR` | `logs` | 存取紀錄目錄 |
| `ACCESS_LOG_RETENTION_DAYS` | `90` | 保留天數 |
| `ACCESS_LOG_EXCLUDE_PATHS` | `/` | 不記錄的路徑，逗號分隔 |
| `RATE_LIMIT_PER_MINUTE` | `120` | 每 IP 每分鐘上限，`0` 為關閉 |
| `RATE_LIMIT_EXCLUDE_PATHS` | `/` | 不限流的路徑，逗號分隔 |

### `.env` 的尋找方式

`.env` 是從**工作目錄**往上找的（`find_dotenv(usecwd=True)`），所以各 API 必須以
自己的目錄為工作目錄啟動 —— Dockerfile 的 `WORKDIR=/app` 已經滿足這點，本機開發
則要先 `cd` 進該 API 的資料夾再跑 uvicorn。

這與搬移前不同：舊的 `utils/db_stg.py` 是相對於**該檔案自己的位置**去找 `.env`，
不受工作目錄影響。搬進套件後不能沿用那個做法，否則會去 site-packages 底下找。

工作目錄不對時，`DB_HOST`、`API_KEYS` 等會全部變成 `None`，而服務照常啟動 ——
所以找不到 `.env` 時會在 stderr 印一行警告（`docker logs` 看得到），
避免這種失敗完全無聲。若你是用 docker-compose 的 `env_file` / `environment`
直接注入變數、本來就沒有 `.env`，忽略那行警告即可。

`load_dotenv()` 由 `cda_api_common/__init__.py` 在匯入任何子模組之前呼叫。
這點很重要：`access_log` 和 `rate_limit` 都在模組載入當下讀環境變數，晚一步就讀不到，
而且不會報錯，只會安靜套用預設值。原本各 API 把 `load_dotenv()` 放在 `utils/db_stg.py`，
使得 `API_KEYS` 能否生效取決於 `main.py` 的 import 順序 —— 這個套件修掉了這件事。

## 從舊的 utils/ 遷移

1. `requirements.txt` 移除 `fastapi`、`uvicorn`、`psycopg2-binary`、`python-dotenv`、
   `pandas`、`pytz`，改成上面那一行 `cda-api-common @ git+...`
2. 刪掉整個 `utils/` 資料夾
3. `routers/` 裡的 `from utils.X import Y` 改成 `from cda_api_common.X import Y`
   （`utils.db_stg` 改為 `cda_api_common.db`）
4. `main.py` 改用 `create_app()`
5. `.env` 加一行 `SERVICE_NAME=<你的服務名>`
6. `docker compose up -d --build`

一次只改一支，跑通、對照 log 確認行為一致，再改下一支。
