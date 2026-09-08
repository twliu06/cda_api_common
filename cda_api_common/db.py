import psycopg2
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

def get_conn(autocommit: bool = True):
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        conn.close()

def get_conn_direct(autocommit: bool = True):
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = autocommit
    return conn
