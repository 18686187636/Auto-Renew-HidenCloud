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
CF_TURNSTILE_TIMEOUT         = 15
CF_RETRY_WAIT                = 10


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


def solve_turnstile_checkbox(page, timeout=15):
    """智能处理 CF Turnstile：找到 checkbox 就点，找不到快速跳过"""
    start = time.time()
    no_checkbox_rounds = 0
    MAX_NO_CHECKBOX_ROUNDS = 3

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
        cf_exists = any(
            "cloudflare" in (f.url or "").lower() or "turnstile" in (f.url or "").lower()
            for f in page.frames
        )
        if not cf_exists:
            log("✅ 无 CF frame，视为通过")
            return True

        try:
            result = page.evaluate(js_click_turnstile)
            if result and result.get("clicked"):
                log(f"🖱️ JS 递归点击成功，路径: {result.get('path')}")
                time.sleep(6)
                still_cf = any(
                    "cloudflare" in (f.url or "").lower() or "turnstile" in (f.url or "").lower()
                    for f in page.frames
                )
                if not still_cf:
                    log("✅ Turnstile 验证通过！")
                    return True
                log("⚠️ 点击后 CF frame 仍在")
                no_checkbox_rounds = 0
            else:
                no_checkbox_rounds += 1
                if no_checkbox_rounds == 1:
                    log(f"🔍 第 {no_checkbox_rounds} 轮未找到 checkbox...")
                if no_checkbox_rounds >= MAX_NO_CHECKBOX_ROUNDS:
                    log(f"✅ 连续 {MAX_NO_CHECKBOX_ROUNDS} 轮未找到 checkbox，判定为自动挑战模式，跳过")
                    return True
        except Exception as e:
            log(f"⚠️ JS 递归失败: {e}")
            no_checkbox_rounds += 1
            if no_checkbox_rounds >= MAX_NO_CHECKBOX_ROUNDS:
                return True

        for f in page.frames:
            u = (f.url or "").lower()
            if "cloudflare" not in u and "turnstile" not in u:
                continue
            try:
                for sel in ['input[type="checkbox"]', '[role="checkbox"]']:
                    cb = f.locator(sel)
                    if cb.count() > 0:
                        try:
                            if cb.first.is_visible(timeout=800):
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
                                no_checkbox_rounds = 0
                        except Exception:
                            pass
            except Exception:
                pass

        time.sleep(2)

    log("⚠️ Turnstile 处理超时，继续后续流程")
    return True


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


def js_click(locator, page, label=""):
    """JS 强制点击，绕过 viewport 限制"""
    try:
        locator.evaluate("el => { el.scrollIntoView({block:'center'}); el.click(); }")
        return True
    except Exception as e:
        log(f"⚠️ JS 点击 {label} 失败: {e}")
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=5000, force=True)
            return True
        except Exception as e2:
            log(f"⚠️ force 点击 {label} 也失败: {e2}")
            return False


def click_submit_button(page, locator, label=""):
    """点击 type=submit 按钮：优先 form.requestSubmit"""
    try:
        ok = locator.evaluate("""
            el => {
                const form = el.form || el.closest('form');
                if (form && form.requestSubmit) {
                    form.requestSubmit(el);
                    return true;
                }
                return false;
            }
        """)
        if ok:
            log(f"✅ 通过 form.requestSubmit 提交 {label}")
            return True
    except Exception as e:
        log(f"⚠️ requestSubmit 失败: {e}")

    try:
        locator.scroll_into_view_if_needed(timeout=5000)
        locator.click(timeout=5000)
        log(f"✅ Playwright click {label}")
        return True
    except Exception as e:
        log(f"⚠️ Playwright click 失败: {e}")

    try:
        locator.evaluate("el => el.click()")
        log(f"✅ JS click {label}")
        return True
    except Exception as e:
        log(f"❌ JS click 也失败: {e}")
        return False


