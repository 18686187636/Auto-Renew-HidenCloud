#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, random, requests, json
from playwright.sync_api import sync_playwright

COOKIE_VALUE  = os.environ.get('COOKIE_VALUE') or ""
EMAIL         = os.environ.get('EMAIL') or ""
PASSWORD      = os.environ.get('PASSWORD') or ""
TG_BOT_TOKEN  = os.environ.get('TG_BOT_TOKEN') or ""
TG_CHAT_ID    = os.environ.get('TG_CHAT_ID') or ""
ACCOUNTS_JSON = os.environ.get('ACCOUNTS_JSON') or ""

BASE_URL  = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

IS_PROXY     = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER = os.environ.get('PROXY_SERVER') or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

INVOICE_URL_KEYWORDS = ("/payment/invoice/",)
RENEW_URL_KEYWORDS   = ("/renew",)
UNPAID_INVOICES_URL  = f"{BASE_URL}/invoices?where=unpaid"

WAIT_RENDER_BEFORE_CF   = 20
CF_CLICK_TIMEOUT        = 60    # 点 checkbox 最长尝试时间
NAV_POLL_SECONDS        = 30
WAIT_AFTER_PAY          = 15
WAIT_AFTER_RENEW_RESP   = 20
RENEW_VERIFY_ATTEMPTS   = 3
RENEW_VERIFY_INTERVAL   = 10
ACCOUNT_INTERVAL_SEC    = 90

CF_IFRAME_SEL = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[title*="cloudflare"], '
    'iframe[title*="widget"], '
    'iframe[title*="challenge"], '
    '.cf-turnstile iframe'
)
CF_DIV_SEL = '.cf-turnstile, div[class*="cf-turnstile"], div[id*="cf-chl-widget"]'

RENEWAL_WINDOW_HINTS = [
    "renewal window", "renewal is not available", "not available yet",
    "not yet time", "too early", "can only renew", "renewal restricted",
    "available from", "renewal opens",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""


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


def safe_filename(email, fallback=""):
    base = email or fallback or "account"
    return re.sub(r'[^a-zA-Z0-9]', '_', base)[:60]


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


# ---------- 点击工具 ----------

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


def click_with_fallback(page, locator, label=""):
    try:
        locator.scroll_into_view_if_needed()
        locator.click(timeout=5000)
        log(f"✅ 原生点击 {label} 成功")
        return True
    except Exception as e:
        log(f"⚠️ 原生点击 {label} 失败: {e}")
    try:
        locator.evaluate("el => el.click()")
        log(f"✅ JS 点击 {label} 成功")
        return True
    except Exception as e:
        log(f"⚠️ JS 点击 {label} 失败: {e}")
    if mouse_click_element(page, locator, label):
        log(f"✅ 物理点击 {label} 成功")
        return True
    return False


# ---------- Accept 按钮 ----------

def click_accept_if_present(page):
    selectors = [
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        'button:has-text("I Accept")',
        'button:has-text("I agree")',
        'button:has-text("Agree")',
        'button:has-text("Allow")',
        'button:has-text("同意")',
        'button:has-text("接受")',
        '[role="dialog"] button:has-text("Accept")',
        '.fc-cta-consent',
        '.fc-button-label',
    ]
    for sel in selectors:
        try:
            btns = page.locator(sel)
            n = btns.count()
            for i in range(n):
                btn = btns.nth(i)
                try:
                    if not btn.is_visible() or not btn.is_enabled():
                        continue
                    text = (btn.inner_text() or "").strip()
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=3000)
                    log(f"✅ 点击 Accept 按钮: {sel} text={text!r}")
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


# ---------- Cloudflare 核心 ----------

