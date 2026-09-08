"""
以來源 IP 為單位的滑動視窗限流。

目的是保護資料庫連線，而不是防禦大流量攻擊。
每支 router 的 handler 都是同步的 def，FastAPI 會丟進預設 40 條的執行緒池，
而 cda_api_common/db.py 每次請求都開一條新的 psycopg2 連線、沒有連線池。
平台上三支服務同時被打滿，就可能吃光 PostgreSQL 的 max_connections（預設 100），
連帶影響其他也在用同一個資料庫的服務。

被擋下來的請求在這一層就回應，不會進到 handler、不會開資料庫連線。

計數存在記憶體。Dockerfile 的 CMD 沒有 --workers，是單一 process，
因此夠用；若之後改成多 worker，額度會變成每個 worker 各一份。
"""

import os
import time
from collections import defaultdict, deque

from fastapi.responses import JSONResponse

from cda_api_common.access_log import get_client_ip
from cda_api_common.response import error_response

# =========================
# 設定（皆可用環境變數覆寫）
# =========================

# 每個 IP 每分鐘允許的請求數，設為 0 表示關閉限流。
# 預設值對官網正常瀏覽（一頁同時打好幾支端點）很寬鬆，
# 但迴圈式的連續呼叫幾秒內就會撞到。
MAX_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))

# 不限流的路徑，逗號分隔。預設排除健康檢查，避免監控被擋。
EXCLUDE_PATHS = {
    p.strip()
    for p in os.getenv("RATE_LIMIT_EXCLUDE_PATHS", "/").split(",")
    if p.strip()
}

WINDOW_SECONDS = 60

# IP -> 該 IP 在視窗內的請求時間點
_hits: dict[str, deque] = defaultdict(deque)

# 沒有背景工作可以清掉不再出現的 IP，
# 改成每處理一定次數的請求就順手掃一次，避免長期執行累積大量空的 deque。
_CLEANUP_EVERY = 1000
_since_cleanup = 0


def _cleanup(now: float) -> None:
    stale = [
        ip
        for ip, hits in _hits.items()
        if not hits or now - hits[-1] > WINDOW_SECONDS
    ]

    for ip in stale:
        del _hits[ip]


def _allow(ip: str, now: float) -> bool:
    hits = _hits[ip]
    cutoff = now - WINDOW_SECONDS

    # 丟掉已經滑出視窗的紀錄
    while hits and hits[0] <= cutoff:
        hits.popleft()

    if len(hits) >= MAX_PER_MINUTE:
        # 被擋下時不計入，否則持續重試的呼叫端會一直把視窗往後推、永遠解不開
        return False

    hits.append(now)

    return True


async def rate_limit_middleware(request, call_next):
    """
    超過額度時直接回 429，信封與其他回應一致：{"message": ..., "data": null}。

    註冊順序由 cda_api_common.app.create_app() 決定：這一層要在 access_log 的
    內側（429 才會留下存取紀錄），
    並且在 CORS 的內側（429 才會帶上 CORS header，否則瀏覽器只會看到
    CORS 錯誤，看不到真正的狀態碼）。
    """

    if MAX_PER_MINUTE <= 0 or request.url.path in EXCLUDE_PATHS:
        return await call_next(request)

    global _since_cleanup

    # 用 monotonic 而非 wall clock，避免系統時間被調整時算錯
    now = time.monotonic()

    _since_cleanup += 1

    if _since_cleanup >= _CLEANUP_EVERY:
        _since_cleanup = 0
        _cleanup(now)

    if not _allow(get_client_ip(request), now):
        return JSONResponse(
            status_code=429,
            content=error_response("請求過於頻繁，請稍後再試"),
            # 給自動化的呼叫端一個明確的等待秒數，瀏覽器不會用到
            headers={"Retry-After": str(WINDOW_SECONDS)},
        )

    return await call_next(request)
