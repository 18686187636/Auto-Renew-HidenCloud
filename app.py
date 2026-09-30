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

CF_IFRAME_SEL = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[title*="cloudflare"], '
    'iframe[src*="turnstile"], '
    'iframe[src*="challenge-platform"], '
    'iframe[src*="cf-chl"]'
)

WAIT_RENDER_BEFORE_CF = 5
WAIT_AFTER_PAY        = 15
NAV_POLL_SECONDS      = 20


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


def get_turnstile_token_len(page):
    try:
        return page.evaluate("""
            () => {
                const inputs = document.querySelectorAll('[name="cf-turnstile-response"]');
                if (inputs.length === 0) return -1;
                let maxLen = 0;
                inputs.forEach(i => { if (i.value && i.value.length > maxLen) maxLen = i.value.length; });
                return maxLen;
            }
        """)
    except Exception:
        return -1


def diagnose_turnstile(page):
    """打印所有 Turnstile 容器的状态（是否有 iframe、位置、尺寸）"""
    try:
        diag = page.evaluate("""
            () => {
                const cs = document.querySelectorAll('div.cf-turnstile, [data-sitekey], div[class*="turnstile"]');
                const out = [];
                cs.forEach((c, idx) => {
                    const ifr = c.querySelector('iframe');
                    const r = c.getBoundingClientRect();
                    out.push({
                        idx: idx,
                        hasIframe: !!ifr,
                        src: ifr ? (ifr.src || '').substring(0, 100) : null,
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        visible: r.width > 0 && r.height > 0,
                        display: window.getComputedStyle(c).display
                    });
                });
                return out;
            }
        """)
        log(f"🔍 诊断: {diag}")
        return diag
    except Exception as e:
        log(f"⚠️ 诊断失败: {e}")
        return []


def solve_turnstile_checkbox(page):
    """
    等 Turnstile input 出现 → 等 token 生成 → 未生成则点击容器位置
    """
    log("🔍 等待 Turnstile input 出现（最多 15 秒）...")

    start = time.time()
    has_turnstile = False
    while time.time() - start < 15:
        if get_turnstile_token_len(page) >= 0:
            log("✅ 检测到 Turnstile input")
            has_turnstile = True
            break
        time.sleep(1)

    if not has_turnstile:
        log("⚠️ 未检测到 Turnstile input，视为无 CF")
        return True

    token_len = get_turnstile_token_len(page)
    if token_len > 0:
        log(f"✅ token 已生成（{token_len}）")
        return True

    log("🔒 token 为空，开始点击 Turnstile 容器...")
    click_start = time.time()
    attempt = 0

    while time.time() - click_start < 60:
        token_len = get_turnstile_token_len(page)
        if token_len > 0:
            log(f"✅ token 已生成（{token_len}）")
            return True

        attempt += 1
        clicked = False

        # === 核心：直接点击 cf-turnstile 容器位置 ===
        try:
            container = page.locator('div.cf-turnstile, [data-sitekey], div[class*="turnstile"]').first
            cnt = container.count()
            if cnt > 0:
                box = container.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    # Turnstile checkbox 在容器左侧
                    x = box["x"] + 25
                    y = box["y"] + box["height"] / 2
                    log(f"🖱️ 尝试 {attempt}: 点击容器位置 ({x:.0f}, {y:.0f}) size=({box['width']:.0f}x{box['height']:.0f})")
                    page.mouse.move(x - random.uniform(30, 60), y - random.uniform(15, 30))
                    time.sleep(0.2)
                    page.mouse.move(x, y)
                    time.sleep(0.15)
                    page.mouse.click(x, y)
                    clicked = True
                else:
                    log(f"🖱️ 尝试 {attempt}: 容器无尺寸 (box={box})")
        except Exception as e:
            log(f"⚠️ 点击容器失败: {e}")

        # 备用：如果 iframe 出现了，点 iframe
        if not clicked:
            try:
                iframe_n = page.locator(CF_IFRAME_SEL).count()
                if iframe_n > 0:
                    frames = page.locator(CF_IFRAME_SEL)
                    box = frames.first.bounding_box()
                    if box:
                        x = box["x"] + 25
                        y = box["y"] + box["height"] / 2
                        log(f"🖱️ 尝试 {attempt}: 点击 iframe ({x:.0f}, {y:.0f})")
                        page.mouse.click(x, y)
                        clicked = True
            except Exception:
                pass

        # 每 3 次打印一次诊断
        if attempt % 3 == 0:
            diagnose_turnstile(page)

        time.sleep(3)

    token_len = get_turnstile_token_len(page)
    if token_len > 0:
        log(f"✅ token 已生成（{token_len}）")
        return True
    log("❌ Turnstile 处理超时，token 仍为空")
    diagnose_turnstile(page)
    return False


