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

INVOICE_URL_KEYWORDS = ("/payment/invoice/", "/invoice/", "/invoices/", "/billing/invoice")

WAIT_RENDER_BEFORE_TURNSTILE = 5
WAIT_AFTER_CREATE_CLICK      = 30
FALLBACK_POLL_SECONDS        = 60
CF_TURNSTILE_TIMEOUT         = 30
CF_RETRY_WAIT                = 15
FALLBACK_WAIT_SECONDS        = 20


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


def handle_cloudflare(page):
    iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
    if page.locator(iframe_selector).count() == 0:
        return True
    log("⚠️ 检测到 Cloudflare 验证...")
    start_time = time.time()
    while time.time() - start_time < 60:
        if page.locator(iframe_selector).count() == 0:
            log("✅ Cloudflare 验证通过！")
            return True
        try:
            frame = page.frame_locator(iframe_selector)
            checkbox = frame.locator('input[type="checkbox"]')
            if checkbox.is_visible():
                log("🖱️ 点击验证复选框...")
                time.sleep(random.uniform(0.5, 1.5))
                checkbox.click()
                log("⏳ 已点击，等待验证结果...")
                time.sleep(5)
            else:
                time.sleep(1)
        except Exception:
            pass
    log("❌ 验证超时。")
    return False


