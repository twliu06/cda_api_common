import logging
import os
from cda_api_common.time_utils import now_taipei


def setup_logging(log_dir="logs", service_name=None):
    """
    設定應用程式 log。

    service_name 決定 log 檔名前綴，例如 smart_meter_api_2026-09-08.log。
    未指定時讀環境變數 SERVICE_NAME —— 這是各 API 之間唯一需要不同的值，
    抽成變數之後這個檔案就能完全共用。
    """

    os.makedirs(log_dir, exist_ok=True)

    if service_name is None:
        service_name = os.getenv("SERVICE_NAME", "api")

    log_file = os.path.join(
        log_dir,
        f"{service_name}_{now_taipei().strftime('%Y-%m-%d')}.log"
    )

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 避免重複 handler
    if root_logger.handlers:
        return

    # Console
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    # File
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)