def handle_cloudflare(page):
    token_len = get_turnstile_token_len(page)
    if token_len < 0:
        # 完全没有 Turnstile input，视为无 CF
        try:
            iframe_n = page.locator(CF_IFRAME_SEL).count()
            container_n = page.locator('div.cf-turnstile, [data-sitekey]').count()
            if iframe_n == 0 and container_n == 0:
                return True
        except Exception:
            return True
    return solve_turnstile_checkbox(page)


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


def click_create_invoice_once(page, create_btn):
    state = {"posted": False, "status": 0, "location": ""}

    def on_response(resp):
        try:
            if resp.request.method == "POST" and "/renew" in resp.url:
                state["posted"] = True
                state["status"] = resp.status
                try:
                    state["location"] = resp.headers.get("location", "")
                except Exception:
                    pass
                log(f"🌐 捕获 POST /renew: {resp.status}  Location: {state['location'] or '(无)'}")
        except Exception:
            pass

    page.on("response", on_response)

    log("🖱️ 点击 Create Invoice（单次）...")
    clicked = mouse_click_element(page, create_btn, "Create Invoice")
    if not clicked:
        try:
            create_btn.click(timeout=5000)
        except Exception:
            try:
                create_btn.evaluate("el => el.click()")
            except Exception as e:
                log(f"❌ 点击失败: {e}")
                return False, 0, ""

    for _ in range(30):
        if state["posted"]:
            break
        time.sleep(0.5)

    return state["posted"], state["status"], state["location"]


