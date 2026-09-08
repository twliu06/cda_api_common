import logging
import os
from typing import Optional

from cda_api_common.time_utils import now_taipei


class TaipeiFormatter(logging.Formatter):
    """使用台北時間格式化 Log 時間。"""

    def formatTime(self, record, datefmt=None):
        dt = now_taipei()

        if datefmt:
            return dt.strftime(datefmt)

        return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_logger(
    log_dir: str,
    log_prefix: Optional[str] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    建立共用 Logger。

    Parameters
    ----------
    log_dir : str
        Log 資料夾（不存在會自動建立）

    log_prefix : str | None
        Log 檔名前綴。
        若為 None，則使用資料夾名稱。

    level : int
        Logging Level，預設 INFO。

    Returns
    -------
    logging.Logger
    """

    os.makedirs(log_dir, exist_ok=True)

    if log_prefix is None:
        log_prefix = os.path.basename(os.path.normpath(log_dir))

    log_file = os.path.join(
        log_dir,
        f"{log_prefix}_{now_taipei().strftime('%Y-%m-%d')}.log"
    )

    logger_name = f"{log_dir}/{log_prefix}"
    logger = logging.getLogger(logger_name)

    # 避免重複建立 Handler
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    formatter = TaipeiFormatter(
        fmt="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # File
    file_handler = logging.FileHandler(
        log_file,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
