"""
共用套件的煙霧測試。

跑法（在套件根目錄）：
    pip install -e . httpx
    python tests/test_smoke.py

發新版 tag 之前先跑這個 —— 所有 API 都相依這個套件，
壞掉的話影響範圍是全部，不是一支。
"""

import os, shutil, sys, logging, tempfile

WORK = os.path.join(tempfile.mkdtemp(prefix="cda_api_common_test_"), "run")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK); os.chdir(WORK)

# 環境變數必須在 import 之前設好（模組載入當下就會讀）
os.environ["SERVICE_NAME"] = "smart_meter_api"
os.environ["API_KEYS"] = "rk_cat_f38ca939:api_catalog, rk_lib_11112222:圖書資訊處"
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"          # 先關閉，第 2 段再開

from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient
from cda_api_common import create_app, success_response, clean_rows
import cda_api_common.rate_limit as rl

router = APIRouter(tags=["test"])

@router.get("/ok")
def ok(limit: int = 10):
    return success_response({"limit": limit})

@router.get("/boom")
def boom():
    raise RuntimeError("故意炸開")

@router.get("/notfound")
def notfound():
    raise HTTPException(status_code=404, detail="查無此設備")

app = create_app(title="測試 API", root_path="/api/smart-meter", description="驗證用")
app.include_router(router, prefix="/v1")

