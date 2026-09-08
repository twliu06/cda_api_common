"""
逢甲大學資料分析中心 開放資料 API 的共用基礎模組。

各 API 的 main.py 只要：

    from cda_api_common import create_app

    app = create_app(title="...", root_path="/api/...")

限流、CORS、存取紀錄、全域錯誤處理都已經接好，順序也固定了。
"""

import os

from dotenv import find_dotenv, load_dotenv

# 必須在匯入下面任何模組「之前」執行。
#
# access_log 與 rate_limit 都是在模組載入當下就讀環境變數
# （API_KEYS、RATE_LIMIT_PER_MINUTE 等），晚一步就讀不到，
# 而且不會報錯，只會安靜地套用預設值。
#
# 這也修掉了原本各 API 的問題：load_dotenv() 放在 utils/db_stg.py，
# 使得 API_KEYS 能否生效取決於 main.py 的 import 順序。
#
# usecwd=True 不可省略。load_dotenv() 預設是從「呼叫它的那個檔案」所在目錄
# 往上找 .env —— 在這個套件裡，呼叫者就是本檔案，於是它會去 site-packages
# 底下找，永遠找不到 API 自己的 .env，而且不會報錯：DB_HOST、API_KEYS 全部
# 變成 None，服務照常啟動但連不上資料庫。
# 各 API 都以自己的目錄為工作目錄執行（Dockerfile 的 WORKDIR=/app），
# 所以要改成從工作目錄往上找。
_dotenv_path = find_dotenv(usecwd=True)

if _dotenv_path:
    load_dotenv(_dotenv_path)
else:
    # 找不到 .env 不一定是錯的 —— docker-compose 也可能用 env_file 或
    # environment 直接把變數塞進行程環境。但這同時是「所有設定都變成 None、
    # 服務照常啟動卻連不上資料庫」最常見的原因，而且完全無聲。
    # 留一行到 stderr（docker logs 看得到），讓它至少不是靜默失敗。
    import sys as _sys

    print(
        f"[cda_api_common] 警告：在工作目錄 {os.getcwd()} 及其上層找不到 .env，"
        f"環境變數將只來自行程環境。",
        file=_sys.stderr,
    )

from cda_api_common.app import create_app, register_exception_handlers  # noqa: E402
from cda_api_common.logging_config import setup_logging  # noqa: E402
from cda_api_common.logger import get_logger  # noqa: E402
from cda_api_common.response import success_response, error_response  # noqa: E402
from cda_api_common.rows import clean_rows, clean_value  # noqa: E402
from cda_api_common.time_utils import now_taipei, now_taipei_str  # noqa: E402

# 注意：cda_api_common.db 刻意不在這裡匯入。
# 它在載入時就會讀 DB_* 環境變數，若某支 API 不連資料庫就不該被迫設定它們。
# 需要時各自 from cda_api_common.db import get_conn_direct。

__version__ = "1.1.0"

__all__ = [
    "create_app",
    "register_exception_handlers",
    "setup_logging",
    "get_logger",
    "success_response",
    "error_response",
    "clean_rows",
    "clean_value",
    "now_taipei",
    "now_taipei_str",
]