def _turnstile_token_ready(page):
    """严格检查 CF token 是否生成。只认 token，不认容器状态。"""
    try:
        result = page.evaluate("""() => {
            // 1) 标准 input/textarea
            const sels = [
                'input[name="cf-turnstile-response"]',
                'input[id^="cf-chl-widget-"][id$="_response"]',
                'textarea[name="cf-turnstile-response"]',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.value && el.value.length > 20) {
                    return {ok: true, src: s, val: el.value.slice(0, 40)};
                }
            }
            // 2) .cf-turnstile 上的 data-response 属性
            const widget = document.querySelector('.cf-turnstile');
            if (widget) {
                const r = widget.getAttribute('data-response');
                if (r && r.length > 20) {
                    return {ok: true, src: 'data-response', val: r.slice(0, 40)};
                }
            }
            // 3) 全局回调标记
            if (window.__cf_turnstile_done) {
                return {ok: true, src: 'global-flag', val: ''};
            }
            return {ok: false};
        }""")
        if result and result.get("ok"):
            return result
        return None
    except Exception:
        return None


def _get_cf_widget_info(page):
    """打印 CF widget / iframe 的实际位置，方便调试。"""
    try:
        return page.evaluate("""() => {
            const out = {};
            const widget = document.querySelector('.cf-turnstile');
            if (widget) {
                const r = widget.getBoundingClientRect();
                out.widget = {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
            }
            const iframes = document.querySelectorAll('.cf-turnstile iframe, iframe[src*="challenges.cloudflare.com"], iframe[title*="widget"], iframe[title*="challenge"]');
            out.iframes = [];
            for (const f of iframes) {
                const r = f.getBoundingClientRect();
                out.iframes.push({
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                    src: (f.src || '').slice(0, 100),
                    title: f.title || '',
                    id: f.id || '',
                });
            }
            return out;
        }""")
    except Exception as e:
        log(f"⚠️ 获取 CF widget info 失败: {e}")
        return None


def _try_frame_locator_click(page):
    """方案1：用 frame_locator 进入 CF iframe 点击。"""
    frame_sels = [
        'iframe[src*="challenges.cloudflare.com"]',
        '.cf-turnstile iframe',
        'iframe[title*="widget"]',
        'iframe[title*="challenge"]',
    ]
    for frame_sel in frame_sels:
        try:
            fl = page.frame_locator(frame_sel)
            # 先试内部 checkbox
            for inner_sel in ['input[type="checkbox"]', '[role="checkbox"]', 'label']:
                try:
                    loc = fl.locator(inner_sel).first
                    if loc.count() > 0:
                        loc.click(timeout=2000, force=True)
                        log(f"✅ frame_locator 点击: {frame_sel} >> {inner_sel}")
                        return True
                except Exception:
                    pass
            # 再试直接点 body 的左侧 30,30
            try:
                body = fl.locator('body').first
                if body.count() > 0:
                    body.click(timeout=2000, position={"x": 30, "y": 30}, force=True)
                    log(f"✅ frame_locator 点击 body(30,30): {frame_sel}")
                    return True
            except Exception:
                pass
        except Exception as e:
            log(f"⚠️ frame_locator {frame_sel} 异常: {e}")
    return False


def _try_iframe_coord_click(page):
    """方案2：定位 CF iframe，点它的左侧 30px。"""
    try:
        loc = page.locator(CF_IFRAME_SEL).first
        if loc.count() == 0:
            return False
        box = loc.bounding_box()
        if not box:
            return False
        # CF checkbox 通常在 iframe 左侧约 30px、垂直居中
        x = box["x"] + min(30, box["width"] * 0.3)
        y = box["y"] + box["height"] / 2
        log(f"🖱️ 点 iframe 左侧 ({x:.0f}, {y:.0f})  box=({box['x']:.0f},{box['y']:.0f},{box['width']:.0f}x{box['height']:.0f})")
        # 更自然的轨迹
        page.mouse.move(x - random.uniform(80, 120), y - random.uniform(40, 60))
        time.sleep(random.uniform(0.2, 0.4))
        page.mouse.move(x - random.uniform(20, 40), y - random.uniform(10, 20))
        time.sleep(random.uniform(0.1, 0.2))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.12))
        page.mouse.up()
        return True
    except Exception as e:
        log(f"⚠️ iframe 坐标点击失败: {e}")
        return False


