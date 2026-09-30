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

WAIT_RENDER_BEFORE_CF = 10
CF_TURNSTILE_TIMEOUT  = 90
NAV_POLL_SECONDS      = 30
WAIT_AFTER_PAY        = 15
PAGE_LOAD_TIMEOUT     = 60000

TURNSTILE_IFRAME_SEL = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[title*="cloudflare"], '
    'iframe[src*="turnstile"]'
)

RENEW_BTN_SELECTORS = [
    'button:has-text("Renew")',
    'a:has-text("Renew")',
    'a[href*="renew"]',
    ':is(button, a):has-text("Renew")',
    'button[type="submit"]:has-text("Renew")',
]


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


# ========== 增强版 stealth 脚本 ==========
STEALTH_JS = r"""
// 1. webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. chrome 对象完整模拟
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.app = {
  isInstalled: false,
  InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
  RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
  getDetails: () => null,
  getIsInstalled: () => false,
};
window.chrome.csi = function () { return {}; };
window.chrome.loadTimes = function () { return {}; };

// 3. 语言、插件
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer' },
  ],
});

// 4. 硬件指纹
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

// 5. 权限 API
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(parameters)
  );
}

// 6. WebGL 指纹伪装
const getParameterProto = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (parameter) {
  if (parameter === 37445) return 'Intel Inc.';                       // UNMASKED_VENDOR_WEBGL
  if (parameter === 37446) return 'Intel Iris OpenGL Engine';         // UNMASKED_RENDERER_WEBGL
  return getParameterProto.call(this, parameter);
};
const getParameterProto2 = WebGL2RenderingContext && WebGL2RenderingContext.prototype.getParameter;
if (getParameterProto2) {
  WebGL2RenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameterProto2.call(this, parameter);
  };
}

// 7. Notification.permission 与 permissions.query 一致
if (typeof Notification !== 'undefined') {
  Object.defineProperty(Notification, 'permission', { get: () => 'default' });
}

// 8. 隐藏 CDP 痕迹
['__playwright', '__pw_manual', '__PW_inspect'].forEach(k => { try { delete window[k]; } catch (e) {} });

// 9. iframe contentWindow 上隐藏 webdriver
try {
  Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    get() {
      const win = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get.call(this);
      try { Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
      return win;
    }
  });
} catch (e) {}
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


def save_debug(page, name):
    try:
        page.screenshot(path=f"{name}.png", full_page=True)
    except Exception:
        pass
    try:
        with open(f"{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def human_mouse_warmup(page):
    """在页面中心做几次随机移动，模拟人类使用浏览器"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        w, h = vp["width"], vp["height"]
        for _ in range(3):
            x = random.uniform(w * 0.3, w * 0.7)
            y = random.uniform(h * 0.3, h * 0.7)
            page.mouse.move(x, y, steps=random.randint(10, 20))
            time.sleep(random.uniform(0.15, 0.4))
    except Exception:
        pass


