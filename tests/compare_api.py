"""
把一支 API 所有 GET 端點的回應與 openapi.json 全文抓下來，存成 JSON。

用途是「搬到共用模組之前 / 之後」的逐字比對 —— 端點會不會通只是最低標準，
真正要確認的是 openapi.json 一個字都沒變（呼叫端和 api_catalog 都吃這份），
以及每個端點的回應形狀完全一致。

用法：
    python tests/compare_api.py <API 目錄> <輸出檔.json>
    python tests/compare_api.py <before.json> <after.json> --diff

範例（research_api，用 mock CSV 不連資料庫）：
    cd cda_api_common
    set USE_MOCK_DATA=true
    python tests/compare_api.py ..\research_api before.json
    python tests/compare_api.py before.json after.json --diff
"""

import json
import os
import sys


def capture(api_dir: str, out_path: str) -> None:
    api_dir = os.path.abspath(api_dir)

    # 必須以「該 API 自己的目錄」為工作目錄，跟正式環境一致
    # （Dockerfile 的 WORKDIR=/app，uvicorn 也是從 /app 啟動）。
    # cda_api_common 從工作目錄往上找 .env，跑錯目錄會讓 DB_HOST 等全部變 None。
    os.chdir(api_dir)
    sys.path.insert(0, api_dir)

    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app, raise_server_exceptions=False)

    spec = client.get("/openapi.json").json()

    # 跳過帶路徑參數的端點，那些沒有通用的預設值可填
    paths = sorted(
        p for p, item in spec["paths"].items()
        if "get" in item and "{" not in p
    )

    result = {"openapi_paths": paths, "openapi": spec, "responses": {}}

    for path in paths:
        resp = client.get(path)
        body = resp.json()

        # 資料列只留前兩筆，另外記總筆數：
        # 檔案不會爆掉，但形狀或筆數變了仍抓得到
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            body = {**body, "data": body["data"][:2], "_data_len": len(body["data"])}

        result["responses"][path] = {"status": resp.status_code, "body": body}

    # 錯誤路徑：這幾條走的是全域 exception handler，最容易在搬移時漏掉
    resp = client.get("/這個路徑不存在")
    result["responses"]["__404__"] = {"status": resp.status_code, "body": resp.json()}

    if paths:
        resp = client.post(paths[-1])
        result["responses"]["__405__"] = {"status": resp.status_code, "body": resp.json()}

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"端點數：{len(paths)}")

    for path in paths:
        print(f"  {result['responses'][path]['status']}  {path}")

    print()
    print(f"已寫入 {os.path.abspath(out_path)}")


def diff(before_path: str, after_path: str) -> int:
    with open(before_path, encoding="utf-8") as f:
        before = json.load(f)

    with open(after_path, encoding="utf-8") as f:
        after = json.load(f)

    print("端點清單相同          :", before["openapi_paths"] == after["openapi_paths"])
    print("openapi.json 逐字相同  :", before["openapi"] == after["openapi"])
    print("所有端點回應逐字相同   :", before["responses"] == after["responses"])
    print()

    if before == after:
        print(">>> 完全相同")
        return 0

    def walk(x, y, path=""):
        if type(x) is not type(y):
            print(f"{path}: 型別不同 {type(x).__name__} -> {type(y).__name__}")
            return

        if isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                if k not in x:
                    print(f"{path}.{k}: 只有 after 有")
                elif k not in y:
                    print(f"{path}.{k}: 只有 before 有")
                else:
                    walk(x[k], y[k], f"{path}.{k}")

        elif isinstance(x, list):
            if len(x) != len(y):
                print(f"{path}: 長度 {len(x)} -> {len(y)}")
            else:
                for i, (u, v) in enumerate(zip(x, y)):
                    walk(u, v, f"{path}[{i}]")

        elif x != y:
            print(f"{path}:")
            print(f"   before = {x!r}")
            print(f"   after  = {y!r}")

    walk(before, after)
    return 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[3] == "--diff":
        sys.exit(diff(sys.argv[1], sys.argv[2]))

    if len(sys.argv) == 3:
        capture(sys.argv[1], os.path.abspath(sys.argv[2]))
        sys.exit(0)

    print(__doc__)
    sys.exit(2)
