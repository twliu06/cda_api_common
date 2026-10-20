"""
存取紀錄的 Teams 通知：每日異常檢查，週一另送上一週的週報。

由 cron 每天早上執行一次（見各 API 的 deploy/cron.d/）：

    docker exec <容器> python -m cda_api_common.access_report

手動測試，各送一則到 Teams（加 --dry-run 則只印出內容不送出）：

    docker exec <容器> python -m cda_api_common.access_report --test-alert    # 假資料的異常通知
    docker exec <容器> python -m cda_api_common.access_report --test-weekly   # 最近一個完整週的週報

也可以指定「今天」是哪一天，重跑那天的排程：

    docker exec <容器> python -m cda_api_common.access_report --date 2026-10-19 --dry-run

每日檢查前一天的紀錄，有下列情況才發，全部正常就不發：

    伺服器錯誤   狀態碼 5xx
    被限流       狀態碼 429
    慢請求       耗時超過 ACCESS_ALERT_SLOW_SECONDS（預設 1 秒）

週報不論結果都會發：週報同時是心跳，哪一週沒收到，就代表排程或容器有問題
（容器沒在跑、cron 沒執行）——這兩種情況每日檢查都抓不到，因為程式根本沒有跑。

通知裡不列 IP。每一項異常都附上可以直接貼到伺服器上執行的追查指令，
需要細節時回伺服器看 log。

只讀 log，不寫入任何檔案或資料庫。
"""

import argparse
import glob
import os
import re
import sys
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from cda_api_common.access_log import LOG_DIR
from cda_api_common.notify import Message, Section, send
from cda_api_common.rate_limit import MAX_PER_MINUTE
from cda_api_common.time_utils import now_taipei, now_taipei_str

# =========================
# 設定（皆可用環境變數覆寫）
# =========================
SERVICE_NAME = os.getenv("SERVICE_NAME", "api")

# 超過幾秒算慢請求
SLOW_SECONDS = float(os.getenv("ACCESS_ALERT_SLOW_SECONDS", "1"))

# 追查指令裡的專案目錄，是「主機上」的路徑。
# 程式跑在容器裡，看到的是 /app，但指令是要貼到主機上執行的。
HOST_DIR = os.getenv("ACCESS_REPORT_HOST_DIR", f"~/{SERVICE_NAME}")

# cron 把本程式的輸出導到這裡，放進除錯資訊，讓收到的人知道執行紀錄在哪
REPORT_LOG_PATH = f"/var/log/{SERVICE_NAME}/access_report.log"

# 平台內部的呼叫者，不列入週報的統計，只用一行帶過。
# api_catalog 每次有人開目錄頁就會抓一次各 API 的 /openapi.json，
# 算進去會讓「本週有人在用」的數字失真。
# 每日的異常檢查仍包含它們：內部呼叫遇到 5xx 一樣是問題。
INTERNAL_CALLERS = {"api_catalog"}

# 每條規則、週報的每份排行最多列幾項，其餘合併成一行
TOP_N = 5

# 對應 access_log._format_line() 的輸出：
# [2026-09-04 16:22:44] 140.134.246.215 [圖書資訊處] GET /v1/x?y=1 200 0.021s "UA" "Referer"
LINE_PATTERN = re.compile(
    r'^\[(?P<date>\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\] '
    r'\S+ \[(?P<caller>.*?)\] \S+ (?P<path>\S+) '
    r'(?P<status>\d{3}) (?P<duration>[\d.]+)s "(?P<ua>.*)" "(?P<referer>[^"]*)"$'
)


@dataclass
class Record:
    date: str
    caller: str
    # 去掉查詢參數的端點
    endpoint: str
    status: int
    duration: float
    ua: str
    referer: str


def load_records(start: date, end: date) -> tuple[list[Record], list[str], int]:
    """
    讀出 start 到 end（含）之間的紀錄。

    access.log 與所有 access_*.log 都要讀，再依每行的時間篩日期：輪替要等午夜後
    有新的請求寫入才會發生，某天半夜到早上都沒有請求時，前一天的紀錄還留在
    access.log 裡，只讀前一天的歷史檔會漏掉。
    """

    files = sorted(glob.glob(os.path.join(LOG_DIR, "access*.log")))
    first, last = start.isoformat(), end.isoformat()

    records = []
    unparsed = 0

    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # 先比對日期再跑正規表示式，90 天份的檔案大多數行都在範圍外
                day = line[1:11]

                if not first <= day <= last:
                    continue

                match = LINE_PATTERN.match(line.rstrip("\n"))

                if not match:
                    unparsed += 1
                    continue

                records.append(Record(
                    date=match["date"],
                    caller=match["caller"],
                    endpoint=match["path"].split("?", 1)[0],
                    status=int(match["status"]),
                    duration=float(match["duration"]),
                    ua=match["ua"],
                    referer=match["referer"],
                ))

    return records, [os.path.basename(path) for path in files], unparsed