def renew_service(page, service_url):
    log("➡ 进入续期流程...")
    close_cookie_consent(page)

    if page.url != service_url:
        page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
    close_cookie_consent(page)
    handle_cloudflare(page)

    log("🖱️ 准备点击 'Renew' 按钮...")
    renew_btn = page.locator('button:has-text("Renew")').first
    create_btn = page.locator('button[type="submit"]:has-text("Create Invoice")').first
    if create_btn.count() == 0:
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

    log("🔒 智能检测 Cloudflare Turnstile...")
    solve_turnstile_checkbox(page, timeout=CF_TURNSTILE_TIMEOUT)

    cf_exists = any(
        "cloudflare" in (f.url or "").lower() or "turnstile" in (f.url or "").lower()
        for f in page.frames
    )
    if cf_exists:
        log(f"⚠️ CF 仍在，再给 {CF_RETRY_WAIT} 秒...")
        time.sleep(CF_RETRY_WAIT)
        solve_turnstile_checkbox(page, timeout=10)

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

    # === 监听关键请求 ===
    state = {"paid": False, "renewed": False}

    def on_response(resp):
        try:
            url = resp.url or ""
            method = resp.request.method
            if "hidencloud" not in url and "challenges.cloudflare" not in url:
                return
            line = f"[{resp.status}] {method} {url}"
            if method in ("POST", "PUT", "PATCH"):
                log(f"🌐 {line}")
                if "/balance/add" in url and resp.status in (200, 302, 303):
                    state["paid"] = True
                if "/renew" in url and resp.status in (200, 302, 303):
                    state["renewed"] = True
        except Exception:
            pass

    page.on("response", on_response)

    pages_before = len(page.context.pages)
    log(f"🔍 点击前 URL: {page.url}")

    log("🖱️ 点击 'Create Invoice'（type=submit）...")
    if not click_submit_button(page, create_btn, "Create Invoice"):
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

    log(f"🔍 状态: renewed={state['renewed']}, paid={state['paid']}")

    # === 检查是否进入发票页 ===
    new_invoice_url = None
    pages_after = len(page.context.pages)
    if pages_after > pages_before:
        for p in page.context.pages:
            if any(k in p.url for k in INVOICE_URL_KEYWORDS):
                new_invoice_url = p.url
                page = p
                log(f"✅ 新标签页发票: {new_invoice_url}")
                break

    if not new_invoice_url and any(k in page.url for k in INVOICE_URL_KEYWORDS):
        new_invoice_url = page.url
        log(f"🎉 当前页跳转: {new_invoice_url}")

    # === 核心：点击 Pay Now + 监听 /balance/add ===
    if not new_invoice_url and not state["paid"]:
        log("🔍 检查弹窗内是否出现 'Pay Now'...")
        try:
            pay_now = page.locator('button:has-text("Pay Now"), a:has-text("Pay Now")').first
            cnt = pay_now.count()
            log(f"🔍 Pay Now 按钮数量: {cnt}")
            if cnt > 0:
                log("✅ 发现 'Pay Now'，用 JS 强制点击")
                js_click(pay_now, page, "Pay Now")

                log("⏳ 等待支付请求 /balance/add（最多 30 秒）...")
                for _ in range(30):
                    if state["paid"]:
                        log("✅ 检测到 /balance/add，支付完成！")
                        break
                    if any(k in page.url for k in INVOICE_URL_KEYWORDS):
                        new_invoice_url = page.url
                        log(f"🎉 页面跳转: {new_invoice_url}")
                        break
                    time.sleep(1)

                if not state["paid"] and not new_invoice_url:
                    log("⚠️ 30 秒内未检测到支付响应")
        except Exception as e:
            log(f"⚠️ 处理 Pay Now 失败: {e}")

    # === 如果进入了发票页，点 Pay ===
    if new_invoice_url:
        if page.url != new_invoice_url:
            page.goto(new_invoice_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        close_cookie_consent(page)

        log("🔎 查找 'Pay' 按钮...")
        try:
            pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
            pay_btn.wait_for(state="visible", timeout=30000)
            js_click(pay_btn, page, "Pay")
            log("✅ 'Pay' 按钮已点击")
            time.sleep(5)
        except Exception as e:
            log(f"⚠️ 点击 'Pay' 失败: {e}")

    # === 最后：等页面稳定后重新加载服务页 ===
    if state["paid"] or state["renewed"] or new_invoice_url:
        log("⏳ 支付/续期已触发，等待 5 秒让后端处理...")
        time.sleep(5)

    log("🔍 返回服务页确认状态...")
    try:
        page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        close_cookie_consent(page)
    except Exception as e:
        log(f"⚠️ 返回服务页失败: {e}")

    if state["paid"] or state["renewed"] or new_invoice_url:
        return True

    log("❌ 未能完成续期流程")
    return False


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
