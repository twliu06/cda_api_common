"""
建立一個已經接好共用設定的 FastAPI app。

這個模組存在的主要理由是 middleware 的註冊順序。順序錯了不會有任何錯誤訊息，
只會在特定情況下產生很難追查的症狀（例如瀏覽器看到 CORS 錯誤，但真正的原因
是 429），所以集中在 create_app() 裡定死，不要在各 API 的 main.py 重寫一次。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cda_api_common.access_log import access_log_middleware
from cda_api_common.logging_config import setup_logging
from cda_api_common.rate_limit import rate_limit_middleware
from cda_api_common.response import error_response

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """
    讓錯誤與成功使用相同的信封：{"message": ..., "data": null}

    需要註冊三個 handler，缺一就會漏掉某一類錯誤：
      StarletteHTTPException  404、405，以及程式中 raise 的 HTTPException
      RequestValidationError  422 參數驗證失敗
      Exception               其餘未預期的例外

    注意：不能只註冊 Exception 再用 isinstance 判斷 HTTPException，
    FastAPI 內建的 handler 優先權較高，那段判斷永遠不會執行。
    """

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response(str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # 保留是「哪個參數、錯在哪」，否則呼叫端無從得知
        details = "；".join(
            f"{'.'.join(str(x) for x in err.get('loc', []))}：{err.get('msg')}"
            for err in exc.errors()
        )

        return JSONResponse(
            status_code=422,
            content=error_response(f"參數驗證失敗（{details}）"),
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception(f"Unhandled error on {request.url}")

        return JSONResponse(
            status_code=500,
            content=error_response(),
        )


def create_app(
    *,
    title: str,
    root_path: str,
    version: str = "1.0.0",
    description: str = "",
    allow_origins: list[str] | None = None,
    service_name: str | None = None,
    include_health_check: bool = True,
) -> FastAPI:
    """
    建立 FastAPI app，並接上限流、CORS、存取紀錄與全域錯誤處理。

    Parameters
    ----------
    title : str
        顯示在 /docs 的 API 名稱。

    root_path : str
        反向代理掛載的路徑，例如 "/api/smart-meter"。
        沒設的話 /docs 產生的網址會少掉前綴。

    allow_origins : list[str] | None
        CORS 允許的來源，預設 ["*"]。

    service_name : str | None
        log 檔名前綴，未指定時讀環境變數 SERVICE_NAME。

    include_health_check : bool
        是否註冊 GET / 回 {"status": "ok"}。預設開啟；
        存取紀錄與限流預設都會排除這個路徑。
    """

    setup_logging(service_name=service_name)

    app = FastAPI(
        title=title,
        version=version,
        root_path=root_path,
        description=description,
    )

    # ---------- middleware ----------
    #
    # Starlette 的規則是「後註冊的在外層」，所以下面的順序等於由內到外：
    #   rate_limit -> CORS -> access_log
    #
    # 限流在最內側：被擋下的 429 才會帶上 CORS header（否則瀏覽器只會看到
    # CORS 錯誤，看不到真正的狀態碼），也才會留下存取紀錄。
    #
    # 存取紀錄在最外側：這樣才量得到含 CORS 處理在內的完整耗時，
    # 並且連被 CORS 擋掉的請求也會留下紀錄。
    app.middleware("http")(rate_limit_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins if allow_origins is not None else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.middleware("http")(access_log_middleware)

    if include_health_check:
        # 維持與各 API 原本 main.py 一致：這個路徑會出現在 openapi.json，
        # 且函式必須叫 health_check —— FastAPI 用函式名稱推導 operationId
        # 與 summary，改名字會讓 openapi.json 跟著變。
        #
        # 若日後想讓它從 /docs 消失，改成 @app.get("/", include_in_schema=False)，
        # 但那是獨立的一次調整，不要混在搬移共用模組的同一批改動裡。
        @app.get("/")
        def health_check():
            return {"status": "ok"}

    register_exception_handlers(app)

    return app
