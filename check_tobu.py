#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tobu Hotel Levant Tokyo -> Tokyo Disney Resort shuttle vacancy watcher.

Fixed target:
- Direction: 東武ホテルレバント東京 -> 東京ディズニーリゾート
- Passengers: 2
- Ride date: 2026-09-10
- Departure: ホテル 7:00発
- Monitor until: end of 2026-09-09 (Asia/Taipei)

This script performs ONE check and exits.
GitHub Actions is responsible for running it periodically.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


URL = "https://tobuhotel-levant.tdr-bus.blockservice.jp/index_jp.php"

TARGET_YEAR = 2026
TARGET_MONTH = 9
TARGET_DAY = 10
TARGET_PASSENGERS = 2

TARGET_DEPARTURE_RE = re.compile(
    r"ホテル\s*[7７]\s*[：:]\s*[0０][0０]\s*発",
    re.IGNORECASE,
)
TARGET_DATE_RE = re.compile(
    r"2026年\s*0?9月\s*10日",
    re.IGNORECASE,
)
SEATS_RE = re.compile(r"残り\s*(\d+)\s*席")

TAIPEI = ZoneInfo("Asia/Taipei")
MONITOR_START = datetime(2026, 8, 30, 0, 0, 0, tzinfo=TAIPEI)
MONITOR_END = datetime(2026, 9, 10, 0, 0, 0, tzinfo=TAIPEI)

DEBUG_DIR = Path("debug")


def log(message: str) -> None:
    now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now} Asia/Taipei] {message}", flush=True)


