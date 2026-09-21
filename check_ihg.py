#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IHG 积分房（Reward Night）监控脚本 —— 配合 GitHub Actions 定时运行。

原理：
  调用 IHG 官网搜索页同款公开接口
  POST {domain}/availability/v3/hotels/offers?fieldset=summary,summary.rateRanges
  在返回的 ratePlanDefinitions 中找到 isRewardNight=true 的条目（rate code: IVANI），
  读取其 isAvailable 字段判断积分房是否可订。

零第三方依赖（仅 Python 标准库），适合直接跑在 GitHub Actions 上。

配置（全部来自环境变量）：
  IHG_HOTEL_CODES  必填。IHG 酒店代码，多个用英文逗号分隔，如 OKJJA
                   （代码 = 酒店详情页 URL 里的那段，如 ihg.com/hotels/cn/zh/okayama/OKJJA/hoteldetail）
  IHG_CHECKIN      必填。入住日期，YYYY-MM-DD
  IHG_CHECKOUT     必填。退房日期，YYYY-MM-DD
  RESEND_API_KEY   需要邮件通知时必填。Resend 的 API Key
  NOTIFY_EMAIL     需要邮件通知时必填。收件邮箱
  RESEND_FROM      可选。发件地址，默认 onboarding@resend.dev（Resend 免费版默认可用）
  IHG_API_DOMAIN   可选。默认 apis.ihg.com.cn，也可换成 apis.ihg.com
  STATUS_FILE      可选。状态文件路径，默认 .status/ihg_status.json

通知规则（用状态文件去重，避免每小时重复轰炸）：
  - 积分房从「无」变「有」→ 发邮件提醒
  - 积分房从「有」变「无」→ 发邮件告知
  - 状态不变 → 不发邮件
  - API 调用失败 → 不更新状态、不发邮件，进程以非零码退出（Actions 上会标红，便于发现问题）
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_DOMAIN = os.environ.get("IHG_API_DOMAIN", "apis.ihg.com.cn")
API_KEY = "pQM1YazQwnWi5AWXmoRoA5FSfW0S9x8A"  # IHG 网页端公开使用的 key
REWARD_RATE_CODE = "IVANI"  # Reward Night 的 internal rate code（即 URL 里的 RWD01）

API_URL = f"https://{API_DOMAIN}/availability/v3/hotels/offers?fieldset=summary,summary.rateRanges"

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json; charset=UTF-8",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
    "x-ihg-api-key": API_KEY,
    "ihg-language": "zh-CN",
    "referer": f"https://www.{'ihg.com.cn' if 'com.cn' in API_DOMAIN else 'ihg.com'}/",
}