def _try_cdp_click(page):
    """方案3：CDP Input.dispatchMouseEvent，事件更接近真实。"""
    try:
        loc = page.locator(CF_IFRAME_SEL).first
        if loc.count() == 0:
            return False
        box = loc.bounding_box()
        if not box:
            return False
        x = box["x"] + min(30, box["width"] * 0.3)
        y = box["y"] + box["height"] / 2
        client = page.context.new_cdp_session(page)
        # 移动
        client.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x - 60, "y": y - 30, "button": "none"
        })
        time.sleep(0.15)
        client.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "none"
        })
        time.sleep(0.1)
        # 按下
        client.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1, "buttons": 1
        })
        time.sleep(0.08)
        # 抬起
        client.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1, "buttons": 0
        })
        log(f"✅ CDP 点击 ({x:.0f}, {y:.0f})")
        return True
    except Exception as e:
        log(f"⚠️ CDP 点击失败: {e}")
        return False


def try_click_cf_checkbox(page):
    """按优先级尝试三种点击方式。"""
    # 方案1
    if _try_frame_locator_click(page):
        return True
    # 方案2
    if _try_iframe_coord_click(page):
        return True
    # 方案3
    if _try_cdp_click(page):
        return True
    return False


def solve_cf_and_wait_token(page, timeout=CF_CLICK_TIMEOUT, tag=""):
    """
    循环尝试点击 CF checkbox，直到 token 生成或超时。
    只认 token 生成，不认容器消失。
    """
    log(f"🔒 开始解决 CF（最长 {timeout}s），要求必须拿到 token")

    info = _get_cf_widget_info(page)
    if info:
        log(f"🔍 CF widget: {info.get('widget')}")
        for i, f in enumerate(info.get("iframes", [])[:5]):
            log(f"   iframe[{i}] {f}")

    if tag:
        try:
            page.screenshot(path=f"cf_widget_{tag}.png", full_page=False)
            log("📸 已保存 CF widget 截图")
        except Exception:
            pass

    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        # 先检查是否已有 token
        tok = _turnstile_token_ready(page)
        if tok:
            log(f"✅ Turnstile token 已生成: src={tok.get('src')} val={tok.get('val')}")
            return True

        attempt += 1
        log(f"🔁 第 {attempt} 次尝试点击 CF checkbox (已用 {int(time.time()-start)}s)")

        try_click_cf_checkbox(page)

        # 等几秒再检查
        for _ in range(6):
            time.sleep(1)
            tok = _turnstile_token_ready(page)
            if tok:
                log(f"✅ Turnstile token 已生成: src={tok.get('src')} val={tok.get('val')}")
                return True

    log("❌ CF token 未生成，超时")
    return False


def handle_cloudflare(page, tag=""):
    has_div = False
    has_iframe = False
    try:
        has_div = page.locator(CF_DIV_SEL).count() > 0
    except Exception:
        pass
    try:
        has_iframe = page.locator(CF_IFRAME_SEL).count() > 0
    except Exception:
        pass

    if not has_div and not has_iframe:
        return True

    log("⚠️ 页面检测到 Cloudflare 验证...")
    return solve_cf_and_wait_token(page, timeout=CF_CLICK_TIMEOUT, tag=tag)


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


# ---------- 登录 ----------

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


def detect_renewal_window_msg(page):
    try:
        body_lower = page.locator("body").inner_text().lower()
    except Exception:
        return None
    for kw in RENEWAL_WINDOW_HINTS:
        if kw in body_lower:
            return kw
    return None


# ---------- 未付发票提取 ----------