def solve_turnstile_checkbox(page, timeout=30):
    """
    处理嵌套在弹窗 iframe 内的 Cloudflare Turnstile。
    多级递归查找，用 JS 派发鼠标事件更可靠。
    """
    start = time.time()
    checkbox_found = False

    js_click_turnstile = """
    () => {
        const results = { found: false, clicked: false, path: [] };

        function tryClickInDoc(doc, depth, path) {
            if (depth > 5) return false;
            const sels = [
                'input[type="checkbox"]',
                'label input[type="checkbox"]',
                '[role="checkbox"]',
                'span.mark',
                'div.ctp-checkbox-label',
            ];
            for (const sel of sels) {
                const el = doc.querySelector(sel);
                if (el) {
                    results.found = true;
                    results.path = path.concat([sel]);
                    const rect = el.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    const opts = {
                        bubbles: true, cancelable: true, view: doc.defaultView,
                        clientX: x, clientY: y, button: 0, buttons: 1
                    };
                    try { el.dispatchEvent(new MouseEvent('mouseover', opts)); } catch (e) {}
                    try { el.dispatchEvent(new MouseEvent('mousemove', opts)); } catch (e) {}
                    try { el.dispatchEvent(new MouseEvent('mousedown', opts)); } catch (e) {}
                    try { el.dispatchEvent(new MouseEvent('mouseup', opts)); } catch (e) {}
                    try { el.click(); } catch (e) {}
                    try { if (el.parentElement) el.parentElement.click(); } catch (e) {}
                    results.clicked = true;
                    return true;
                }
            }
            const iframes = doc.querySelectorAll('iframe');
            for (let i = 0; i < iframes.length; i++) {
                try {
                    const innerDoc = iframes[i].contentDocument ||
                                     iframes[i].contentWindow.document;
                    if (innerDoc) {
                        if (tryClickInDoc(innerDoc, depth + 1, path.concat(['iframe[' + i + ']']))) {
                            return true;
                        }
                    }
                } catch (e) {}
            }
            return false;
        }

        tryClickInDoc(document, 0, []);
        return results;
    }
    """

    while time.time() - start < timeout:
        cf_frames = []
        for f in page.frames:
            u = (f.url or "").lower()
            if "cloudflare" in u or "turnstile" in u:
                cf_frames.append(f)
        log(f"🔍 frame 总数: {len(page.frames)}, 含 cloudflare/turnstile: {len(cf_frames)}")

        # 方式 1：JS 递归遍历
        try:
            result = page.evaluate(js_click_turnstile)
            if result and result.get("clicked"):
                checkbox_found = True
                log(f"🖱️ JS 递归点击成功，路径: {result.get('path')}")
                log("⏳ 等待 6 秒看验证结果...")
                time.sleep(6)
                still_cf = any(
                    "cloudflare" in (f.url or "").lower() or "turnstile" in (f.url or "").lower()
                    for f in page.frames
                )
                if not still_cf:
                    log("✅ Turnstile 验证通过！")
                    return True
                log("⚠️ 点击后 CF frame 仍在，继续尝试...")
            elif result and result.get("found"):
                checkbox_found = True
                log(f"🔍 JS 找到了 checkbox 但点击可能无效，路径: {result.get('path')}")
        except Exception as e:
            log(f"⚠️ JS 递归点击失败: {e}")

        # 方式 2：frame_locator
        for f in cf_frames:
            try:
                for sel in ['input[type="checkbox"]', '[role="checkbox"]', 'label']:
                    cb = f.locator(sel)
                    if cb.count() > 0:
                        try:
                            if cb.first.is_visible(timeout=1000):
                                checkbox_found = True
                                log(f"🖱️ frame_locator 点击 {sel}")
                                cb.first.click(force=True, timeout=3000)
                                time.sleep(6)
                                still_cf = any(
                                    "cloudflare" in (fr.url or "").lower() or "turnstile" in (fr.url or "").lower()
                                    for fr in page.frames
                                )
                                if not still_cf:
                                    log("✅ Turnstile 验证通过！")
                                    return True
                        except Exception as e:
                            log(f"⚠️ 点击 {sel} 失败: {e}")
            except Exception as e:
                log(f"⚠️ 处理 frame 失败: {e}")

        # 方式 3：物理坐标点击
        try:
            iframe_els = page.locator('iframe')
            n = iframe_els.count()
            for i in range(n):
                try:
                    src = iframe_els.nth(i).get_attribute("src") or ""
                    if "cloudflare" in src.lower() or "turnstile" in src.lower():
                        box = iframe_els.nth(i).bounding_box()
                        if box:
                            x = box["x"] + 30
                            y = box["y"] + box["height"] / 2
                            log(f"🖱️ 物理点击 iframe[{i}] 坐标 ({x:.0f}, {y:.0f})")
                            page.mouse.move(x - 10, y - 10)
                            time.sleep(0.3)
                            page.mouse.move(x, y)
                            time.sleep(0.2)
                            page.mouse.click(x, y)
                            time.sleep(6)
                            still_cf = any(
                                "cloudflare" in (fr.url or "").lower() or "turnstile" in (fr.url or "").lower()
                                for fr in page.frames
                            )
                            if not still_cf:
                                log("✅ Turnstile 验证通过！")
                                return True
                except Exception as e:
                    log(f"⚠️ 物理点击 iframe[{i}] 失败: {e}")
        except Exception as e:
            log(f"⚠️ 枚举 iframe 失败: {e}")

        time.sleep(2)

    if checkbox_found:
        log("❌ 已找到 checkbox 但验证未通过")
    else:
        log("⚠️ 所有 frame 中均未找到 checkbox")
    return False


def close_cookie_consent(page):
    try:
        consent_root = page.locator('.fc-consent-root')
        if consent_root.count() == 0:
            return
        log("🍪 检测到 Cookie 同意弹窗，尝试关闭...")
        accept_selectors = [
            'button:has-text("Accept")',
            'button:has-text("Accept All")',
            'button:has-text("I agree")',
            'button:has-text("Allow")',
            'button:has-text("OK")',
            '.fc-cta-consent',
            '.fc-button:has-text("Accept")',
        ]
        for selector in accept_selectors:
            try:
                btn = page.locator(selector).first
                btn.wait_for(state="visible", timeout=1000)
                btn.click()
                log("✅ 点击了接受按钮")
                try:
                    page.wait_for_selector('.fc-consent-root', state='detached', timeout=5000)
                except Exception:
                    pass
                return
            except Exception:
                continue
        page.evaluate("""
            document.querySelectorAll('.fc-consent-root, .fc-dialog-overlay, .fc-header').forEach(el => el.remove());
        """)
        log("⚠️ 未找到接受按钮，已通过 JS 移除覆盖层")
    except Exception as e:
        log(f"⚠️ 关闭 Cookie 弹窗时出错: {e}")


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
            page_title = page.title()
            log(f"📝 当前Title: {page_title}")
            if "auth/login" not in page.url:
                log("✅ Cookie 登录成功！当前已到达dashboard页面")
                return True
            log("❌ Cookie 失效，请更换")
        except Exception as e:
            log(f"⚠️ Cookie 登录异常: {e}")

    if not email or not password:
        log("⚠️ 无可用账号密码，跳过密码登录")
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
        page_title = page.title()
        log(f"📝 当前Title: {page_title}")
        if "auth/login" in page.url:
            log("❌ 登录失败。")
            return False
        log("✅ 账号密码登录成功！当前已到达dashboard页面")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        page.screenshot(path="login_fail.png")
        return False


