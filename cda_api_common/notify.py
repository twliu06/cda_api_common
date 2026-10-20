"""
Teams 通知。

存取紀錄的異常與每週的週報靠這裡送到 Teams。伺服器沒有寄信服務，cron 的 MAILTO
無法投遞，所以用 Teams 的 incoming webhook。

webhook URL 視同密碼（任何人取得即可對該頻道發文），因此放在各 API `.env` 的
`TEAMS_WEBHOOK_URL`，與資料庫密碼走同一條路徑，不進版控。

**通知的成敗不拋例外**：URL 沒設定、網路不通、Teams 回錯，一律只印一行訊息就結束。
通知是附屬功能，它自己壞掉時不該再製造一個要追查的錯誤。

送 HTTP 用標準函式庫的 urllib，不額外引入相依套件。
"""

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

TIMEOUT_SECONDS = 20

# 長度上限，純粹是防爆用的保險。
#
# Teams 的 webhook 對整個 payload 有大小限制（約 28 KB），中文在 UTF-8 下一個字
# 3 個位元組。本文、除錯資訊、純文字備援三者合計的最壞情況要壓在這個限制以下，
# 所以分開設上限；實務上一天的異常遠不到這個量。
MAX_SECTIONS_CHARS = 5000
MAX_DETAILS_CHARS = 3000
MAX_FALLBACK_CHARS = 1000

# 狀態同時以顏色與標題前的 emoji 表示。顏色用 Adaptive Card 的語意色；emoji 是後備，
# 手機推播的預覽只顯示純文字，靠 emoji 仍看得出是週報還是異常。
#
# INFO 給週報用：週報是心跳，不代表「一切都好」，內文裡可能正列著問題，標成綠色的
# OK 會讓人誤會。真正的好壞放在結尾那句，另外上色。
Status = Literal["OK", "INFO", "FAIL"]

COLORS = {"OK": "good", "INFO": "accent", "FAIL": "attention"}
PREFIXES = {"OK": "✅ ", "INFO": "📊 ", "FAIL": "🚨 "}


@dataclass
class Section:
    """
    一段本文。heading 之下依序放 text、facts、code，有填的才出現。

    facts 用 Adaptive Card 的 FactSet：左欄名稱、右欄內容會自動對齊，
    是 1.2 版裡最接近表格、在手機上也不會擠的排法。
    """

    heading: str | None = None
    text: str | None = None
    facts: list[tuple[str, str]] = field(default_factory=list)
    # 等寬字型，放要貼到伺服器上執行的指令
    code: str | None = None

    def plain_text(self) -> str:
        """純文字版本：手機推播預覽、截斷長度、--dry-run 都用這個"""
        lines = [self.heading] if self.heading else []

        if self.text:
            lines.append(self.text)

        lines += [f"　{name}：{value}" for name, value in self.facts]

        if self.code:
            lines.append(self.code)

        return "\n".join(lines)


@dataclass
class Message:
    status: Literal["INFO", "FAIL"]
    title: str
    sections: list[Section]
    # 除錯資訊：容器、統計範圍、讀了哪些檔、原始錯誤，預設收合
    details: str
    # 接在本文後面、單獨上色的一句話（同一個 TextBlock 沒辦法局部上色）
    footer: tuple[Status, str] | None = None

    def plain_text(self) -> str:
        parts = [f"{PREFIXES[self.status]}{self.title}"]
        parts += [section.plain_text() for section in self.sections]

        if self.footer:
            parts.append(self.footer[1])

        return "\n\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    return f"{text[:limit]}\n\n……訊息過長已截斷"


def _fit_sections(sections: list[Section]) -> list[Section]:
    """本文超過上限時從後面的段落開始截，排在前面的段落比較重要"""
    budget = MAX_SECTIONS_CHARS
    fitted = []

    for section in sections:
        if budget <= 0:
            break

        text = section.plain_text()

        if len(text) <= budget:
            fitted.append(section)
            budget -= len(text)
            continue

        # 放不下時改成截斷後的純文字，內容不會整段消失
        truncated = _truncate(text, budget)
        fitted.append(Section(text=truncated))
        budget -= len(truncated)

    return fitted


def build_payload(message: Message) -> dict:
    heading = f"{PREFIXES[message.status]}{message.title}"
    sections = _fit_sections(message.sections)

    # version 維持 1.2：較新的版本可能讓整張卡片退化成無法閱讀的佔位訊息。
    # isVisible、fontType 與 Action.ToggleVisibility 自 1.2 起支援。
    blocks = [
        {
            "type": "TextBlock",
            "text": heading,
            "weight": "Bolder",
            "size": "Medium",
            "color": COLORS[message.status],
            "wrap": True,
        }
    ]

    for section in sections:
        if section.heading:
            blocks.append({
                "type": "TextBlock",
                "text": section.heading,
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            })

        if section.text:
            blocks.append({"type": "TextBlock", "text": section.text, "spacing": "Small", "wrap": True})

        if section.facts:
            blocks.append({
                "type": "FactSet",
                "facts": [{"title": name, "value": value} for name, value in section.facts],
                "spacing": "Small",
            })

        if section.code:
            blocks.append({
                "type": "TextBlock",
                "text": section.code,
                "fontType": "Monospace",
                "size": "Small",
                "spacing": "Small",
                "wrap": True,
            })

    if message.footer:
        status, text = message.footer
        blocks.append({
            "type": "TextBlock",
            "text": text,
            "weight": "Bolder",
            "color": COLORS[status],
            "spacing": "Medium",
            "wrap": True,
        })

    blocks.append({
        "type": "Container",
        "id": "details",
        "isVisible": False,
        "separator": True,
        "spacing": "Medium",
        "items": [
            {
                "type": "TextBlock",
                "text": _truncate(message.details, MAX_DETAILS_CHARS),
                "fontType": "Monospace",
                "size": "Small",
                "wrap": True,
            }
        ],
    })

    # 頂層 text 是後備：卡片無法渲染時至少讀得到純文字，手機推播的預覽也只有這個欄位。
    # 結尾那句最關鍵，排在標題後面，預覽被截短也看得到。
    fallback = "\n\n".join(
        [heading]
        + ([message.footer[1]] if message.footer else [])
        + [section.plain_text() for section in sections]
    )

    return {
        "type": "message",
        "text": _truncate(fallback, MAX_FALLBACK_CHARS),
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.2",
                    "body": blocks,
                    "actions": [
                        {
                            "type": "Action.ToggleVisibility",
                            "title": "顯示／隱藏除錯資訊",
                            "targetElements": ["details"],
                        }
                    ],
                },
            }
        ],
    }


def send(message: Message) -> None:
    """
    送出一則 Teams 通知。

    除錯資訊預設收合，按卡片底部的按鈕才展開：內容完整保留，只是不擋在結論前面。
    """

    webhook_url = os.getenv("TEAMS_WEBHOOK_URL", "").strip()

    if not webhook_url:
        print("（未設定 TEAMS_WEBHOOK_URL，略過通知）")
        return

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(build_payload(message), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        # 非 2xx 時 urlopen 會拋 HTTPError，一併由下面攔下
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            pass

        print(f"已送出 Teams 通知：{message.title}")

    except Exception as error:
        # 這裡刻意攔所有例外：通知只是附屬功能，失敗時印一行讓 log 看得到就好
        print(f"Teams 通知送出失敗：{error}")
