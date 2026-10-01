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
CF_TURNSTILE_TIMEOUT  = 20
NAV_POLL_SECONDS      = 30
WAIT_AFTER_PAY        = 15


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
window.chrome = { runtime: {} };
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
        page.mouse.click(x, y)
        return True
    except Exception as e:
        log(f"⚠️ 物理点击 {label} 失败: {e}")
        return False


def handle_cloudflare(page):
    iframe_sel = 'iframe[src*="challenges.cloudflare.com"], iframe[title*="cloudflare"], iframe[src*="turnstile"]'
    if page.locator(iframe_sel).count() == 0:
        return True
    log("⚠️ 检测到 Cloudflare 验证...")
    start = time.time()
    while time.time() - start < 60:
        if page.locator(iframe_sel).count() == 0:
            log("✅ CF 验证通过！")
            return True
        try:
            box = page.locator(iframe_sel).first.bounding_box()
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


def solve_turnstile_checkbox(page, timeout=20):
    iframe_sel = 'iframe[src*="challenges.cloudflare.com"], iframe[title*="cloudflare"], iframe[src*="turnstile"]'
    start = time.time()
    while time.time() - start < timeout:
        frames = page.locator(iframe_sel)
        n = frames.count()
        if n == 0:
            log("✅ 未检测到 CF iframe")
            return True
        log(f"🔍 检测到 {n} 个 CF iframe")
        for i in range(n):
            try:
                box = frames.nth(i).bounding_box()
                if not box:
                    continue
                x = box["x"] + 30
                y = box["y"] + box["height"] / 2
                log(f"🖱️ 物理点击 CF iframe[{i}] ({x:.0f}, {y:.0f})")
                page.mouse.move(x - 80, y - 40)
                time.sleep(0.3)
                page.mouse.move(x, y)
                time.sleep(0.15)
                page.mouse.click(x, y)
                time.sleep(5)
                if page.locator(iframe_sel).count() == 0:
                    log("✅ CF 验证通过！")
                    return True
            except Exception as e:
                log(f"⚠️ 点击 CF 失败: {e}")
        time.sleep(2)
    return True


def close_cookie_consent(page):
    try:
        if page.locator('.fc-consent-root').count() == 0:
            return
        for sel in [
            'button:has-text("Accept")', 'button:has-text("Accept All")',
            'button:has-text("I agree")', 'button:has-text("Allow")',
            '.fc-cta-consent',
        ]:
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=1000)
                btn.click()
                return
            except Exception:
                continue
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
    """访问 /invoices?where=unpaid，提取所有未付发票 URL（按页面顺序）"""
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

    # 优先从含 "Unpaid" 的行提取链接
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

    # 兜底：从 HTML 里提取所有发票链接
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
    """判断发票页是否有真正的 Pay 按钮"""
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
    """
    访问发票页，尝试点击 Pay。
    返回：
      True  → 支付成功
      False → 不是真发票页（跳过）
      None  → 是真发票页但支付失败
    """
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

    log("🔒 处理 CF Turnstile...")
    solve_turnstile_checkbox(page, timeout=CF_TURNSTILE_TIMEOUT)

    log(f"🔍 点击前 URL: {page.url}")

    try:
        page.screenshot(path="before_create_invoice.png", full_page=True)
        with open("before_create_invoice.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass

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

    try:
        page.screenshot(path="after_create_invoice_click.png", full_page=True)
        with open("after_create_invoice_click.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass

    # === 支付处理 ===
    paid_ok = False

    # 情况 1：自动跳转
    if auto_url:
        log("🚀 自动跳转到发票页，直接处理")
        result = try_pay_invoice(page, auto_url)
        if result is True:
            paid_ok = True

    # 情况 2：去未付发票列表
    if not paid_ok:
        log("⏳ 等待 5 秒让后端生成发票...")
        time.sleep(5)
        unpaid_urls = get_unpaid_invoice_urls(page)

        if not unpaid_urls:
            log("❌ 未付发票列表为空")
            return False

        # 遍历所有未付发票，找到第一个能支付的
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

    # === 回服务页确认 ===
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
        proxy={"server": PROXY_SERVER} if IS_PROXY else None
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
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
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
