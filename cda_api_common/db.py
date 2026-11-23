import logging
import psycopg2
import time
import warnings
warnings.simplefilter(action='ignore', category=UserWarning)
import os

# 這裡不呼叫 load_dotenv()：改由 cda_api_common/__init__.py 統一在最前面載入。
# 原本放在這個檔案會讓 access_log 能否讀到 API_KEYS 取決於 main.py 的 import 順序。

# =========================
# DB 設定（單一來源）
# =========================
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_STG_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}

# SQLAlchemy 連線字串（給 pandas / read_sql 用）
DB_URI = (
    f"postgresql+psycopg2://{DB_CONFIG['user']}:"
    f"{DB_CONFIG['password']}@"
    f"{DB_CONFIG['host']}:"
    f"{DB_CONFIG['port']}/"
    f"{DB_CONFIG['dbname']}"
)

# =========================
# 開連線的逾時與重試
# =========================
# 每個請求都新開一條連線。開連線的第一個封包（SYN）掉了，Linux 會在累計第
# 1、3、7、15、31 秒才重送，網路只要斷一兩秒，請求就會卡到第 7 秒甚至更久。
# 設定逾時後立刻重試，會馬上送出新的 SYN，不必乾等系統排定的下一次重送。
#
# libpq 的 connect_timeout 最小有效值是 2 秒；SYN 第一次重送在第 1 秒，
# 落在逾時之前，所以只掉一次封包時行為不變。
CONNECT_TIMEOUT_SECONDS = 2
CONNECT_ATTEMPTS = 3

# 開連線超過這個秒數就寫 warning，用來分辨慢請求是卡在連線（網路）還是查詢（DB）
SLOW_CONNECT_SECONDS = 1

logger = logging.getLogger(__name__)


def _is_timeout(error: psycopg2.OperationalError) -> bool:
    """只有逾時才重試；密碼錯誤、資料庫不存在這類錯誤重試也不會好"""
    return "timeout expired" in str(error)


def _connect():
    started = time.monotonic()

    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            conn = psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT_SECONDS)

        except psycopg2.OperationalError as error:
            if not _is_timeout(error) or attempt == CONNECT_ATTEMPTS:
                logger.warning(
                    "DB 開連線失敗：第 %d 次嘗試，累計 %.2fs：%s",
                    attempt, time.monotonic() - started, error,
                )
                raise

            logger.warning(
                "DB 開連線逾時，立刻重試：第 %d 次嘗試，累計 %.2fs",
                attempt, time.monotonic() - started,
            )
            continue

        elapsed = time.monotonic() - started

        if elapsed > SLOW_CONNECT_SECONDS:
            logger.warning("DB 開連線較慢：第 %d 次嘗試成功，累計 %.2fs", attempt, elapsed)

        return conn


def get_conn(autocommit: bool = True):
    conn = _connect()
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        conn.close()

def get_conn_direct(autocommit: bool = True):
    conn = _connect()
    conn.autocommit = autocommit
    return conn