def get_server_id(page):
    try:
        handle_cloudflare(page)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")
        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从链接中获取到 Server ID: {server_id}")
            return server_id
        matches = re.findall(r'#(\d{4,})', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从文本 #号中获取到 Server ID: {server_id}")
            return server_id
        log("❌ 所有 URL 均未找到 Server ID")
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        page.screenshot(path="server_id_error.png")
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
            r"Due date\s*[:\-]?\s*([A-Za-z]{3}\s+\d{1,2},?\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                due_date = match.group(1).strip()
                log(f"📅 获取到Due Date: {due_date}")
                return due_date
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"


def renew_service(page, service_url):
    log("➡ 进入续期流程...")
    close_cookie_consent(page)

    if page.url != service_url:
        page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
    close_cookie_consent(page)
    handle_cloudflare(page)

    log("🖱️ 准备点击 'Renew' 按钮...")
    renew_btn = page.locator('button:has-text("Renew")').first
    create_btn = page.locator('button:has-text("Create Invoice")').first

    modal_opened = False
    for i in range(3):
        try:
            renew_btn.wait_for(state="visible", timeout=10000)
            renew_btn.scroll_into_view_if_needed()
            log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
            close_cookie_consent(page)
            renew_btn.click()

            time.sleep(2)
            page_text = page.locator("body").inner_text()
            if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                log("⚠️ 未到续期时间，无法续期。")
                page.screenshot(path="renew_not_allowed.png")
                return "NOT_TIME"

            log("🖲️ 等待弹窗出现...")
            try:
                create_btn.wait_for(state="visible", timeout=5000)
                modal_opened = True
                log("✅ 弹窗已成功弹出！")
                break
            except Exception:
                log("⚠️ 弹窗未出现，可能是点击未响应，准备重试...")
                time.sleep(2)
        except Exception as e:
            log(f"❌ 点击尝试出错: {e}")

    if not modal_opened:
        log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
        page.screenshot(path="renew_modal_failed.png")
        return False

    handle_cloudflare(page)
    close_cookie_consent(page)

    log(f"⏳ 弹窗已弹出，等待 {WAIT_RENDER_BEFORE_TURNSTILE} 秒让页面渲染...")
    time.sleep(WAIT_RENDER_BEFORE_TURNSTILE)

    log("🔒 尝试处理 Cloudflare Turnstile 验证（递归遍历嵌套 frame）...")
    turnstile_ok = solve_turnstile_checkbox(page, timeout=CF_TURNSTILE_TIMEOUT)

    if not turnstile_ok:
        log(f"⚠️ Turnstile 第一次未通过，等待 {CF_RETRY_WAIT} 秒后重试...")
        time.sleep(CF_RETRY_WAIT)
        turnstile_ok = solve_turnstile_checkbox(page, timeout=CF_TURNSTILE_TIMEOUT)

    if not turnstile_ok:
        log(f"⚠️ 无法主动通过 CF 验证，回退到等待 {FALLBACK_WAIT_SECONDS} 秒...")
        time.sleep(FALLBACK_WAIT_SECONDS)

    try:
        disabled = create_btn.is_disabled()
        log(f"🔍 Create Invoice disabled = {disabled}")
    except Exception as e:
        log(f"⚠️ 检查按钮状态失败: {e}")

    try:
        unchecked = page.locator('input[type="checkbox"]:not(:checked)')
        n = unchecked.count()
        for i in range(n):
            try:
                unchecked.nth(i).check(force=True)
                log(f"✅ 勾选第 {i+1} 个 checkbox")
                time.sleep(0.3)
            except Exception as e:
                log(f"⚠️ 勾选第 {i+1} 个 checkbox 失败: {e}")
    except Exception as e:
        log(f"⚠️ 处理 checkbox 出错: {e}")

    for _ in range(20):
        try:
            if not create_btn.is_disabled():
                break
        except Exception:
            break
        time.sleep(0.5)

    try:
        page.screenshot(path="before_create_invoice.png", full_page=True)
        with open("before_create_invoice.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存点击前截图和 HTML")
    except Exception as e:
        log(f"⚠️ 保存点击前现场失败: {e}")

    try:
        btns = page.locator('button:visible')
        n = btns.count()
        btn_texts = []
        for i in range(min(n, 30)):
            try:
                t = btns.nth(i).inner_text().strip()
                if t:
                    btn_texts.append(t)
            except Exception:
                pass
        log(f"🔍 当前可见按钮: {btn_texts}")
    except Exception as e:
        log(f"⚠️ 枚举按钮失败: {e}")

    network_log = []
    def on_response(resp):
        try:
            if resp.request.method in ("POST", "PUT", "PATCH"):
                line = f"[{resp.status}] {resp.request.method} {resp.url}"
                network_log.append(line)
                log(f"🌐 {line}")
        except Exception:
            pass
    page.on("response", on_response)

    pages_before = len(page.context.pages)
    log(f"🔍 点击前 URL: {page.url}")

    log("🖱️ 点击 'Create Invoice'...")
    try:
        create_btn.scroll_into_view_if_needed()
    except Exception:
        pass

    clicked = False
    try:
        create_btn.click(timeout=5000)
        clicked = True
    except Exception as e:
        log(f"⚠️ 普通点击失败，尝试 JS 点击: {e}")
        try:
            create_btn.evaluate("el => el.click()")
            clicked = True
        except Exception as e2:
            log(f"❌ JS 点击也失败: {e2}")

    if not clicked:
        page.screenshot(path="create_invoice_click_failed.png")
        return False

    log(f"⏳ 已点击 Create Invoice，等待 {WAIT_AFTER_CREATE_CLICK} 秒让后端处理...")
    time.sleep(WAIT_AFTER_CREATE_CLICK)

    try:
        page.screenshot(path="after_create_invoice_click.png", full_page=True)
        with open("after_create_invoice_click.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("📸 已保存点击后截图和 HTML")
    except Exception as e:
        log(f"⚠️ 保存现场失败: {e}")

    pages_after = len(page.context.pages)
    log(f"🔍 等待后页面数: {pages_before} -> {pages_after}")
    new_invoice_url = None

    if pages_after > pages_before:
        for p in page.context.pages:
            if any(k in p.url for k in INVOICE_URL_KEYWORDS):
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                new_invoice_url = p.url
                page = p
                log(f"✅ 在新标签页发现发票: {new_invoice_url}")
                break

    if not new_invoice_url and any(k in page.url for k in INVOICE_URL_KEYWORDS):
        new_invoice_url = page.url
        log(f"🎉 当前页已跳转: {new_invoice_url}")

    if not new_invoice_url:
        log("🔍 检查弹窗内是否直接出现 'Pay Now'...")
        try:
            pay_now = page.locator('button:has-text("Pay Now"), a:has-text("Pay Now")').first
            if pay_now.is_visible(timeout=3000):
                log("✅ 发现 'Pay Now'，直接点击")
                pay_now.click()
                time.sleep(5)
                if any(k in page.url for k in INVOICE_URL_KEYWORDS):
                    new_invoice_url = page.url
                    log(f"🎉 点击 Pay Now 后已跳转: {new_invoice_url}")
        except Exception as e:
            log(f"⚠️ 未找到 Pay Now: {e}")

    if not new_invoice_url:
        log(f"⏳ 未跳转，继续兜底轮询 {FALLBACK_POLL_SECONDS} 秒...")
        start_wait = time.time()
        while time.time() - start_wait < FALLBACK_POLL_SECONDS:
            if any(k in page.url for k in INVOICE_URL_KEYWORDS):
                new_invoice_url = page.url
                log(f"🎉 页面已跳转: {new_invoice_url}")
                break
            for p in page.context.pages:
                if any(k in p.url for k in INVOICE_URL_KEYWORDS):
                    new_invoice_url = p.url
                    page = p
                    log(f"🎉 在其它标签页发现发票: {new_invoice_url}")
                    break
            if new_invoice_url:
                break
            if any("cloudflare" in (f.url or "").lower() or "turnstile" in (f.url or "").lower()
                   for f in page.frames):
                log("⚠️ 检测到 CF，尝试处理...")
                solve_turnstile_checkbox(page, timeout=5)
            time.sleep(1)

    if not new_invoice_url:
        log("❌ 未能进入发票页面，超时。")
        log(f"🌐 本次网络请求记录: {network_log}")
        try:
            page.screenshot(path="renew_stuck_invoice.png", full_page=True)
            with open("renew_stuck_invoice.html", "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass
        return False

    if page.url != new_invoice_url:
        page.goto(new_invoice_url, wait_until="domcontentloaded", timeout=60000)
    handle_cloudflare(page)
    close_cookie_consent(page)

    log("🔎 查找 'Pay' 按钮...")
    try:
        pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        pay_btn.wait_for(state="visible", timeout=30000)
        pay_btn.click()
        log("✅ 'Pay' 按钮已点击。")
    except Exception as e:
        log(f"❌ 点击 'Pay' 失败: {e}")
        page.screenshot(path="pay_btn_failed.png")
        return False

    time.sleep(5)
    page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
    handle_cloudflare(page)
    close_cookie_consent(page)
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
            log("⏳ 未到续期时间，目前无法续期")
            status = "⏳ 未到续期时间"
        elif renew_result is False:
            log("❌ 续费失败")
            status = "❌ 续期失败"
        else:
            new_due = get_due_date(page, service_url)
            log(f"📆 续费后到期时间：{new_due}")
            if new_due != "未知" and new_due == old_due:
                status = "⚠️ 续期后到期时间未变化"
            else:
                status = "✅ 续期成功"
        return (status, old_due, new_due)

    except Exception as e:
        log(f"❌ 处理账号 {mask_email(email) or identifier} 时发生异常: {e}")
        status = f"❌ 异常: {e}"
        return (status, old_due, new_due)

    finally:
        try:
            send_telegram_notification(status, old_due, new_due, email or identifier)
        except Exception as e:
            log(f"⚠️ 发送通知失败: {e}")
        try:
            context.close()
        except Exception:
            pass


def main():
    if ACCOUNTS_JSON:
        try:
            accounts = json.loads(ACCOUNTS_JSON)
            if not isinstance(accounts, list) or len(accounts) == 0:
                log("❌ ACCOUNTS_JSON 格式错误：应为非空数组")
                sys.exit(1)
        except json.JSONDecodeError as e:
            log(f"❌ ACCOUNTS_JSON 解析失败: {e}")
            sys.exit(1)
        log(f"📋 多账号模式，共 {len(accounts)} 个账号")
    else:
        if not COOKIE_VALUE and not (EMAIL and PASSWORD):
            log("❌ 缺少登录凭证，请提供 COOKIE_VALUE 或 EMAIL+PASSWORD")
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
                    log(f"⚠️ 第 {idx+1} 个账号缺少有效凭证，跳过")
                    continue

                status, old_due, new_due = process_account(identifier, email, password, cookie, browser)

                if status not in ("✅ 续期成功", "⏳ 未到续期时间"):
                    all_success = False

                if idx < total - 1:
                    log("⏳ 等待 3 分钟后处理下一个账号...")
                    time.sleep(180)

            if all_success:
                log("🎉 所有账号处理完毕（成功或未到期）")
                sys.exit(0)
            else:
                log("⚠️ 部分账号处理失败，请检查日志")
                sys.exit(1)

        except Exception as e:
            log(f"❌ 浏览器启动或运行出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