def human_click_at(page, x, y):
    """慢速移动 + 停顿 + 点击"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        # 从随机起点靠近目标
        sx = x - random.uniform(60, 140)
        sy = y - random.uniform(20, 60)
        sx = max(0, min(sx, vp["width"]))
        sy = max(0, min(sy, vp["height"]))
        page.mouse.move(sx, sy, steps=random.randint(6, 12))
        time.sleep(random.uniform(0.15, 0.35))
        # 分两段到达目标，中间停顿
        mid_x = (sx + x) / 2 + random.uniform(-6, 6)
        mid_y = (sy + y) / 2 + random.uniform(-6, 6)
        page.mouse.move(mid_x, mid_y, steps=random.randint(4, 8))
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.move(x, y, steps=random.randint(3, 6))
        time.sleep(random.uniform(0.1, 0.25))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.13))
        page.mouse.up()
        return True
    except Exception:
        return False


def mouse_click_element(page, locator, label=""):
    try:
        box = locator.bounding_box()
        if not box:
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        log(f"🖱️ 物理点击 {label} ({x:.0f}, {y:.0f})")
        return human_click_at(page, x, y)
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
                human_click_at(page, x, y)
                time.sleep(5)
        except Exception:
            time.sleep(1)
    return False


# ========== Token 抓取：覆盖多种可能 ==========
def _get_turnstile_token(page):
    """从多个位置尝试抓取 Turnstile token"""
    try:
        val = page.evaluate(r"""
        () => {
          // 1) 标准 input
          const inp = document.querySelector('input[name="cf-turnstile-response"]');
          if (inp && inp.value && inp.value.length > 10) return inp.value;
          // 2) textarea
          const ta = document.querySelector('textarea[name="cf-turnstile-response"]');
          if (ta && ta.value && ta.value.length > 10) return ta.value;
          // 3) 任意 data-response
          const el = document.querySelector('[data-response]');
          if (el) {
            const v = el.getAttribute('data-response');
            if (v && v.length > 10) return v;
          }
          // 4) 任意 input 名字含 turnstile
          const all = document.querySelectorAll('input, textarea');
          for (const e of all) {
            if (e.name && e.name.toLowerCase().includes('turnstile')) {
              if (e.value && e.value.length > 10) return e.value;
            }
          }
          return '';
        }
        """)
        return val or ""
    except Exception:
        return ""


def _dump_turnstile_debug(page, tag):
    try:
        token = _get_turnstile_token(page)
        log(f"    [debug-{tag}] token_len={len(token)}")
    except Exception:
        pass
    try:
        for i, frame in enumerate(page.frames):
            furl = frame.url or ""
            if "cloudflare" in furl or "turnstile" in furl:
                log(f"    [debug-{tag}] frame[{i}] url={furl[:120]}")
                try:
                    html = frame.content()
                    log(f"    [debug-{tag}] frame[{i}] html_len={len(html)}")
                    with open(f"cf_frame_{tag}_{i}.html", "w", encoding="utf-8") as f:
                        f.write(html)
                except Exception as e:
                    log(f"    [debug-{tag}] frame[{i}] content 失败: {e}")
    except Exception:
        pass
    try:
        cnt = page.locator(TURNSTILE_IFRAME_SEL).count()
        log(f"    [debug-{tag}] iframe 数量: {cnt}")
        for i in range(cnt):
            try:
                box = page.locator(TURNSTILE_IFRAME_SEL).nth(i).bounding_box()
                log(f"    [debug-{tag}] iframe[{i}] box={box}")
            except Exception:
                pass
    except Exception:
        pass
    save_debug(page, f"cf_fail_{tag}")


def solve_turnstile_checkbox(page, timeout=90):
    """
    慢速人类交互 + 三重策略 + 多点位 + 耐心等待。
    """
    log(f"🔒 开始处理 Turnstile (超时 {timeout}s)")

    # 先做一次人类鼠标预热
    human_mouse_warmup(page)
    time.sleep(1)

    start = time.time()
    attempt = 0
    coords_list = [(28, 28), (30, 32), (25, 30), (32, 30), (30, 25), (35, 35)]

    while time.time() - start < timeout:
        attempt += 1
        token = _get_turnstile_token(page)
        if token and len(token) > 10:
            log(f"✅ Turnstile token 已填充 (len={len(token)}, attempt={attempt})")
            return True

        close_cookie_consent(page)

        iframe_count = page.locator(TURNSTILE_IFRAME_SEL).count()
        if iframe_count == 0:
            # iframe 消失后多等几秒，让 token 写入
            for _ in range(5):
                time.sleep(1)
                token = _get_turnstile_token(page)
                if token and len(token) > 10:
                    log(f"✅ Turnstile token 已填充 (iframe 消失后)")
                    return True
            continue

        log(f"🔍 attempt={attempt}: 检测到 {iframe_count} 个 CF iframe")

        # ===== 1) frame_locator 内部点击 =====
        try:
            fl = page.frame_locator(TURNSTILE_IFRAME_SEL).first
            for coord in coords_list:
                try:
                    fl.locator('body').first.click(
                        position={"x": coord[0], "y": coord[1]},
                        timeout=3000, force=True, no_wait_after=True,
                    )
                    log(f"  ✅ 策略1 frame_locator {coord}")
                except Exception:
                    pass
                # 每次点击后等 2.5 秒，让 CF 有反应
                time.sleep(2.5)
                token = _get_turnstile_token(page)
                if token and len(token) > 10:
                    log(f"✅ Turnstile token 已填充 (len={len(token)})")
                    return True
        except Exception as e:
            log(f"  策略1 异常: {e}")

        # ===== 2) frame 内部 DOM 点击 =====
        try:
            for frame in page.frames:
                furl = frame.url or ""
                if "challenges.cloudflare.com" not in furl:
                    continue
                for sel in ['input[type="checkbox"]', '[role="checkbox"]', 'label', 'div.cb-lb']:
                    try:
                        el = frame.locator(sel).first
                        if el.count() == 0:
                            continue
                        el.click(timeout=2500, force=True, no_wait_after=True)
                        log(f"  ✅ 策略2 frame 内点击 {sel}")
                        time.sleep(2.5)
                        token = _get_turnstile_token(page)
                        if token and len(token) > 10:
                            log(f"✅ Turnstile token 已填充 (len={len(token)})")
                            return True
                    except Exception:
                        continue
                for coord in coords_list[:4]:
                    try:
                        frame.locator('body').click(
                            position={"x": coord[0], "y": coord[1]},
                            timeout=2500, force=True, no_wait_after=True,
                        )
                        log(f"  ✅ 策略2 frame body {coord}")
                        time.sleep(2.5)
                        token = _get_turnstile_token(page)
                        if token and len(token) > 10:
                            log(f"✅ Turnstile token 已填充 (len={len(token)})")
                            return True
                    except Exception:
                        continue
        except Exception as e:
            log(f"  策略2 异常: {e}")

        # ===== 3) 外层鼠标：人类曲线移动 =====
        try:
            box = page.locator(TURNSTILE_IFRAME_SEL).first.bounding_box()
            if box:
                for rx, ry in [(0.05, 0.5), (0.08, 0.5), (0.06, 0.6), (0.04, 0.4)]:
                    x = box["x"] + box["width"] * rx
                    y = box["y"] + box["height"] * ry
                    log(f"  🖱️ 策略3 人类点击 ({x:.0f},{y:.0f})")
                    human_click_at(page, x, y)
                    time.sleep(3)
                    token = _get_turnstile_token(page)
                    if token and len(token) > 10:
                        log(f"✅ Turnstile token 已填充 (len={len(token)})")
                        return True
        except Exception as e:
            log(f"  策略3 异常: {e}")

        time.sleep(1.5)

    log("❌ CF Turnstile 验证超时")
    _dump_turnstile_debug(page, f"fail_{int(time.time())}")
    return False


def close_cookie_consent(page):
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


def goto_and_settle(page, url, timeout=PAGE_LOAD_TIMEOUT):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as e:
        log(f"⚠️ goto {url} 失败: {e}")
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    handle_cloudflare(page)
    close_cookie_consent(page)
    time.sleep(1)
    return True


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
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
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
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        handle_cloudflare(page)
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        time.sleep(0.5)
        handle_cloudflare(page)
        page.click('button[type="submit"]')
        time.sleep(3)
        handle_cloudflare(page)
        page.wait_for_url(f"{BASE_URL}/*", timeout=30000)
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
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


DATE_PATTERN = re.compile(
    r"Due date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL
)


def get_due_date(page, service_url, retries=2):
    for attempt in range(retries + 1):
        try:
            if attempt > 0:
                log(f"🔄 第 {attempt+1} 次尝试获取 Due Date")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                except Exception:
                    pass
            else:
                if service_url not in page.url:
                    goto_and_settle(page, service_url)
                else:
                    handle_cloudflare(page)
                    close_cookie_consent(page)

            for _ in range(20):
                try:
                    body_text = page.locator("body").inner_text()
                except Exception:
                    body_text = ""
                if "Due date" in body_text or "Renew" in body_text:
                    break
                time.sleep(1)

            m = DATE_PATTERN.search(body_text)
            if m:
                due = m.group(1).strip()
                log(f"📅 获取到Due Date: {due}")
                return due

            hits = re.findall(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", body_text)
            log(f"🔍 第 {attempt+1} 次：日期样式文本 {hits[:8]}，页面长度 {len(body_text)}")
        except Exception as e:
            log(f"❌ 获取Due Date 第 {attempt+1} 次失败: {e}")

    log("⚠️ Due Date 多次尝试后仍未获取，保存 debug 快照")
    save_debug(page, "service_page_due_fail")
    return "未知"


def get_unpaid_invoice_urls(page):
    log(f"🔍 访问未付发票列表: {UNPAID_INVOICES_URL}")
    try:
        page.goto(UNPAID_INVOICES_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
    except Exception as e:
        log(f"⚠️ 导航未付发票页失败: {e}")
        return []

    handle_cloudflare(page)
    close_cookie_consent(page)
    time.sleep(3)

    log(f"📝 未付发票页 Title: {page.title()}, URL: {page.url}")
    save_debug(page, "unpaid_invoices")

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
            page.goto(invoice_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
    except Exception as e:
        log(f"⚠️ 导航失败: {e}")
        return False

    handle_cloudflare(page)
    close_cookie_consent(page)
    time.sleep(2)

    log(f"📝 页面 Title: {page.title()}, URL: {page.url}")
    save_debug(page, "invoice_redirected")

    if not has_real_pay_button(page):
        log("⚠️ 该页没有真 Pay 按钮，跳过")
        return False

    log("✅ 该页有真 Pay 按钮，准备点击")
    save_debug(page, "invoice_page")

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

                    save_debug(page, "after_pay")
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


def find_renew_button(page, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in RENEW_BTN_SELECTORS:
            try:
                loc = page.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
                for i in range(cnt):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            return el, sel
                    except Exception:
                        continue
            except Exception:
                continue
        time.sleep(1)
    return None, None


def try_open_renew_modal(page, create_btn, service_url):
    """尝试打开 Renew 弹窗，返回 True/False/'NOT_TIME'"""
    renew_btn, used_sel = find_renew_button(page, timeout=30)
    if renew_btn is None:
        log("⚠️ 未找到 Renew 按钮，尝试 reload 一次")
        save_debug(page, "renew_not_found")
        try:
            page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            handle_cloudflare(page)
            close_cookie_consent(page)
            time.sleep(3)
        except Exception:
            pass
        renew_btn, used_sel = find_renew_button(page, timeout=20)

    if renew_btn is None:
        log("❌ reload 后仍未找到 Renew 按钮")
        save_debug(page, "renew_not_found_after_reload")
        return False

    log(f"✅ 找到 Renew 按钮，使用选择器: {used_sel}")

    for i in range(3):
        try:
            renew_btn.scroll_into_view_if_needed()
            log(f"🖱️ 第 {i+1} 次点击 'Renew'...")
            close_cookie_consent(page)
            try:
                renew_btn.click(timeout=8000)
            except Exception:
                renew_btn, used_sel = find_renew_button(page, timeout=10)
                if renew_btn is None:
                    log("❌ 重试时未找到 Renew 按钮")
                    break
                renew_btn.click(timeout=8000)

            time.sleep(2)
            body_lower = page.locator("body").inner_text().lower()
            for kw in ["renewal restricted", "can only renew", "not yet time",
                       "too early", "renewal window"]:
                if kw in body_lower:
                    log("⚠️ 未到续期时间")
                    return "NOT_TIME"

            log("🖲️ 等待弹窗...")
            try:
                create_btn.wait_for(state="visible", timeout=5000)
                log("✅ 弹窗已弹出！")
                return True
            except Exception:
                log("⚠️ 弹窗未出现，重试...")
                time.sleep(2)
        except Exception as e:
            log(f"❌ 点击出错: {e}")

    log("❌ 弹窗未出现")
    save_debug(page, "modal_not_opened")
    return False


def renew_service(page, service_url):
    log("➡ 进入续期流程...")
    close_cookie_consent(page)

    if service_url not in page.url:
        log(f"🔄 重新导航到服务页: {service_url}")
        goto_and_settle(page, service_url)
    else:
        handle_cloudflare(page)
        close_cookie_consent(page)

    create_btn = page.locator('button[type="submit"]:has-text("Create Invoice")').first
    if create_btn.count() == 0:
        create_btn = page.locator('button:has-text("Create Invoice")').first

    # 最多 2 轮尝试（Turnstile 失败时，关闭弹窗重开）
    for round_idx in range(2):
        if round_idx > 0:
            log(f"🔁 第 {round_idx+1} 轮：刷新服务页后重新打开 Renew 弹窗")
            try:
                page.goto(service_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                handle_cloudflare(page)
                close_cookie_consent(page)
                time.sleep(3)
            except Exception as e:
                log(f"⚠️ 刷新服务页失败: {e}")
                return False

        log("🖱️ 准备点击 'Renew'...")
        open_result = try_open_renew_modal(page, create_btn, service_url)
        if open_result == "NOT_TIME":
            return "NOT_TIME"
        if open_result is False:
            return False

        handle_cloudflare(page)
        close_cookie_consent(page)

        log(f"⏳ 等待 {WAIT_RENDER_BEFORE_CF} 秒让弹窗渲染...")
        time.sleep(WAIT_RENDER_BEFORE_CF)

        close_cookie_consent(page)
        time.sleep(0.5)

        save_debug(page, "before_create_invoice")

        log("🔒 处理 CF Turnstile...")
        if not solve_turnstile_checkbox(page, timeout=CF_TURNSTILE_TIMEOUT):
            log(f"❌ 第 {round_idx+1} 轮 CF Turnstile 验证失败")
            save_debug(page, f"cf_failed_round{round_idx+1}")
            # 关闭弹窗（按 Escape）后重试
            try:
                page.keyboard.press("Escape")
                time.sleep(1)
            except Exception:
                pass
            continue

        # Turnstile 通过，点击 Create Invoice
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
                    continue

        try:
            time.sleep(1)
            save_debug(page, "after_create_invoice_click_immediate")
        except Exception:
            pass

        time.sleep(3)
        try:
            body_text = page.locator("body").inner_text()
            if "cf-turnstile-response" in body_text or "field is required" in body_text:
                log("❌ 检测到错误：cf-turnstile-response field is required")
                save_debug(page, "cf_error_after_click")
                # 关闭弹窗重试
                try:
                    page.keyboard.press("Escape")
                    time.sleep(1)
                except Exception:
                    pass
                continue
        except Exception:
            pass

        # 进入支付流程
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

        save_debug(page, "after_create_invoice_redirect")

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
            page.goto(service_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            handle_cloudflare(page)
            close_cookie_consent(page)
        except Exception as e:
            log(f"⚠️ 返回服务页失败: {e}")

        return True

    log("❌ 两轮 Turnstile 均失败")
    return False


def process_account(identifier, email, password, cookie_value, browser):
    log(f"=== 开始处理账号: {mask_email(email) or identifier} ===")
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        proxy={"server": PROXY_SERVER} if IS_PROXY else None,
        locale='en-US',
        timezone_id='Europe/Berlin',
        extra_http_headers={
            'Accept-Language': 'en-US,en;q=0.9',
        },
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
            launch_kwargs = dict(
                headless=False,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--disable-dev-shm-usage',
                    '--window-size=1920,1080',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--lang=en-US',
                ]
            )
            # 优先使用真 Chrome channel（若已安装），否则用默认 Chromium
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kwargs)
                log("🌐 使用真实 Chrome channel")
            except Exception as e:
                log(f"⚠️ channel=chrome 启动失败，回退到默认 Chromium: {e}")
                browser = p.chromium.launch(**launch_kwargs)

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