def short_date(day: date) -> str:
    return f"{day.month}/{day.day}"


def top_facts(counter: Counter, label=lambda key: key, unit="個端點") -> list[tuple[str, str]]:
    """排行前 TOP_N 名，其餘合併成「其他 N 個端點」一行"""
    ranked = counter.most_common()
    facts = [(label(key), f"{count:,}") for key, count in ranked[:TOP_N]]
    rest = ranked[TOP_N:]

    if rest:
        facts.append((f"其他 {len(rest)} {unit}", f"{sum(count for _, count in rest):,}"))

    return facts


def endpoint_facts(rows: list[Record], describe) -> list[tuple[str, str]]:
    """依端點分組，筆數多的排前面；describe 把同一個端點的紀錄寫成右欄的說明"""
    groups = defaultdict(list)

    for row in rows:
        groups[row.endpoint].append(row)

    ranked = sorted(groups.items(), key=lambda item: -len(item[1]))
    facts = [(endpoint, describe(items)) for endpoint, items in ranked[:TOP_N]]
    rest = ranked[TOP_N:]

    if rest:
        facts.append((
            f"其他 {len(rest)} 個端點",
            f"{sum(len(items) for _, items in rest)} 筆",
        ))

    return facts


# =========================
# 每日異常檢查
# =========================

def find_alerts(rows: list[Record], day: date) -> list[Section]:
    """依規則檢查一天的紀錄，每條成立的規則一段；全部正常時回傳空清單"""
    sections = []

    # 先篩出當天的紀錄再交給 awk，access*.log 裡有 90 天份
    grep_day = f"grep -h '^\\[{day.isoformat()}' {HOST_DIR}/logs/access*.log"

    errors = [row for row in rows if row.status >= 500]

    if errors:
        sections.append(Section(
            heading="💥 伺服器錯誤（5xx）",
            text="程式出過例外，traceback 在應用程式 log。",
            facts=endpoint_facts(
                errors,
                lambda items: f"{len(items)} 筆（{'、'.join(sorted({str(r.status) for r in items}))}）",
            ),
            # 應用程式 log 的檔名是容器啟動那天的日期，之後不會換檔，
            # 所以不能拿出錯的日期當檔名找，改成搜尋所有檔案裡那一天的錯誤
            code=(
                f"# 出錯的請求\n"
                f"{grep_day} \\\n  | awk '$7 >= 500'\n"
                f"# traceback\n"
                f"grep -h -A30 '^\\[{day.isoformat()}.*Unhandled error' \\\n"
                f"  {HOST_DIR}/logs/{SERVICE_NAME}_*.log"
            ),
        ))

    limited = [row for row in rows if row.status == 429]

    if limited:
        sections.append(Section(
            heading="🚦 被限流（429）",
            text=f"有呼叫端在一分鐘內超過 {MAX_PER_MINUTE} 次，被擋下的請求沒有拿到資料。",
            facts=endpoint_facts(limited, lambda items: f"{len(items)} 筆"),
            code=f"{grep_day} \\\n  | awk '$7 == 429'",
        ))

    slow = [row for row in rows if row.duration > SLOW_SECONDS]

    if slow:
        sections.append(Section(
            heading=f"🐢 慢請求（超過 {SLOW_SECONDS:g} 秒）",
            facts=endpoint_facts(
                slow,
                lambda items: f"{len(items)} 筆，最慢 {max(r.duration for r in items):.2f}s",
            ),
            code=(
                f"{grep_day} \\\n"
                f"  | awk '{{t=$8; sub(/s$/,\"\",t); if (t+0 > {SLOW_SECONDS:g}) print}}'"
            ),
        ))

    return sections


def daily_message(records: list[Record], day: date, details: str) -> Message | None:
    rows = [row for row in records if row.date == day.isoformat()]
    sections = find_alerts(rows, day)

    print(f"[{now_taipei_str()}] {SERVICE_NAME} 每日檢查 {day}：{len(rows)} 筆，異常 {len(sections)} 項")

    if not sections:
        return None

    return Message(
        status="FAIL",
        title=f"{SERVICE_NAME} 存取紀錄有異常（{short_date(day)}）",
        sections=sections,
        details=details,
    )


# =========================
# 每週週報
# =========================

