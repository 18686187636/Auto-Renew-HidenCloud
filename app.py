#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, random, requests, json
from playwright.sync_api import sync_playwright

COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""
EMAIL        = os.environ.get('EMAIL') or ""
PASSWORD     = os.environ.get('PASSWORD') or ""
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""
ACCOUNTS_JSON = os.environ.get('ACCOUNTS_JSON') or ""

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

INVOICE_URL_KEYWORDS = ("/payment/invoice/",)
UNPAID_INVOICES_URL  = f"{BASE_URL}/invoices?where=unpaid"

WAIT_RENDER_BEFORE_CF = 5
CF_TURNSTILE_TIMEOUT  = 45   # 增加到 45 秒，给 Turnstile 更多时间
NAV_POLL_SECONDS      = 30
WAIT_AFTER_PAY        = 15

TURNSTILE_IFRAME_SEL = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[title*="cloudflare"], '
    'iframe[src*="turnstile"]'
)


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def mask_email(email):
    if not email:
        return "未知账号"
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return email[:2] + '****'


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
"""


def get_current_ip(proxy_server=None):
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"


def send_telegram_notification(status, old_due, new_due, email):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {mask_email(email)}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        log(f"❌ Telegram 通知失败: {resp.text}")
        return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False


def mouse_click_element(page, locator, label=""):
    try:
        box = locator.bounding_box()
        if not box:
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        log(f"🖱️ 物理点击 {label} ({x:.0f}, {y:.0f})")
        page.mouse.move(x - random.uniform(40, 80), y - random.uniform(20, 40))
        time.sleep(random.uniform(0.15, 0.35))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.08, 0.18))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.up()
        return True
    except Exception as e:
        log(f"⚠️ 物理点击 {label} 失败: {e}")
        return False


def handle_cloudflare(page):
    if page.locator(TURNSTILE_IFRAME_SEL).count() == 0:
        return True
    log("⚠️ 检测到 Cloudflare 验证...")
    start = time.time()
    while time.time() - start < 60:
        if page.locator(TURNSTILE_IFRAME_SEL).count() == 0:
            log("✅ CF 验证通过！")
            return True
        try:
            box = page.locator(TURNSTILE_IFRAME_SEL).first.bounding_box()
            if box:
                x = box["x"] + 30
                y = box["y"] + box["height"] / 2
                page.mouse.move(x - 40, y - 20)
                time.sleep(0.3)
                page.mouse.click(x, y)
                time.sleep(5)
        except Exception:
            time.sleep(1)
    return False


def _get_turnstile_token(page):
    """检查 cf-turnstile-response 隐藏 input 是否已被填充"""
    try:
        val = page.evaluate(
            "() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]'); return el ? el.value : ''; }"
        )
        return val or ""
    except Exception:
        return ""


def solve_turnstile_checkbox(page, timeout=45):
    """
    三重策略处理 Cloudflare Turnstile：
      A. 遍历 page.frames，直接在 CF frame 内部点击 checkbox / body
      B. frame_locator + position 在 iframe 内部坐标点击
      C. 外层 page 鼠标坐标点击 + down/up 模拟人类行为
    每次点击后都检查 cf-turnstile-response token 是否已填充。
    """
    log(f"🔒 开始处理 Turnstile (超时 {timeout}s)")
    start = time.time()
    attempt = 0

    while time.time() - start < timeout:
        attempt += 1

        # 1. 检查 token 是否已填充
        token = _get_turnstile_token(page)
        if token and len(token) > 10:
            log(f"✅ Turnstile token 已填充 (len={len(token)}, attempt={attempt})")
            return True

        # 2. 每次循环都尝试关闭 Cookie 横幅
        close_cookie_consent(page)

        iframe_count = page.locator(TURNSTILE_IFRAME_SEL).count()
        if iframe_count == 0:
            # iframe 已消失，再确认一次 token
            time.sleep(1)
            token = _get_turnstile_token(page)
            if token and len(token) > 10:
                log(f"✅ Turnstile 已通过（iframe 消失）")
                return True
            time.sleep(1)
            continue

        log(f"🔍 attempt={attempt}: 检测到 {iframe_count} 个 CF iframe")

        # ===== 策略 A：直接操作 frame 对象 =====
        try:
            for frame in page.frames:
                furl = frame.url or ""
                if "challenges.cloudflare.com" in furl:
                    # A1：尝试内部 checkbox 元素
                    try:
                        cb = frame.locator('input[type="checkbox"]').first
                        if cb.count() > 0:
                            cb.click(timeout=2000, force=True)
                            log("  ✅ A1: frame 内部 checkbox 点击")
                            time.sleep(2)
                    except Exception as e:
                        log(f"  A1 失败: {e}")
                    # A2：尝试内部 [role=checkbox]
                    try:
                        rcb = frame.locator('[role="checkbox"]').first
                        if rcb.count() > 0:
                            rcb.click(timeout=2000, force=True)
                            log("  ✅ A2: frame 内部 role=checkbox 点击")
                            time.sleep(2)
                    except Exception:
                        pass
                    # A3：直接点击 frame body 的 checkbox 区域
                    try:
                        frame.locator('body').click(
                            position={"x": 30, "y": 32},
                            timeout=2000, force=True
                        )
                        log("  ✅ A3: frame body 坐标点击")
                        time.sleep(2)
                    except Exception as e:
                        log(f"  A3 失败: {e}")
        except Exception as e:
            log(f"  策略A 异常: {e}")

        # A 策略点击后检查 token
        token = _get_turnstile_token(page)
        if token and len(token) > 10:
            log(f"✅ Turnstile token 已填充 (len={len(token)})")
            return True

        # ===== 策略 B：frame_locator + position =====
        try:
            fl = page.frame_locator(TURNSTILE_IFRAME_SEL).first
            try:
                body = fl.locator('body').first
                body.click(position={"x": 30, "y": 32}, timeout=3000, force=True)
                log("  ✅ B: frame_locator body 坐标点击")
            except Exception as e:
                log(f"  B 失败: {e}")
        except Exception as e:
            log(f"  策略B 异常: {e}")

        time.sleep(2)

        token = _get_turnstile_token(page)
        if token and len(token) > 10:
            log(f"✅ Turnstile token 已填充 (len={len(token)})")
            return True

        # ===== 策略 C：外层鼠标坐标点击 =====
        try:
            box = page.locator(TURNSTILE_IFRAME_SEL).first.bounding_box()
            if box:
                # 尝试 2 个不同的水平位置
                for ratio in (0.05, 0.09):
                    x = box["x"] + box["width"] * ratio
                    y = box["y"] + box["height"] / 2
                    log(f"  🖱️ C: 物理点击 ({x:.0f}, {y:.0f})")
                    # 模拟人类移动
                    page.mouse.move(
                        x - random.uniform(70, 110),
                        y - random.uniform(30, 55)
                    )
                    time.sleep(random.uniform(0.25, 0.45))
                    page.mouse.move(
                        x + random.uniform(-3, 3),
                        y + random.uniform(-3, 3)
                    )
                    time.sleep(random.uniform(0.1, 0.25))
                    page.mouse.down()
                    time.sleep(random.uniform(0.05, 0.14))
                    page.mouse.up()
                    time.sleep(2)

                    token = _get_turnstile_token(page)
                    if token and len(token) > 10:
                        log(f"✅ Turnstile token 已填充 (len={len(token)})")
                        return True
        except Exception as e:
            log(f"  策略C 异常: {e}")

        time.sleep(2)

    log("❌ CF Turnstile 验证超时")
    return False


def close_cookie_consent(page):
    """强化 Cookie 横幅关闭逻辑"""
    try:
        if page.locator('.fc-consent-root').count() == 0:
            return
        for sel in [
            'button:has-text("Accept")',
            'button:has-text("Accept All")',
            'button:has-text("I agree")',
            'button:has-text("Allow")',
            '.fc-cta-consent',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=1500)
                    log("🍪 已点击关闭 Cookie 横幅")
                    time.sleep(0.8)
                    return
            except Exception:
                continue
        try:
            page.evaluate("document.querySelector('.fc-consent-root')?.remove()")
            log("🍪 已 JS 移除 Cookie 横幅")
            time.sleep(0.5)
        except Exception:
            pass
    except Exception:
        pass


def login(page, email, password, cookie_value):
    if cookie_value:
        log("📇 尝试 Cookie 登录...")
        try:
            page.context.add_cookies([{
                'name': 'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d',
                'value': cookie_value,
                'domain': 'dash.hidencloud.com',
                'path': '/',
                'expires': int(time.time()) + 3600 * 24 * 365,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax'
            }])
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            log(f"📝 当前Title: {page.title()}")
            if "auth/login" not in page.url:
                log("✅ Cookie 登录成功！")
                return True
            log("❌ Cookie 失效")
        except Exception as e:
            log(f"⚠️ Cookie 登录异常: {e}")

    if not email or not password:
        return False

    log("💣 尝试账号密码登录...")
    try:
        try:
            page.context.clear_cookies()
        except Exception:
            pass
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        time.sleep(0.5)
        handle_cloudflare(page)
        page.click('button[type="submit"]')
        time.sleep(3)
        handle_cloudflare(page)
        page.wait_for_url(f"{BASE_URL}/*", timeout=30000)
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        if "auth/login" in page.url:
            return False
        log("✅ 账号密码登录成功！")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return False


def get_server_id(page):
    try:
        handle_cloudflare(page)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")
        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            log(f"✅ Server ID: {matches[0]}")
            return matches[0]
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        return None


def get_due_date(page, service_url):
    try:
        if service_url not in page.url:
            page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        close_cookie_consent(page)
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})",
        ]
        for pattern in patterns:
            m = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if m:
                due = m.group(1).strip()
                log(f"📅 获取到Due Date: {due}")
                return due
        date_hits = re.findall(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", body_text)
        log(f"🔍 页面中所有日期样式文本: {date_hits[:10]}")
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"


def get_unpaid_invoice_urls(page):
    log(f"🔍 访问未付发票列表: {UNPAID_INVOICES_URL}")
    try:
        page.goto(UNPAID_INVOICES_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"⚠️ 导航未付发票页失败: {e}")
        return []

    handle_cloudflare(page)
    close_cookie_consent(page)
    time.sleep(3)

    log(f"📝 未付发票页 Title: {page.title()}, URL: {page.url}")
    try:
        page.screenshot(path="unpaid_invoices.png", full_page=True)
        with open("unpaid_invoices.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存未付发票页")
    except Exception:
        pass

    urls = []
    try:
        rows = page.locator('tr, div').filter(has_text=re.compile(r'\bUnpaid\b', re.I))
        n = rows.count()
        log(f"🔍 含 Unpaid 的行数: {n}")
        for i in range(min(n, 20)):
            try:
                row = rows.nth(i)
                link = row.locator('a[href*="/payment/invoice/"]').first
                if link.count() > 0:
                    href = link.get_attribute("href")
                    if href:
                        if not href.startswith("http"):
                            href = BASE_URL + href
                        if href not in urls:
                            urls.append(href)
            except Exception:
                continue
    except Exception as e:
        log(f"⚠️ 定位 Unpaid 行失败: {e}")

    if not urls:
        html = page.content()
        found = re.findall(r'href="(/payment/invoice/[a-f0-9\-]{20,})"', html)
        for u in found:
            full = BASE_URL + u
            if full not in urls:
                urls.append(full)

    log(f"🔍 未付发票共 {len(urls)} 个")
    for i, u in enumerate(urls[:5]):
        log(f"   [{i}] {u}")
    return urls


def has_real_pay_button(page):
    try:
        pay_btn = page.locator('form[action*="/payment/invoice/"][action$="/pay"] button[type="submit"]')
        if pay_btn.count() > 0:
            return True
        alt = page.locator('button.bg-green-700:has-text("Pay")')
        if alt.count() > 0:
            return True
    except Exception:
        pass
    return False


def try_pay_invoice(page, invoice_url):
    log(f"🔗 访问: {invoice_url}")
    try:
        if page.url != invoice_url:
            page.goto(invoice_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"⚠️ 导航失败: {e}")
        return False

    handle_cloudflare(page)
    close_cookie_consent(page)
    time.sleep(2)

    title = page.title()
    log(f"📝 页面 Title: {title}, URL: {page.url}")

    # 跳转到发票页后立即截图
    try:
        page.screenshot(path="invoice_redirected.png", full_page=True)
        with open("invoice_redirected.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存：跳转后的发票页截图")
    except Exception as e:
        log(f"⚠️ 跳转后的发票页截图失败: {e}")

    if not has_real_pay_button(page):
        log("⚠️ 该页没有真 Pay 按钮，跳过")
        return False

    log("✅ 该页有真 Pay 按钮，准备点击")

    try:
        page.screenshot(path="invoice_page.png", full_page=True)
        with open("invoice_page.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存发票页")
    except Exception:
        pass

    selectors = [
        'form[action*="/payment/invoice/"][action$="/pay"] button[type="submit"]',
        'button.bg-green-700:has-text("Pay")',
    ]

    for sel in selectors:
        try:
            btns = page.locator(sel)
            if btns.count() == 0:
                continue
            for i in range(btns.count()):
                btn = btns.nth(i)
                try:
                    text = btn.inner_text().strip()
                except Exception:
                    text = ""
                log(f"   - 候选按钮 [{i}] text={text!r}")

                if text == "Pay" or re.match(r"^Pay\s*[€$£]?\d", text):
                    log(f"✅ 锁定: {text!r}")
                    if not mouse_click_element(page, btn, f"Pay({text})"):
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            try:
                                btn.evaluate("el => el.click()")
                            except Exception as e:
                                log(f"❌ 点击失败: {e}")
                                continue

                    log(f"⏳ 已点击 Pay，等待 {WAIT_AFTER_PAY} 秒...")
                    time.sleep(WAIT_AFTER_PAY)

                    try:
                        page.screenshot(path="after_pay.png", full_page=True)
                        with open("after_pay.html", "w", encoding="utf-8") as f:
                            f.write(page.content())
                    except Exception:
                        pass

                    log(f"🔍 Pay 后 URL: {page.url}")
                    try:
                        body_text = page.locator("body").inner_text().lower()
                        for kw in ["success", "paid", "thank", "complete", "completed"]:
                            if kw in body_text:
                                log(f"✅ 页面出现成功提示: {kw}")
                                break
                    except Exception:
                        pass
                    return True
        except Exception as e:
            log(f"⚠️ 处理 {sel} 出错: {e}")

    log("⚠️ 未找到可点击的 Pay 按钮")
    return None


def renew_service(page, service_url):
    log("➡ 进入续期流程...")
    close_cookie_consent(page)

    if page.url != service_url:
        page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
    close_cookie_consent(page)
    handle_cloudflare(page)

    log("🖱️ 准备点击 'Renew'...")
    renew_btn = page.locator('button:has-text("Renew")').first
    create_btn = page.locator('button[type="submit"]:has-text("Create Invoice")').first
    if create_btn.count() == 0:
        create_btn = page.locator('button:has-text("Create Invoice")').first

    modal_opened = False
    for i in range(3):
        try:
            renew_btn.wait_for(state="visible", timeout=10000)
            renew_btn.scroll_into_view_if_needed()
            log(f"🖱️ 第 {i+1} 次点击 'Renew'...")
            close_cookie_consent(page)
            renew_btn.click()

            time.sleep(2)
            body_lower = page.locator("body").inner_text().lower()
            for kw in ["renewal restricted", "can only renew", "not yet time", "too early", "renewal window"]:
                if kw in body_lower:
                    log("⚠️ 未到续期时间")
                    return "NOT_TIME"

            log("🖲️ 等待弹窗...")
            try:
                create_btn.wait_for(state="visible", timeout=5000)
                modal_opened = True
                log("✅ 弹窗已弹出！")
                break
            except Exception:
                log("⚠️ 弹窗未出现，重试...")
                time.sleep(2)
        except Exception as e:
            log(f"❌ 点击出错: {e}")

    if not modal_opened:
        log("❌ 弹窗未出现")
        return False

    handle_cloudflare(page)
    close_cookie_consent(page)

    log(f"⏳ 等待 {WAIT_RENDER_BEFORE_CF} 秒让弹窗渲染...")
    time.sleep(WAIT_RENDER_BEFORE_CF)

    # 关闭 Cookie 横幅（可能会在弹窗出现后再次渲染）
    close_cookie_consent(page)
    time.sleep(0.5)

    try:
        page.screenshot(path="before_create_invoice.png", full_page=True)
        with open("before_create_invoice.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass

    log("🔒 处理 CF Turnstile...")
    if not solve_turnstile_checkbox(page, timeout=CF_TURNSTILE_TIMEOUT):
        log("❌ CF Turnstile 验证失败，无法创建发票")
        try:
            page.screenshot(path="cf_failed.png", full_page=True)
        except Exception:
            pass
        return False

    log(f"🔍 点击 Create Invoice 前 URL: {page.url}")

    log("🖱️ 物理点击 'Create Invoice'...")
    clicked = False
    if mouse_click_element(page, create_btn, "Create Invoice"):
        clicked = True
    else:
        try:
            create_btn.click(timeout=5000)
            clicked = True
        except Exception:
            try:
                create_btn.evaluate("el => el.click()")
                clicked = True
            except Exception as e:
                log(f"❌ 点击 Create Invoice 失败: {e}")
                return False

    # 点击 Create Invoice 后立即截图
    try:
        time.sleep(1)
        page.screenshot(path="after_create_invoice_click_immediate.png", full_page=True)
        with open("after_create_invoice_click_immediate.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存：点击 Create Invoice 后立即截图")
    except Exception as e:
        log(f"⚠️ 点击 Create Invoice 后立即截图失败: {e}")

    # 检查是否出现 CF 错误
    time.sleep(3)
    try:
        body_text = page.locator("body").inner_text()
        if "cf-turnstile-response" in body_text or "field is required" in body_text:
            log("❌ 检测到错误：cf-turnstile-response field is required，CF 验证未通过或未提交")
            try:
                page.screenshot(path="cf_error_after_click.png", full_page=True)
            except Exception:
                pass
            return False
    except Exception:
        pass

    # 短轮询
    log(f"⏳ 短轮询 {NAV_POLL_SECONDS} 秒，看是否自动跳转...")
    auto_url = None
    for i in range(NAV_POLL_SECONDS):
        cur = page.url
        if any(k in cur for k in INVOICE_URL_KEYWORDS):
            log(f"🎉 自动跳转到发票页: {cur}")
            auto_url = cur
            break
        for p in page.context.pages:
            if any(k in p.url for k in INVOICE_URL_KEYWORDS):
                log(f"🎉 新标签页发票: {p.url}")
                auto_url = p.url
                page = p
                break
        if auto_url:
            break
        time.sleep(1)

    # 跳转后截图
    try:
        page.screenshot(path="after_create_invoice_redirect.png", full_page=True)
        with open("after_create_invoice_redirect.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log(f"📸 已保存：跳转后截图，当前 URL={page.url}")
    except Exception as e:
        log(f"⚠️ 跳转后截图失败: {e}")

    # === 支付处理 ===
    paid_ok = False

    if auto_url:
        log("🚀 自动跳转到发票页，直接处理")
        result = try_pay_invoice(page, auto_url)
        if result is True:
            paid_ok = True

    if not paid_ok:
        log("⏳ 等待 5 秒让后端生成发票...")
        time.sleep(5)
        unpaid_urls = get_unpaid_invoice_urls(page)

        if not unpaid_urls:
            log("❌ 未付发票列表为空")
            return False

        for idx, url in enumerate(unpaid_urls):
            log(f"🔎 尝试第 {idx+1}/{len(unpaid_urls)} 个未付发票")
            result = try_pay_invoice(page, url)
            if result is True:
                log(f"✅ 第 {idx+1} 个支付成功")
                paid_ok = True
                break
            elif result is False:
                log(f"⚠️ 第 {idx+1} 个不是真发票，继续")
                continue
            else:
                log(f"⚠️ 第 {idx+1} 个是真发票但点击失败，继续")
                continue

    if not paid_ok:
        log("❌ 所有未付发票都尝试失败")
        return False

    log("🔍 返回服务页确认状态...")
    time.sleep(3)
    try:
        page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        close_cookie_consent(page)
    except Exception as e:
        log(f"⚠️ 返回服务页失败: {e}")

    return True


def process_account(identifier, email, password, cookie_value, browser):
    log(f"=== 开始处理账号: {mask_email(email) or identifier} ===")
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        proxy={"server": PROXY_SERVER} if IS_PROXY else None,
        locale='en-US',
        timezone_id='Europe/Berlin',
    )
    page = context.new_page()
    page.add_init_script(STEALTH_JS)

    status, old_due, new_due = "❌ 未知错误", "未知", "未知"
    try:
        if not login(page, email, password, cookie_value):
            status = "❌ 登录失败"
            return (status, old_due, new_due)

        close_cookie_consent(page)
        server_id = get_server_id(page)
        if not server_id:
            status = "❌ 获取Server ID失败"
            return (status, old_due, new_due)
        service_url = f"{BASE_URL}/service/{server_id}/manage"

        old_due = get_due_date(page, service_url)
        log(f"📆 续费前到期时间：{old_due}")

        renew_result = renew_service(page, service_url)

        if renew_result == "NOT_TIME":
            status = "⏳ 未到续期时间"
        elif renew_result is False:
            status = "❌ 续期失败"
        else:
            new_due = get_due_date(page, service_url)
            log(f"📆 续费前到期时间：{old_due}")
            log(f"📆 续费后到期时间：{new_due}")
            if new_due != "未知" and new_due == old_due:
                status = "⚠️ 续期后到期时间未变化"
            else:
                status = "✅ 续期成功"
        log(f"🏁 最终状态: {status}")
        return (status, old_due, new_due)

    except Exception as e:
        log(f"❌ 异常: {e}")
        status = f"❌ 异常: {e}"
        return (status, old_due, new_due)

    finally:
        try:
            send_telegram_notification(status, old_due, new_due, email or identifier)
        except Exception as e:
            log(f"⚠️ 通知失败: {e}")
        try:
            context.close()
        except Exception:
            pass


def main():
    if ACCOUNTS_JSON:
        try:
            accounts = json.loads(ACCOUNTS_JSON)
            if not isinstance(accounts, list) or len(accounts) == 0:
                log("❌ ACCOUNTS_JSON 格式错误")
                sys.exit(1)
        except json.JSONDecodeError as e:
            log(f"❌ ACCOUNTS_JSON 解析失败: {e}")
            sys.exit(1)
        log(f"📋 多账号模式，共 {len(accounts)} 个账号")
    else:
        if not COOKIE_VALUE and not (EMAIL and PASSWORD):
            log("❌ 缺少登录凭证")
            sys.exit(1)
        accounts = [{"email": EMAIL, "password": PASSWORD, "cookie": COOKIE_VALUE}]
        log("📋 单账号模式")

    current_ip = get_current_ip(PROXY_SERVER if IS_PROXY else None)
    log(f"🎯 当前出口IP: {current_ip}")

    with sync_playwright() as p:
        browser = None
        try:
            log("🚀 启动浏览器...")
            browser = p.chromium.launch(
                headless=False,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--disable-dev-shm-usage',
                    '--window-size=1920,1080',
                ]
            )

            all_success = True
            total = len(accounts)
            for idx, acc in enumerate(accounts):
                email = acc.get('email', '')
                password = acc.get('password', '')
                cookie = acc.get('cookie', '')
                identifier = email or f"账号{idx+1}"

                if not cookie and not (email and password):
                    log(f"⚠️ 第 {idx+1} 个账号缺少凭证，跳过")
                    continue

                status, old_due, new_due = process_account(identifier, email, password, cookie, browser)

                if status not in ("✅ 续期成功", "⏳ 未到续期时间"):
                    all_success = False

                if idx < total - 1:
                    log("⏳ 等待 3 分钟...")
                    time.sleep(180)

            if all_success:
                log("🎉 所有账号处理完毕")
                sys.exit(0)
            else:
                log("⚠️ 部分账号处理失败")
                sys.exit(1)

        except Exception as e:
            log(f"❌ 出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