def wait_for_invoice_generated(page, timeout=30):
    log(f"⏳ 等待 'Invoice has been generated' 提示（最多 {timeout} 秒）...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            body_text = page.locator("body").inner_text().lower()
            if "invoice has been generated" in body_text or "generated successfully" in body_text:
                log("✅ 页面出现成功提示")
                return True
        except Exception:
            pass
        time.sleep(1)
    log(f"⚠️ {timeout} 秒内未出现成功提示")
    return False


def find_invoice_urls_via_dom(page, url):
    log(f"🔍 访问: {url}")
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"⚠️ 导航失败: {e}")
            return []

    handle_cloudflare(page)
    close_cookie_consent(page)
    time.sleep(3)

    log(f"📝 页面 Title: {page.title()}, URL: {page.url}")

    html = page.content()
    all_links = set()
    for m in re.finditer(r'href="(/payment/invoice/[a-fA-F0-9\-]{20,})"', html):
        all_links.add(BASE_URL + m.group(1))
    for m in re.finditer(r'href="(https?://[^"]*?/payment/invoice/[a-fA-F0-9\-]{20,})"', html):
        all_links.add(m.group(1))
    for m in re.finditer(r'/payment/invoice/([a-fA-F0-9\-]{20,})', html):
        all_links.add(f"{BASE_URL}/payment/invoice/{m.group(1)}")

    urls = list(all_links)
    log(f"🔍 共 {len(urls)} 个发票 URL")
    for i, u in enumerate(urls[:10]):
        log(f"   [{i}] {u}")

    try:
        page.screenshot(path="invoices_page.png", full_page=True)
        with open("invoices_page.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

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

    log(f"📝 页面 Title: {page.title()}, URL: {page.url}")

    if not has_real_pay_button(page):
        log("⚠️ 该页没有真 Pay 按钮，跳过")
        return False

    log("✅ 该页有真 Pay 按钮，准备点击")

    try:
        page.screenshot(path="invoice_page.png", full_page=True)
        with open("invoice_page.html", "w", encoding="utf-8") as f:
            f.write(page.content())
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


def click_renew_and_create(page, service_url, attempt=1):
    log(f"🔄 第 {attempt} 轮: 点击 Renew → Create Invoice")
    try:
        if service_url not in page.url:
            page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        close_cookie_consent(page)
        time.sleep(2)
    except Exception as e:
        log(f"⚠️ 加载服务页失败: {e}")
        return False

    renew_btn = page.locator('button:has-text("Renew")').first
    create_btn = page.locator('button[type="submit"]:has-text("Create Invoice")').first
    if create_btn.count() == 0:
        create_btn = page.locator('button:has-text("Create Invoice")').first

    modal_opened = False
    for i in range(3):
        try:
            renew_btn.wait_for(state="visible", timeout=10000)
            renew_btn.scroll_into_view_if_needed()
            log(f"🖱️ 点击 'Renew'（第 {i+1} 次）")
            if not mouse_click_element(page, renew_btn, "Renew"):
                renew_btn.click()
            time.sleep(2)

            body_lower = page.locator("body").inner_text().lower()
            for kw in ["renewal restricted", "can only renew", "not yet time", "too early", "renewal window"]:
                if kw in body_lower:
                    log("⚠️ 未到续期时间")
                    return "NOT_TIME"

            try:
                create_btn.wait_for(state="visible", timeout=5000)
                modal_opened = True
                log("✅ 弹窗已弹出！")
                break
            except Exception:
                log("⚠️ 弹窗未出现，重试...")
                time.sleep(2)
        except Exception as e:
            log(f"❌ 点击 Renew 出错: {e}")

    if not modal_opened:
        log("❌ 弹窗未出现")
        return False

    close_cookie_consent(page)

    log(f"⏳ 等待 {WAIT_RENDER_BEFORE_CF} 秒让弹窗渲染...")
    time.sleep(WAIT_RENDER_BEFORE_CF)

    # 先做一次诊断
    log("🔍 弹窗内 Turnstile 状态诊断:")
    diagnose_turnstile(page)

    cf_ok = solve_turnstile_checkbox(page)
    if not cf_ok:
        log("❌ CF 未通过，本轮跳过 Create Invoice")
        return False

    try:
        page.screenshot(path=f"before_create_invoice_{attempt}.png", full_page=True)
        with open(f"before_create_invoice_{attempt}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log(f"📸 已保存 before_create_invoice_{attempt}")
    except Exception:
        pass

    posted, status, location = click_create_invoice_once(page, create_btn)
    if not posted:
        log("⚠️ 未捕获 POST /renew（可能未触发）")
        return False

    log(f"✅ Create Invoice POST 已触发（{status}），Location: {location}")
    wait_for_invoice_generated(page, timeout=30)
    return True


def renew_service(page, service_url):
    log("➡ 进入续期流程...")
    paid_ok = False

    for attempt in [1, 2]:
        log(f"==================== 尝试 {attempt}/2 ====================")
        result = click_renew_and_create(page, service_url, attempt=attempt)
        if result == "NOT_TIME":
            return "NOT_TIME"
        if result is False:
            log(f"⚠️ 第 {attempt} 轮失败")
        else:
            log(f"✅ 第 {attempt} 轮 Create Invoice POST 已触发")

        log(f"⏳ 短轮询 {NAV_POLL_SECONDS} 秒...")
        auto_url = None
        for _ in range(NAV_POLL_SECONDS):
            cur = page.url
            if any(k in cur for k in INVOICE_URL_KEYWORDS):
                auto_url = cur
                log(f"🎉 自动跳转: {cur}")
                break
            for p in page.context.pages:
                if any(k in p.url for k in INVOICE_URL_KEYWORDS):
                    auto_url = p.url
                    page = p
                    break
            if auto_url:
                break
            time.sleep(1)

        try:
            page.screenshot(path=f"after_create_invoice_click_{attempt}.png", full_page=True)
            with open(f"after_create_invoice_click_{attempt}.html", "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass

        if auto_url:
            log("🚀 自动跳转到发票页，直接处理")
            if try_pay_invoice(page, auto_url) is True:
                paid_ok = True

        if not paid_ok:
            log("⏳ 等 15 秒让后端生成发票...")
            time.sleep(15)

            log("🔍 方式1: 尝试 /invoices?where=unpaid")
            unpaid_urls = find_invoice_urls_via_dom(page, UNPAID_INVOICES_URL)

            if not unpaid_urls:
                log("🔍 方式2: 尝试 /invoices")
                unpaid_urls = find_invoice_urls_via_dom(page, f"{BASE_URL}/invoices")

            for idx, url in enumerate(unpaid_urls):
                log(f"🔎 尝试第 {idx+1}/{len(unpaid_urls)} 个候选发票")
                r = try_pay_invoice(page, url)
                if r is True:
                    log(f"✅ 第 {idx+1} 个支付成功")
                    paid_ok = True
                    break

        if paid_ok:
            log(f"✅ 第 {attempt} 轮续期成功")
            break
        else:
            log(f"⚠️ 第 {attempt} 轮未成功，准备重试...")
            time.sleep(5)

    if not paid_ok:
        log("❌ 两轮尝试都失败")
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
        proxy={"server": PROXY_SERVER} if IS_PROXY else None
    )
    page = context.new_page()
    page.add_init_script(STEALTH_JS)

    # 监听 CF 相关请求，用于诊断
    def on_request(req):
        u = req.url or ""
        if "challenges.cloudflare.com" in u or "turnstile" in u:
            log(f"📡 [请求] {req.method} {u[:120]}")
    page.on("request", on_request)

    def on_requestfailed(req):
        u = req.url or ""
        if "challenges.cloudflare.com" in u or "turnstile" in u:
            log(f"❌ [请求失败] {req.method} {u[:120]}  原因: {req.failure}")
    page.on("requestfailed", on_requestfailed)

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
