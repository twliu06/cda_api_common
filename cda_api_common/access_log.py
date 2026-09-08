import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler

from cda_api_common.time_utils import now_taipei

# =========================
# 設定（皆可用環境變數覆寫）
# =========================
LOG_DIR = os.getenv("ACCESS_LOG_DIR", "logs")

# 保留幾天份的存取紀錄
RETENTION_DAYS = int(os.getenv("ACCESS_LOG_RETENTION_DAYS", "90"))

# 不記錄的路徑，逗號分隔。
# 預設排除健康檢查，否則監控每分鐘打一次會把 log 灌爆。
EXCLUDE_PATHS = {
    p.strip()
    for p in os.getenv("ACCESS_LOG_EXCLUDE_PATHS", "/").split(",")
    if p.strip()
}

# 選填的呼叫端識別。
# 格式：API_KEYS=key1:名稱1,key2:名稱2
#
# 這裡「不做驗證」，只做對照：沒帶 key 或帶了未知的 key 都照常放行，
# 只是 log 中顯示為 "-" 或 "unknown"。
# 目的是在不影響任何現有呼叫端的前提下，讓願意配合的單位在 log 中留下名字。
def _load_api_keys() -> dict[str, str]:
    raw = os.getenv("API_KEYS", "").strip()

    if not raw:
        return {}

    keys = {}

    for entry in raw.split(","):
        entry = entry.strip()

        if not entry or ":" not in entry:
            continue

        key, name = entry.split(":", 1)
        key, name = key.strip(), name.strip()

        if key and name:
            keys[key] = name

    return keys


API_KEYS = _load_api_keys()


class TaipeiFormatter(logging.Formatter):
    """存取紀錄一律用台北時間，不受容器時區影響。"""

    def formatTime(self, record, datefmt=None):
        return now_taipei().strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def _build_logger() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger("access")

    # 重複載入模組時不要疊加 handler
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # 存取紀錄自成一個檔案，不要跟應用程式的錯誤 log 混在一起
    logger.propagate = False

    handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "access.log"),
        when="midnight",
        backupCount=RETENTION_DAYS,
        encoding="utf-8",
    )

    # 輪替後的檔名改成 access_2026-09-04.log，
    # 對齊 logs/ 既有的 <SERVICE_NAME>_YYYY-MM-DD.log 命名
    handler.namer = lambda name: os.path.join(
        LOG_DIR,
        f"access_{name.rsplit('.', 1)[-1]}.log",
    )

    handler.setFormatter(TaipeiFormatter("[%(asctime)s] %(message)s"))

    logger.addHandler(handler)

    return logger


access_logger = _build_logger()


def get_client_ip(request) -> str:
    """
    取得呼叫端 IP。

    服務跑在反向代理後面（見 create_app() 的 root_path），
    request.client.host 會是 proxy 的內網 IP，因此必須優先讀
    proxy 轉送過來的 header。

    X-Forwarded-For 可能是 "真實IP, proxy1, proxy2"，取最左邊那個。
    這個值是呼叫端可以偽造的，僅供流量觀察用，不能當作存取控制依據。
    """

    forwarded = request.headers.get("x-forwarded-for")

    if forwarded:
        return forwarded.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")

    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else "-"


def get_caller(request) -> str:
    """把 X-API-Key 對照成使用單位名稱；沒帶或對不到就標示出來。"""

    key = request.headers.get("x-api-key") or request.query_params.get("api_key")

    if not key:
        return "-"

    name = API_KEYS.get(key.strip())

    if name:
        return name

    # 未知的 key 不寫進 log，避免把憑證留在檔案裡，只留前 4 碼供比對
    return f"unknown:{key.strip()[:4]}..."


def _format_line(request, status_code: int, duration: float) -> str:
    path = request.url.path

    if request.url.query:
        path = f"{path}?{request.url.query}"

    return (
        f"{get_client_ip(request)} "
        f"[{get_caller(request)}] "
        f"{request.method} {path} "
        f"{status_code} "
        f"{duration:.3f}s "
        f'"{request.headers.get("user-agent", "-")}" '
        f'"{request.headers.get("referer", "-")}"'
    )


async def access_log_middleware(request, call_next):
    """
    記錄每一次請求。

    注意：未處理的例外會穿過這層 middleware 往外拋
    （全域 exception handler 位於更外層的 ServerErrorMiddleware），
    所以要自己攔下來記成 500 再往上拋，否則出錯的請求不會留下存取紀錄。
    """

    if request.url.path in EXCLUDE_PATHS:
        return await call_next(request)

    started = time.perf_counter()

    try:
        response = await call_next(request)

    except Exception:
        duration = time.perf_counter() - started
        access_logger.info(_format_line(request, 500, duration))

        raise

    duration = time.perf_counter() - started
    access_logger.info(_format_line(request, response.status_code, duration))

    return response