def get_unpaid_invoice_urls(page, tag="acc"):
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
        page.screenshot(path=f"unpaid_invoices_{tag}.png", full_page=True)
        with open(f"unpaid_invoices_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存未付发票页")
    except Exception:
        pass

    urls = []

    try:
        rows = page.locator('tr').filter(
            has=page.locator('a[href*="/payment/invoice/"]')
        )
        n = rows.count()
        log(f"🔍 含发票链接的 tr 行数: {n}")
        for i in range(n):
            try:
                link = rows.nth(i).locator('a[href*="/payment/invoice/"]').first
                href = link.get_attribute("href")
                if href:
                    if not href.startswith("http"):
                        href = BASE_URL + href
                    if href not in urls:
                        urls.append(href)
            except Exception:
                continue
    except Exception as e:
        log(f"⚠️ 定位发票行失败: {e}")

    if not urls:
        html = page.content()
        found = re.findall(
            r'''href=["'](/payment/invoice/[A-Za-z0-9\-_]{8,})["']''',
            html
        )
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
        if page.locator('form[action*="/payment/invoice/"][action$="/pay"] button[type="submit"]').count() > 0:
            return True
        btns = page.locator('button')
        for i in range(min(btns.count(), 30)):
            try:
                t = (btns.nth(i).inner_text() or "").lower()
                if "pay" in t and "paypal" not in t:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def try_pay_invoice(page, invoice_url, tag="acc"):
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
        log("⚠️ 该页没有 Pay 按钮，跳过")
        return False

    log("✅ 该页有 Pay 按钮，准备点击")

    try:
        page.screenshot(path=f"invoice_page_{tag}.png", full_page=True)
        with open(f"invoice_page_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass

    candidates = []
    try:
        cand1 = page.locator('form[action*="/payment/invoice/"][action$="/pay"] button[type="submit"]')
        for i in range(cand1.count()):
            candidates.append(cand1.nth(i))
    except Exception:
        pass

    if not candidates:
        try:
            btns = page.locator('button')
            for i in range(min(btns.count(), 30)):
                try:
                    t = (btns.nth(i).inner_text() or "").lower()
                    if "pay" in t and "paypal" not in t:
                        candidates.append(btns.nth(i))
                except Exception:
                    continue
        except Exception:
            pass

    if not candidates:
        log("⚠️ 未找到可点击的 Pay 按钮")
        return None

    btn = candidates[0]
    try:
        text = btn.inner_text().strip()
    except Exception:
        text = "Pay"

    log(f"🎯 锁定 Pay 按钮: {text!r}")
    if not click_with_fallback(page, btn, f"Pay({text})"):
        log("❌ Pay 按钮点击失败")
        return None

    log(f"⏳ 已点击 Pay，等待 {WAIT_AFTER_PAY} 秒...")
    time.sleep(WAIT_AFTER_PAY)

    try:
        page.screenshot(path=f"after_pay_{tag}.png", full_page=True)
        with open(f"after_pay_{tag}.html", "w", encoding="utf-8") as f:
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


# ---------- 续期主流程 ----------

def renew_service(page, service_url, tag="acc"):
    log("➡ 进入续期流程...")
    close_cookie_consent(page)

    if page.url != service_url:
        page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
    close_cookie_consent(page)
    handle_cloudflare(page, tag=tag)

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
            for kw in RENEWAL_WINDOW_HINTS:
                if kw in body_lower:
                    log(f"⚠️ 未到续期时间（命中: {kw}）")
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
            log(f"❌ 点击 Renew 出错: {e}")

    if not modal_opened:
        log("❌ 弹窗未出现")
        return False

    log(f"⏳ 等待 {WAIT_RENDER_BEFORE_CF} 秒让弹窗渲染...")
    time.sleep(WAIT_RENDER_BEFORE_CF)

    log("🔍 查找 Accept 按钮...")
    if click_accept_if_present(page):
        time.sleep(2)

    # === 关键：CF 必须解决，token 必须拿到，否则不点 Create Invoice ===
    log("🔒 处理弹窗内 CF（必须拿到 token）...")
    if not solve_cf_and_wait_token(page, timeout=CF_CLICK_TIMEOUT, tag=f"modal_{tag}"):
        log("❌ 未拿到 CF token，放弃本次续期")
        return False

    log("✅ CF token 已确认，准备提交 Create Invoice")
    log(f"🔍 点击前 URL: {page.url}")

    try:
        page.screenshot(path=f"before_create_invoice_{tag}.png", full_page=True)
        page.screenshot(path=f"before_click_viewport_{tag}.png", full_page=False)
        with open(f"before_create_invoice_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存点击前截图")
    except Exception:
        pass

    # === 监听网络 ===
    net_log = []
    def _on_response(resp):
        try:
            u = resp.url
            if "/payment/invoice" in u or "/renew" in u or "/invoice" in u:
                net_log.append((resp.status, resp.request.method, u))
        except Exception:
            pass
    try:
        page.on("response", _on_response)
    except Exception:
        pass

    log("🖱️ 点击 'Create Invoice'...")
    if not click_with_fallback(page, create_btn, "Create Invoice"):
        log("❌ 点击 Create Invoice 失败")
        return False

    # 等请求
    time.sleep(6)

    modal_still_open = False
    try:
        modal_still_open = create_btn.is_visible()
    except Exception:
        modal_still_open = False
    log(f"🔍 点击后弹窗是否仍在: {modal_still_open}")

    for status, method, url in net_log:
        log(f"📡 请求: {method} {status} {url}")

    # 只认发票生成或弹窗关闭为成功
    if not modal_still_open:
        log("✅ 弹窗已关闭，请求已提交")
        log(f"⏳ 等待 {WAIT_AFTER_RENEW_RESP} 秒...")
        time.sleep(WAIT_AFTER_RENEW_RESP)
        return "REQUEST_SUBMITTED"

    # 弹窗仍开 → 检查是否 captcha 错误
    body_lower = ""
    try:
        body_lower = page.locator("body").inner_text().lower()
    except Exception:
        pass

    if "captcha" in body_lower or "verification failed" in body_lower:
        log("❌ 仍然报 captcha 错误，本次续期失败")
        try:
            page.screenshot(path=f"captcha_failed_{tag}.png", full_page=True)
        except Exception:
            pass
        return False

    # 弹窗开着但没 captcha 错误，可能是别的问题
    log("⚠️ 弹窗未关，但也没 captcha 错误，视为失败")
    return False


def verify_renewal(page, service_url, old_due, tag="acc"):
    last_due = old_due
    window_msg = None
    for i in range(RENEW_VERIFY_ATTEMPTS):
        log(f"🔄 第 {i+1}/{RENEW_VERIFY_ATTEMPTS} 次刷新服务页...")
        try:
            page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
            page.reload(wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            close_cookie_consent(page)
        except Exception as e:
            log(f"⚠️ 刷新失败: {e}")

        wm = detect_renewal_window_msg(page)
        if wm:
            window_msg = wm
            log(f"⚠️ 页面出现续费窗口提示: {wm}")

        due = get_due_date(page, service_url)
        if due != "未知":
            last_due = due
        log(f"📆 当前 Due Date: {last_due}")

        if due != "未知" and due != old_due:
            log(f"✅ Due Date 已变化：{old_due} → {due}")
            return (due, window_msg)

        if i < RENEW_VERIFY_ATTEMPTS - 1:
            log(f"⏳ 等 {RENEW_VERIFY_INTERVAL}s 再试...")
            time.sleep(RENEW_VERIFY_INTERVAL)

    return (last_due, window_msg)


# ---------- 单账号处理 ----------

def process_account(identifier, email, password, cookie_value, browser):
    log(f"=== 开始处理账号: {mask_email(email) or identifier} ===")
    tag = safe_filename(email, identifier)

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

        renew_result = renew_service(page, service_url, tag=tag)

        if renew_result == "NOT_TIME":
            status = "⏳ 未到续期时间"
            new_due = old_due
        elif renew_result is False:
            status = "❌ 续期失败"
            new_due = old_due
        elif renew_result == "REQUEST_SUBMITTED":
            new_due, window_msg = verify_renewal(page, service_url, old_due, tag=tag)
            if new_due != old_due:
                status = "✅ 续期成功"
            elif window_msg:
                status = f"⏳ 未到续期时间（{window_msg}）"
            else:
                status = "⚠️ 请求已提交但到期时间未变化"
        else:
            status = "❌ 未知结果"
            new_due = old_due

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


# ---------- main ----------

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

                if not (
                    status.startswith("✅ 续期成功")
                    or status.startswith("⏳ 未到续期时间")
                ):
                    all_success = False

                if idx < total - 1:
                    log(f"⏳ 等待 {ACCOUNT_INTERVAL_SEC} 秒...")
                    time.sleep(ACCOUNT_INTERVAL_SEC)

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