class NoRetryError(RuntimeError):
    """确定性错误（如酒店代码无效），无需重试。"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def http_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=BASE_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def check_hotel(hotel_code: str, checkin: str, checkout: str, retries: int = 3) -> dict:
    """查询一家酒店的积分房是否可订。

    返回 {"available": bool, "points": int|None, "points_and_cash": str|None}。
    连续失败则抛异常。

    实测字段规律（2026-09 验证）：
      可订  -> 酒店级 rewardNightAvailable=true，并带 lowestPointsOnlyCost 等积分价；
               IVANI 条目无 isAvailable 字段
      不可订 -> rewardNightAvailable 缺失；IVANI 条目显式 isAvailable=false
    """
    payload = {
        "hotelMnemonics": [hotel_code],
        "radius": None,
        "maxRadius": None,
        "minHotels": 1,
        "incrementRadiusBy": None,
        "distanceUnit": "MI",
        "distanceType": "STRAIGHT_LINE",
        "startDate": checkin,
        "endDate": checkout,
        "geoLocation": None,
        "products": [
            {
                "productCode": "SR",
                "startDate": checkin,
                "endDate": checkout,
                "quantity": 1,
                "guestCounts": [{"otaCode": "AQC10", "count": 2}],
            }
        ],
        "rates": {"ratePlanCodes": [{"internal": REWARD_RATE_CODE}]},
        "options": {"summary": {"includeTaxDetails": True}},
    }

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            result = http_post_json(API_URL, payload)
            hotels = result.get("hotels") or []
            if not hotels:
                raise RuntimeError(f"返回结果中没有酒店（可能酒店代码错误）: {hotel_code}")
            h = hotels[0]

            # 无效酒店代码的识别
            warnings = result.get("warnings") or []
            invalid_code = any(w.get("code") in ("CRS_50010", "AVL_100027") for w in warnings)
            if h.get("availabilityStatus") == "UNKNOWN" or invalid_code:
                raise NoRetryError(f"酒店代码无效或无法识别: {hotel_code}（请核对酒店详情页 URL 中的代码）")

            # 主判断：酒店级 rewardNightAvailable
            if "rewardNightAvailable" in h:
                available = bool(h["rewardNightAvailable"])
            else:
                # 兜底：IVANI 条目的 isAvailable（显式 false = 不可订，缺失 = 可订）
                available = None
                for rpd in h.get("ratePlanDefinitions", []):
                    if rpd.get("isRewardNight"):
                        available = rpd.get("isAvailable", True)
                        break
                if available is None:
                    raise NoRetryError(f"返回数据异常（未找到积分房条目）: {hotel_code}")

            info = {"available": available, "points": None, "points_and_cash": None}
            if available:
                p = (h.get("lowestPointsOnlyCost") or {}).get("points")
                info["points"] = p
                pc = h.get("lowestPointsAndCashCost") or {}
                if pc.get("points") is not None:
                    info["points_and_cash"] = f"{pc['points']:,} 积分 + {pc.get('cash')} {h.get('propertyCurrency', '')}".strip()
            price = f", 纯积分 {p:,} 分/晚" if available and p else ""
            pac = f", 积分+现金 {info['points_and_cash']}" if info["points_and_cash"] else ""
            log(f"{hotel_code}: 积分房可订 = {available}{price}{pac}")
            return info
        except NoRetryError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"{hotel_code}: 第 {attempt} 次请求失败: {e}")
            if attempt < retries:
                time.sleep(5 * attempt)
    raise RuntimeError(f"{hotel_code}: 连续 {retries} 次请求均失败: {last_err}")


def booking_url(hotel_code: str, checkin: str, checkout: str) -> str:
    """生成 IHG 官网积分房搜索直达链接（点开即可下单）。"""
    ci = datetime.strptime(checkin, "%Y-%m-%d")
    co = datetime.strptime(checkout, "%Y-%m-%d")
    return (
        "https://www.ihg.com.cn/hotels/cn/zh/find-hotels/hotel-search"
        f"?qSlH={hotel_code}&qDest={hotel_code}"
        f"&qCiD={ci.day:02d}&qCiMy={ci.month:02d}{ci.year}"
        f"&qCoD={co.day:02d}&qCoMy={co.month:02d}{co.year}"
        "&qAAR=RWD01&qRtP=RWD01&qRms=1&qAdlt=2&qChld=0&qDr=1"
    )


def send_email(subject: str, text: str) -> None:
    """通过 Resend 发送提醒邮件；未配置 key 时只打印预览。"""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to_email = os.environ.get("NOTIFY_EMAIL", "").strip()
    from_email = os.environ.get("RESEND_FROM", "onboarding@resend.dev").strip()

    if not api_key or not to_email:
        log("未配置 RESEND_API_KEY / NOTIFY_EMAIL，跳过发信，仅打印邮件内容预览：")
        print(f"  --- 邮件预览 ---\n  主题: {subject}\n  正文:\n{text}\n  ----------------", flush=True)
        return

    payload = json.dumps(
        {"from": from_email, "to": [to_email], "subject": subject, "text": text}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    log(f"提醒邮件已发送至 {to_email}: {subject}")


def load_status(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # 兼容带 BOM 的文件
                return json.load(f)
        except Exception:  # noqa: BLE001
            log(f"状态文件损坏，忽略旧状态: {path}")
    return {}


def save_status(path: str, status: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def main() -> int:
    hotel_codes = [c.strip().upper() for c in os.environ.get("IHG_HOTEL_CODES", "").split(",") if c.strip()]
    checkin = os.environ.get("IHG_CHECKIN", "").strip()
    checkout = os.environ.get("IHG_CHECKOUT", "").strip()
    status_file = os.environ.get("STATUS_FILE", os.path.join(".status", "ihg_status.json"))

    if not hotel_codes:
        log("缺少 IHG_HOTEL_CODES 环境变量")
        return 1
    if not checkin or not checkout:
        log("缺少 IHG_CHECKIN / IHG_CHECKOUT 环境变量")
        return 1

    log(f"开始检查: 酒店={hotel_codes}, 入住={checkin}, 退房={checkout}")
    now_iso = datetime.now(timezone.utc).isoformat()
    status = load_status(status_file)
    results: dict[str, dict] = {}
    failures: list[str] = []
    changed: list[tuple[str, bool, bool]] = []  # (code, old, new)

    for code in hotel_codes:
        try:
            results[code] = check_hotel(code, checkin, checkout)
        except Exception as e:  # noqa: BLE001
            log(f"检查失败，保留旧状态: {code}: {e}")
            failures.append(code)

    for code, info in results.items():
        available = info["available"]
        old = status.get(code, {}).get("available")
        status[code] = {"available": available, "checked_at": now_iso}
        if old is not None and old != available:
            changed.append((code, old, available))

    # 状态有变化才发邮件；首次运行不发（避免初始化噪音），只记录基线
    if changed:
        lines = []
        subjects = []
        for code, old, new in changed:
            arrow = "有积分房了！" if new else "积分房没了"
            subjects.append(f"{code} {arrow}")
            lines.append(f"酒店代码: {code}")
            lines.append(f"入住: {checkin} ~ 退房: {checkout}")
            lines.append(f"状态变化: {'可订' if old else '不可订'} -> {'可订' if new else '不可订'}")
            info = results[code]
            if new:
                if info.get("points"):
                    lines.append(f"所需积分: {info['points']:,} 分/晚（纯积分兑换）")
                if info.get("points_and_cash"):
                    lines.append(f"积分+现金: {info['points_and_cash']}（另可选）")
            lines.append(f"直达链接: {booking_url(code, checkin, checkout)}")
            lines.append("提示: 积分房数量有限，看到有房请尽快下手；价格以下单页为准。")
            lines.append("")
        subject = f"[IHG积分房] {'; '.join(subjects)}"
        send_email(subject, "\n".join(lines))
    else:
        log("状态无变化，不发送邮件。")

    if results:
        save_status(status_file, status)
        log("当前状态: " + ", ".join(
            f"{c}={'可订' + (f"({i['points']:,}分)" if i['points'] else '') if i['available'] else '不可订'}"
            for c, i in results.items()))
        log(f"状态已写入 {status_file}")

    if failures:
        log(f"以下酒店检查失败: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