c = TestClient(app, raise_server_exceptions=False)
fail = []
def check(label, cond, got=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   got={got}" if not cond else ""))
    if not cond: fail.append(label)

print("\n[1] 回應信封")
check("GET / 健康檢查", c.get("/").json() == {"status": "ok"})
check("成功信封", c.get("/v1/ok").json() == {"success": True, "message": "success", "data": {"limit": 10}})
r = c.get("/沒有這個路徑")
check("404 走共用信封", r.status_code == 404 and set(r.json()) == {"message", "data"} and r.json()["data"] is None, r.text)
r = c.get("/v1/notfound")
check("HTTPException 保留 detail", r.status_code == 404 and r.json()["message"] == "查無此設備", r.text)
r = c.get("/v1/ok?limit=abc")
check("422 帶參數名與原因", r.status_code == 422 and "參數驗證失敗" in r.json()["message"] and "limit" in r.json()["message"], r.text)

# 只關掉 app logger 去掉那串預期中的 traceback；
# 不能用 logging.disable()，那會把 access logger 一起關掉
_applog = logging.getLogger("cda_api_common.app")
_applog.disabled = True
r = c.get("/v1/boom")
_applog.disabled = False
check("500 不外洩內部訊息", r.status_code == 500 and r.json()["message"] == "Internal server error", r.text)
check("500 不含原始例外字串", "故意炸開" not in r.text, r.text)

print("\n[2] 限流")
rl.MAX_PER_MINUTE = 3; rl._hits.clear()
codes = [c.get("/v1/ok").status_code for _ in range(5)]
check(f"前 3 筆放行、之後 429（{codes}）", codes == [200, 200, 200, 429, 429], codes)
r = c.get("/v1/ok")
check("429 帶 Retry-After", r.headers.get("Retry-After") == "60", r.headers.get("Retry-After"))
check("429 信封一致", set(r.json()) == {"message", "data"}, r.text)
check("健康檢查不受限流", c.get("/").status_code == 200)
rl.MAX_PER_MINUTE = 0; rl._hits.clear()

print("\n[3] 呼叫端識別")
c.get("/v1/ok", headers={"X-API-Key": "rk_cat_f38ca939"})
c.get("/v1/ok", headers={"X-API-Key": "rk_lib_11112222"})
c.get("/v1/ok", headers={"X-API-Key": "rk_bad_99999999"})
c.get("/v1/ok")
log = open(os.path.join(WORK, "logs", "access.log"), encoding="utf-8").read()
check("對到 key -> api_catalog", "[api_catalog]" in log)
check("對到 key -> 中文名稱（.env 有空格也能解析）", "[圖書資訊處]" in log)
check("未知 key -> unknown，且完整 key 不落地", "[unknown:rk_b...]" in log and "rk_bad_99999999" not in log)
check("沒帶 key -> -", "[-]" in log)
check("500 有留下存取紀錄", " 500 " in log)
check("429 有留下存取紀錄", " 429 " in log)
check("健康檢查不寫入 access log", " GET / " not in log)

print("\n[4] SERVICE_NAME 決定 log 檔名")
names = sorted(os.listdir(os.path.join(WORK, "logs")))
check("產生 smart_meter_api_*.log", any(n.startswith("smart_meter_api_") for n in names), names)
check("access.log 獨立於 app log", "access.log" in names, names)

print("\n[5] rows.clean_rows")
from decimal import Decimal
out = clean_rows([{"a": Decimal("1.5"), "b": float("nan"), "c": "x"}])
check("Decimal->float、NaN->None", out == [{"a": 1.5, "b": None, "c": "x"}], out)

print("\n[6] OpenAPI")
r = c.get("/openapi.json")
check("openapi.json 可產生", r.status_code == 200 and r.json()["info"]["title"] == "測試 API")
check("健康檢查出現在 schema（與各 API 原本 main.py 一致）", "/" in r.json()["paths"], list(r.json()["paths"]))

print("\n[7] .env 從工作目錄載入")
# 這一段必須用子行程跑真正的 .py 檔，不能用 python -c：
# find_dotenv() 在互動模式下會改用 CWD，用 -c 測會假性通過。
#
# 迴歸對象：load_dotenv() 若少了 usecwd=True，會從套件自己的安裝位置往上找
# .env，於是永遠找不到 API 的 .env —— 而且不會報錯，DB_HOST、API_KEYS 全部
# 變成 None，服務照常啟動但連不上資料庫。
import subprocess, textwrap, json as _json

envdir = tempfile.mkdtemp(prefix="cda_dotenv_test_")
with open(os.path.join(envdir, ".env"), "w", encoding="utf-8") as f:
    f.write("SERVICE_NAME=from_dotenv\nAPI_KEYS=k_probe:探針單位\nDB_HOST=1.2.3.4\n")
with open(os.path.join(envdir, "probe.py"), "w", encoding="utf-8") as f:
    f.write(textwrap.dedent("""
        import os, json
        import cda_api_common
        import cda_api_common.access_log as al
        print(json.dumps({
            "SERVICE_NAME": os.getenv("SERVICE_NAME"),
            "DB_HOST": os.getenv("DB_HOST"),
            "table": al.API_KEYS,
        }, ensure_ascii=False))
    """))

# 子行程要用乾淨的環境：load_dotenv() 不覆蓋已存在的環境變數（這是正確行為，
# docker-compose 傳進來的值本來就該勝過 .env），若直接繼承，讀到的會是本測試
# 在最上面設的值，測不到 .env 有沒有被載入。
child_env = {k: v for k, v in os.environ.items()
             if k not in ("SERVICE_NAME", "API_KEYS", "DB_HOST", "RATE_LIMIT_PER_MINUTE")}

out = subprocess.run([sys.executable, "probe.py"], cwd=envdir, env=child_env,
                     capture_output=True, text=True, encoding="utf-8")
got = _json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
check("SERVICE_NAME 從 .env 讀到", got.get("SERVICE_NAME") == "from_dotenv", got or out.stderr[-200:])
check("DB_HOST 從 .env 讀到", got.get("DB_HOST") == "1.2.3.4", got)
check("API_KEYS 從 .env 解析", got.get("table") == {"k_probe": "探針單位"}, got)


print("\n" + "=" * 50)
print("結果：" + ("全部通過" if not fail else f"失敗 {len(fail)} 項 -> " + ", ".join(fail)))
print("=" * 50)
print("\nlogs/ 檔案：", names)
print("\nlogs/access.log：")
print(log)
sys.exit(1 if fail else 0)