def write_github_summary(text: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except Exception as exc:
        log(f"寫入 GitHub Summary 失敗：{exc}")


def http_post_json(url: str, payload: dict, timeout: int = 15) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "tobu-bus-watch/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        response.read()


def notify_discord(message: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False

    # 空位出現時直接 @everyone，讓 Discord 觸發提及通知。
    # allowed_mentions 明確允許解析 @everyone。
    payload = {
        "content": f"@everyone {message}",
        "allowed_mentions": {
            "parse": ["everyone"]
        }
    }

    http_post_json(webhook, payload)
    log("已送出 Discord @everyone 通知。")
    return True


def notify_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "tobu-bus-watch/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        response.read()

    log("已送出 Telegram 通知。")
    return True


def send_notifications(message: str) -> int:
    sent = 0

    for sender, name in (
        (notify_discord, "Discord"),
        (notify_telegram, "Telegram"),
    ):
        try:
            if sender(message):
                sent += 1
        except Exception as exc:
            log(f"{name} 通知失敗：{exc}")

    if sent == 0:
        log("警告：目前沒有設定 Discord 或 Telegram 通知。")
    return sent


def monitoring_is_active(now: datetime | None = None) -> bool:
    now = now or datetime.now(TAIPEI)
    return MONITOR_START <= now < MONITOR_END


async def save_screenshot(page, filename: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_DIR / filename), full_page=True)
        log(f"已儲存除錯畫面：{DEBUG_DIR / filename}")
    except Exception as exc:
        log(f"截圖失敗：{exc}")


async def numeric_options(select) -> list[int]:
    values: list[int] = []
    options = select.locator("option")

    for i in range(await options.count()):
        option = options.nth(i)
        candidates = [
            (await option.get_attribute("value") or "").strip(),
            (await option.inner_text()).strip(),
        ]

        found = None
        for candidate in candidates:
            m = re.search(r"\d+", candidate)
            if m:
                found = int(m.group(0))
                break

        if found is not None:
            values.append(found)

    return values


async def select_numeric(select, target: int, field_name: str) -> None:
    options = select.locator("option")

    for i in range(await options.count()):
        option = options.nth(i)
        text = (await option.inner_text()).strip()
        value = (await option.get_attribute("value") or "").strip()

        matches = False

        for candidate in (value, text):
            nums = re.findall(r"\d+", candidate)
            if any(int(n) == target for n in nums):
                matches = True
                break

        if matches:
            await select.select_option(index=i)
            log(f"{field_name} -> {target}")
            return

    raise RuntimeError(f"找不到 {field_name}={target} 的選項。")


async def classify_selects(page):
    """
    Identify the passenger/year/month/day dropdowns by their numeric ranges
    instead of relying on fragile CSS ids/classes.

    Expected:
      passengers: 1..8
      year:       includes 2026
      month:      <= 12
      day:        up to 28..31
    """
    selects = page.locator("select")
    info = []

    for i in range(await selects.count()):
        sel = selects.nth(i)
        nums = await numeric_options(sel)

        if nums:
            info.append((sel, nums))

    year_select = None
    passenger_select = None
    day_select = None
    month_select = None

    for sel, nums in info:
        if any(n >= 2000 for n in nums) and TARGET_YEAR in nums:
            year_select = sel
            break

    # Passenger selector should contain all 1..8 and nothing above 8.
    for sel, nums in info:
        unique = set(nums)
        if set(range(1, 9)).issubset(unique) and max(unique) <= 8:
            passenger_select = sel
            break

    # Day selector has many values and reaches at least 28.
    for sel, nums in info:
        unique = set(nums)
        if max(unique) >= 28 and max(unique) <= 31 and len(unique) >= 28:
            day_select = sel
            break

    # Month selector: numeric values within 1..12 and is not passenger/day/year.
    for sel, nums in info:
        if sel in (year_select, passenger_select, day_select):
            continue
        unique = set(nums)
        if unique and min(unique) >= 1 and max(unique) <= 12:
            month_select = sel
            break

    missing = []
    if passenger_select is None:
        missing.append("人数")
    if year_select is None:
        missing.append("年")
    if month_select is None:
        missing.append("月")
    if day_select is None:
        missing.append("日")

    if missing:
        raise RuntimeError("無法辨識下拉選單：" + "、".join(missing))

    return passenger_select, year_select, month_select, day_select


async def choose_direction(page) -> None:
    """
    The requested direction is the first option shown on the official page:
    東武ホテルレバント東京 -> 東京ディズニーリゾート
    """
    radios = page.locator('input[type="radio"]')

    if await radios.count() < 1:
        raise RuntimeError("找不到 STEP 01 的方向選項。")

    # Prefer a radio whose nearby text explicitly contains both locations
    # in hotel -> Disney order.
    for i in range(await radios.count()):
        radio = radios.nth(i)

        nearby = await radio.evaluate(
            """
            el => {
                const texts = [];
                let n = el;
                for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
                    if (n.innerText) texts.push(n.innerText);
                }
                if (el.id) {
                    for (const label of document.querySelectorAll('label')) {
                        if (label.htmlFor === el.id && label.innerText) {
                            texts.push(label.innerText);
                        }
                    }
                }
                return texts.join("\\n");
            }
            """
        )

        compact = re.sub(r"\s+", "", nearby)
        hotel_pos = compact.find("東武ホテルレバント東京")
        disney_pos = compact.find("東京ディズニーリゾート")

        if hotel_pos >= 0 and disney_pos >= 0 and hotel_pos < disney_pos:
            await radio.check()
            log("方向 -> 東武ホテルレバント東京 → 東京ディズニーリゾート")
            return

    # Official page currently displays hotel -> Disney as the first radio.
    await radios.first.check()
    log("方向 -> 使用第一個選項（飯店 → Disney）")


async def click_search(page) -> None:
    candidates = [
        page.get_by_role("button", name=re.compile(r"検索する")),
        page.locator('input[type="submit"]'),
        page.locator('button:has-text("検索する")'),
        page.get_by_text("検索する", exact=True),
    ]

    for candidate in candidates:
        try:
            if await candidate.count() > 0:
                await candidate.first.click()
                log("已按下「検索する」。")
                return
        except Exception:
            continue

    raise RuntimeError("找不到「検索する」按鈕。")


async def locate_target_card(page) -> dict | None:
    """
    Find the 7:00 departure and its OWN seat badge by visual page position.

    Why:
    The site's DOM/text order does not necessarily match the visual card order.
    Reading BODY text sequentially can therefore pair:

        7:00 -> 残り3席   (WRONG; 3 seats actually belongs to 9:20)

    This version uses rendered element coordinates:
      1. Find every visible "ホテル H:MM発" heading.
      2. Find every visible exact "残りN席" badge.
      3. Assign each seat badge to the departure heading that is visually
         closest in the vertical direction.
      4. Read ONLY the badge assigned to 7:00.

    Fail-safe:
    If pairing is ambiguous or too far apart, return None rather than
    triggering a false vacancy alert.
    """

    result = await page.evaluate(
        r"""
        () => {
            const normalize = (s) =>
                (s || "")
                    .normalize("NFKC")
                    .replace(/\u3000/g, " ")
                    .replace(/\u00a0/g, " ")
                    .replace(/[ \t]+/g, " ")
                    .trim();

            const departureRe =
                /^ホテル\s*(\d{1,2})\s*:\s*(\d{2})\s*発$/i;

            const seatsRe =
                /^残り\s*(\d+)\s*席$/;

            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);

                return (
                    r.width > 0 &&
                    r.height > 0 &&
                    style.display !== "none" &&
                    style.visibility !== "hidden" &&
                    Number(style.opacity || "1") > 0
                );
            };

            const rectInfo = (el) => {
                const r = el.getBoundingClientRect();

                return {
                    left: r.left,
                    right: r.right,
                    top: r.top,
                    bottom: r.bottom,
                    width: r.width,
                    height: r.height,
                    cx: r.left + r.width / 2,
                    cy: r.top + r.height / 2,
                };
            };

            const all = Array.from(document.querySelectorAll("body *"));

            /*
             * Use exact-text elements only.
             * This intentionally excludes large parent containers whose
             * innerText contains an entire result card or multiple cards.
             */
            let departures = [];

            for (const el of all) {
                if (!visible(el)) continue;

                const text = normalize(el.innerText || el.textContent || "");
                const m = text.match(departureRe);

                if (!m) continue;

                departures.push({
                    hour: Number(m[1]),
                    minute: Number(m[2]),
                    text,
                    rect: rectInfo(el),
                });
            }

            let seats = [];

            for (const el of all) {
                if (!visible(el)) continue;

                const text = normalize(el.innerText || el.textContent || "");
                const m = text.match(seatsRe);

                if (!m) continue;

                seats.push({
                    seats: Number(m[1]),
                    text,
                    rect: rectInfo(el),
                });
            }

            /*
             * Some sites render the same exact text in nested inline tags.
             * Deduplicate visually overlapping candidates.
             */
            const dedupeByPosition = (items, keyFn) => {
                const out = [];

                for (const item of items) {
                    const key = keyFn(item);

                    const duplicate = out.some((x) => {
                        if (keyFn(x) !== key) return false;

                        return (
                            Math.abs(x.rect.cx - item.rect.cx) < 3 &&
                            Math.abs(x.rect.cy - item.rect.cy) < 3
                        );
                    });

                    if (!duplicate) out.push(item);
                }

                return out;
            };

            departures = dedupeByPosition(
                departures,
                (x) => `${x.hour}:${x.minute}`
            );

            seats = dedupeByPosition(
                seats,
                (x) => String(x.seats)
            );

            if (!departures.length) {
                return {
                    ok: false,
                    reason: "no_departures",
                    departures: [],
                    seats,
                };
            }

            const target = departures.find(
                (x) => x.hour === 7 && x.minute === 0
            );

            if (!target) {
                return {
                    ok: false,
                    reason: "no_7am",
                    departures,
                    seats,
                };
            }

            if (!seats.length) {
                return {
                    ok: false,
                    reason: "no_seat_badges",
                    departures,
                    seats: [],
                };
            }

            /*
             * Pair every seat badge with the departure whose heading is
             * vertically nearest to it.
             *
             * On this page, departure title and "残りN席" are on the same
             * horizontal row of each card, so this is much safer than DOM order.
             */
            const assignments = seats.map((seat) => {
                const ranked = departures
                    .map((dep) => ({
                        dep,
                        dy: Math.abs(dep.rect.cy - seat.rect.cy),
                    }))
                    .sort((a, b) => a.dy - b.dy);

                return {
                    seat,
                    nearest: ranked[0],
                    second: ranked[1] || null,
                };
            });

            const targetAssignments = assignments
                .filter(
                    (a) =>
                        a.nearest.dep.hour === 7 &&
                        a.nearest.dep.minute === 0
                )
                .sort((a, b) => a.nearest.dy - b.nearest.dy);

            if (!targetAssignments.length) {
                return {
                    ok: false,
                    reason: "no_seat_assigned_to_7am",
                    departures,
                    seats,
                    assignments,
                };
            }

            const best = targetAssignments[0];

            /*
             * Safety checks:
             * - Seat badge should be close to the 7:00 heading vertically.
             * - If another departure is almost equally close, pairing is
             *   ambiguous; fail instead of notifying incorrectly.
             */
            const MAX_VERTICAL_DISTANCE = 120;

            if (best.nearest.dy > MAX_VERTICAL_DISTANCE) {
                return {
                    ok: false,
                    reason: "seat_too_far_from_7am",
                    departures,
                    seats,
                    assignments,
                };
            }

            if (
                best.second &&
                Math.abs(best.second.dy - best.nearest.dy) < 20
            ) {
                return {
                    ok: false,
                    reason: "ambiguous_pairing",
                    departures,
                    seats,
                    assignments,
                };
            }

            return {
                ok: true,
                seats: best.seat.seats,
                text:
                    `ホテル 7:00発 -> 残り${best.seat.seats}席 ` +
                    `(vertical distance=${best.nearest.dy.toFixed(1)}px)`,
                departures,
                seatBadges: seats,
                assignments,
            };
        }
        """
    )

    # Diagnostic logging: always show what GitHub visually detected.
    departures = result.get("departures", []) if result else []
    seat_badges = (
        result.get("seatBadges", result.get("seats", []))
        if result
        else []
    )

    log("GitHub 畫面辨識到的班次：")
    if departures:
        for dep in departures:
            log(
                "  ホテル "
                f"{dep['hour']}:{dep['minute']:02d}発 "
                f"(y={dep['rect']['cy']:.1f})"
            )
    else:
        log("  無")

    log("GitHub 畫面辨識到的座位標籤：")
    if isinstance(seat_badges, list) and seat_badges:
        for seat in seat_badges:
            log(
                f"  残り{seat['seats']}席 "
                f"(y={seat['rect']['cy']:.1f})"
            )
    else:
        log("  無")

    if not result or not result.get("ok"):
        reason = result.get("reason", "unknown") if result else "no_result"
        log(f"7:00 班次座位配對失敗：{reason}")

        assignments = result.get("assignments", []) if result else []

        if assignments:
            log("座位標籤與班次的視覺配對診斷：")
            for a in assignments:
                seat = a["seat"]
                nearest = a["nearest"]
                dep = nearest["dep"]

                log(
                    f"  残り{seat['seats']}席 -> "
                    f"ホテル {dep['hour']}:{dep['minute']:02d}発 "
                    f"(dy={nearest['dy']:.1f}px)"
                )

        return None

    log(
        "7:00 視覺配對成功："
        f"ホテル 7:00発 -> 残り{result['seats']}席"
    )

    return {
        "text": result["text"],
        "seats": int(result["seats"]),
        "tag": "VISUAL_POSITION_MATCH",
        "className": "",
        "htmlLength": len(result["text"]),
    }


async def run_check() -> int:
    now = datetime.now(TAIPEI)

    if not monitoring_is_active(now):
        log(
            "監控期間已結束或尚未開始。"
            " 本專案只在 2026/08/30～2026/09/09（台灣時間）有效。"
        )
        write_github_summary(
            "## Tobu Bus Watch\n"
            "監控期間已結束；沒有連線查詢網站。"
        )
        return 0

    headless = os.getenv("HEADLESS", "1") != "0"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140 Safari/537.36"
            ),
        )

        page = await context.new_page()
        page.set_default_timeout(15000)

        try:
            log("開啟東武接駁車預約頁面。")
            await page.goto(URL, wait_until="domcontentloaded", timeout=30000)

            await choose_direction(page)

            passenger, year, month, day = await classify_selects(page)

            await select_numeric(passenger, TARGET_PASSENGERS, "人数")
            await select_numeric(year, TARGET_YEAR, "年")

            # Some date forms rebuild later dropdowns after a change.
            await select_numeric(month, TARGET_MONTH, "月")
            await page.wait_for_timeout(400)

            # Re-classify in case the day <select> was rebuilt.
            passenger, year, month, day = await classify_selects(page)
            await select_numeric(day, TARGET_DAY, "日")

            await click_search(page)

            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeoutError:
                # Some pages keep minor network activity alive.
                pass

            await page.wait_for_timeout(1200)

            target = await locate_target_card(page)

            if target is None:
                await save_screenshot(page, "target_not_found.png")
                body = (await page.locator("body").inner_text())[:5000]
                log("找不到 2026/9/10「ホテル 7:00発」班次卡片。")
                log("頁面文字前 5000 字：\n" + body)

                write_github_summary(
                    "## ⚠️ Tobu Bus Watch\n"
                    "找不到目標班次卡片，可能是網站改版、查詢失敗或班次暫時未顯示。\n"
                    "已嘗試保存 `target_not_found.png`。"
                )
                return 1

            seats = int(target["seats"])
            card_text = re.sub(r"\s+", " ", target["text"]).strip()

            log(f"鎖定目標：ホテル 7:00発 / 2026-09-10 / 2人")
            log(f"目前顯示：残り{seats}席")
            log(f"班次卡片：{card_text[:800]}")

            if seats >= TARGET_PASSENGERS:
                await save_screenshot(page, "VACANCY_FOUND.png")

                message = (
                    "🚨 東武接駁車有位置了！\n\n"
                    "東武ホテルレバント東京 → 東京ディズニーリゾート\n"
                    "日期：2026/09/10\n"
                    "班次：ホテル 7:00発\n"
                    "人數：2 人\n"
                    f"目前顯示：残り{seats}席\n\n"
                    "請立即進入預約：\n"
                    f"{URL}"
                )

                sent = send_notifications(message)

                write_github_summary(
                    "## 🚨 發現空位\n\n"
                    f"- 日期：2026/09/10\n"
                    f"- 班次：ホテル 7:00発\n"
                    f"- 人數需求：2\n"
                    f"- 剩餘座位：**{seats}**\n"
                    f"- 外部通知成功數：{sent}\n"
                )

                log("★★★★★ 符合條件：剩餘座位 >= 2 ★★★★★")
                return 0

            write_github_summary(
                "## Tobu Bus Watch\n\n"
                f"- 2026/09/10 ホテル 7:00発\n"
                f"- 目前：残り{seats}席\n"
                "- 結果：尚不足 2 席，不通知\n"
            )

            log("尚未符合條件（需要至少 2 席）。")
            return 0

        except Exception as exc:
            await save_screenshot(page, "error.png")
            log(f"檢查失敗：{type(exc).__name__}: {exc}")
            write_github_summary(
                "## ❌ Tobu Bus Watch 執行失敗\n\n"
                f"`{type(exc).__name__}: {exc}`\n"
            )
            return 1

        finally:
            await browser.close()


def main() -> None:
    try:
        code = asyncio.run(run_check())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