def caller_label(caller: str) -> str:
    return "[-]（未帶 key）" if caller == "-" else caller


def referer_label(referer: str) -> str:
    return "-（非網頁發出）" if referer == "-" else referer


def weekly_message(records: list[Record], start: date, end: date, details: str) -> Message:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    in_week = {day.isoformat() for day in days}
    prev_start, prev_end = (start - timedelta(days=7)).isoformat(), (start - timedelta(days=1)).isoformat()

    week = [row for row in records if row.date in in_week]
    internal = [row for row in week if row.caller in INTERNAL_CALLERS]
    external = [row for row in week if row.caller not in INTERNAL_CALLERS]
    previous = sum(
        1 for row in records
        if prev_start <= row.date <= prev_end and row.caller not in INTERNAL_CALLERS
    )

    sections = []

    if not external:
        sections.append(Section(text=f"本週沒有請求（上週 {previous:,} 筆）"))

    else:
        change = f"，{(len(external) - previous) / previous:+.0%}" if previous else ""

        by_endpoint = defaultdict(list)

        for row in external:
            by_endpoint[row.endpoint].append(row.duration)

        slowest, durations = max(by_endpoint.items(), key=lambda item: sum(item[1]) / len(item[1]))

        client_errors = sum(1 for r in external if 400 <= r.status < 500)
        server_errors = sum(1 for r in external if r.status >= 500)

        # 都沒有時寫「無」，「4xx 0 筆｜5xx 0 筆」乍看像是有錯誤；
        # 有任一種時兩個數字都列，方便對照
        errors = (
            f"4xx {client_errors:,} 筆｜5xx {server_errors:,} 筆"
            if client_errors or server_errors
            else "無"
        )

        sections.append(Section(
            heading="📋 總覽",
            facts=[
                ("請求數", f"{len(external):,} 筆（上週 {previous:,}{change}）"),
                ("錯誤", errors),
                ("最慢", f"{slowest} 平均 {sum(durations) / len(durations):.2f}s"),
            ],
        ))

        sections.append(Section(
            heading="🏆 端點排行",
            facts=top_facts(Counter(row.endpoint for row in external)),
        ))

        sections.append(Section(
            heading="👥 呼叫者",
            facts=top_facts(Counter(row.caller for row in external), caller_label, "個呼叫者"),
        ))

        # 粗略但夠用的分法：帶 Mozilla 的幾乎都是瀏覽器，其餘是爬蟲或串接程式
        browser = sum(1 for row in external if "Mozilla" in row.ua)

        sections.append(Section(
            heading="🌐 來源",
            facts=[("流量", f"程式 {len(external) - browser:,} 筆｜瀏覽器 {browser:,} 筆")]
            + top_facts(Counter(row.referer for row in external), referer_label, "個來源"),
        ))

    if internal:
        sections.append(Section(
            text=f"另有 {'、'.join(sorted({r.caller for r in internal}))} 的請求 {len(internal):,} 筆，未列入上方統計",
        ))

    # 每天的異常重新算一次，不另外保存狀態：規則與每日檢查相同，結果就一致
    alert_days = [
        day for day in days
        if find_alerts([row for row in records if row.date == day.isoformat()], day)
    ]

    if alert_days:
        footer = (
            "FAIL",
            f"本週有 {len(alert_days)} 天出現異常（{'、'.join(short_date(d) for d in alert_days)}），"
            f"詳見當天的異常通知",
        )
    else:
        footer = ("OK", "本週無異常")

    print(f"[{now_taipei_str()}] {SERVICE_NAME} 週報 {start}–{end}：{len(external)} 筆，異常 {len(alert_days)} 天")

    return Message(
        status="INFO",
        title=f"{SERVICE_NAME} 存取週報（{short_date(start)}–{short_date(end)}）",
        sections=sections,
        footer=footer,
        details=details,
    )


# =========================
# 進入點
# =========================

def build_details(files: list[str], unparsed: int, scope: str) -> str:
    span = f"{files[0]} … {files[-1]}" if len(files) > 1 else "、".join(files)

    text = (
        f"容器：{SERVICE_NAME}\n"
        f"統計範圍：{scope}\n"
        f"讀取：{LOG_DIR}/ 底下 {len(files)} 個檔（{span}）\n"
        f"執行時間：{now_taipei_str()}\n"
        f"執行紀錄：{REPORT_LOG_PATH}"
    )

    if unparsed:
        text += f"\n無法解析的行：{unparsed} 行（格式與 access_log 不符，未列入統計）"

    return text


def scheduled_messages(today: date) -> list[Message]:
    """排程執行：檢查昨天，週一另加上一週的週報"""
    yesterday = today - timedelta(days=1)
    is_monday = today.weekday() == 0

    # 週報要比上週，往回讀 14 天；平日只需要昨天
    start = yesterday - timedelta(days=13 if is_monday else 0)
    records, files, unparsed = load_records(start, yesterday)

    messages = []
    daily = daily_message(records, yesterday, build_details(files, unparsed, yesterday.isoformat()))

    if daily:
        messages.append(daily)

    if is_monday:
        week_start = yesterday - timedelta(days=6)
        messages.append(weekly_message(
            records, week_start, yesterday,
            build_details(files, unparsed, f"{week_start} – {yesterday}（上週為前 7 天）"),
        ))

    return messages


def test_weekly_message(today: date) -> Message:
    """
    --test-weekly：不論今天星期幾，用真實的 log 產生最近一個完整週（週一到週日）的週報。

    內容與週一排程送出的完全相同，只在標題加註「測試」，免得被誤認成排程的週報。
    """

    end = today - timedelta(days=today.weekday() + 1)
    start = end - timedelta(days=6)
    records, files, unparsed = load_records(start - timedelta(days=7), end)

    message = weekly_message(
        records, start, end, build_details(files, unparsed, f"{start} – {end}（上週為前 7 天）"),
    )
    message.title += "（測試）"

    return message


def test_alert_message(today: date) -> Message:
    """
    --test-alert：用固定的假資料走一次正式的異常規則與卡片格式，不讀 log。

    正式的 log 平常沒有異常，每日檢查不會發通知，沒辦法用真實資料看到異常卡片。
    """

    day = today - timedelta(days=1)
    fake = [
        Record(day.isoformat(), "-", "/v1/test", 500, 0.05, "test", "-"),
        Record(day.isoformat(), "-", "/v1/test", 429, 0.0, "test", "-"),
        Record(day.isoformat(), "-", "/v1/test", 200, SLOW_SECONDS + 1, "test", "-"),
    ]

    return Message(
        status="FAIL",
        title=f"{SERVICE_NAME} 存取紀錄有異常（測試，請忽略）",
        sections=[Section(text="這是手動測試的通知，以下為假資料，不是真的異常。")]
        + find_alerts(fake, day),
        details=f"容器：{SERVICE_NAME}\n執行時間：{now_taipei_str()}\n內容：固定的假資料，未讀取 log",
    )


def run(today: date, mode: str, dry_run: bool) -> None:
    if mode == "test-alert":
        messages = [test_alert_message(today)]
    elif mode == "test-weekly":
        messages = [test_weekly_message(today)]
    else:
        messages = scheduled_messages(today)

    for message in messages:
        if dry_run:
            print("-" * 60)
            print(message.plain_text())
            print("-" * 60)
        else:
            send(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="存取紀錄的每日異常檢查與週一週報")
    parser.add_argument("--date", type=date.fromisoformat, help="視為今天的日期 YYYY-MM-DD，預設為台北時間的今天")
    parser.add_argument("--dry-run", action="store_true", help="只印出通知內容，不送出")

    # 手動測試用，一行指令就能在 Teams 看到兩種通知
    tests = parser.add_mutually_exclusive_group()
    tests.add_argument(
        "--test-alert", dest="mode", action="store_const", const="test-alert",
        help="送一則假資料的異常通知，確認異常卡片與送出是否正常",
    )
    tests.add_argument(
        "--test-weekly", dest="mode", action="store_const", const="test-weekly",
        help="不論今天星期幾，送一則最近一個完整週的週報（標題加註測試）",
    )
    parser.set_defaults(mode="scheduled")
    args = parser.parse_args()

    today = args.date or now_taipei().date()

    try:
        run(today, args.mode, args.dry_run)

    except Exception as error:
        # 不靜默結束：讀不到 log 或程式出錯時改送一則失敗通知，否則等於通知機制無聲地停了
        print(traceback.format_exc(), file=sys.stderr)

        message = Message(
            status="FAIL",
            title=f"{SERVICE_NAME} 存取紀錄通知執行失敗",
            sections=[Section(text=f"沒有完成 {today} 的檢查：{error}")],
            details=(
                f"容器：{SERVICE_NAME}\n"
                f"執行時間：{now_taipei_str()}\n"
                f"執行紀錄：{REPORT_LOG_PATH}\n\n"
                f"{traceback.format_exc()}"
            ),
        )

        if args.dry_run:
            print(message.plain_text())
        else:
            send(message)

        sys.exit(1)


if __name__ == "__main__":
    main()
