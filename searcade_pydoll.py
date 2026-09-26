import asyncio
import os
import re
import logging
import random
import base64
import json
from pathlib import Path
from datetime import datetime
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions
import ddddocr
from PIL import Image, ImageFilter, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ========== 配置 ==========
EMAIL = os.environ["SEARCADE_EMAIL"]
PASSWORD = os.environ["SEARCADE_PASSWORD"]
BASE_URL = "https://searcade.com/en/"
LOGIN_URL = "https://searcade.com/en/"
USERVERIA_AUTH_URL = "https://userveria.com/authorize/"
REDIRECT_URI = "https://searcade.com/accounts/userveria/login/callback/"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

def wxpush(content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        return
    import urllib.request
    payload = json.dumps({
        "appToken": WXPUSHER_TOKEN,
        "content": content,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info("📨 WxPusher 推送成功")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

ocr = ddddocr.DdddOcr(beta=True, show_ad=False)

# ========== 邮箱变形生成 ==========

def get_email_variants(email: str) -> list[str]:
    """生成邮箱所有可能的显示变形（按长度倒序，避免子串污染）"""
    if not email:
        return []
    username = email.split("@")[0] if "@" in email else email
    domain   = email.split("@")[1] if "@" in email else ""

    variants = [
        email,                                              
        email.replace("@", "at").replace(".", "-"),         
        email.replace("@", "_at_").replace(".", "_"),       
        email.replace("@", "[at]").replace(".", "[dot]"),
        email.replace("@", " at ").replace(".", " dot "),
        username,                                           
    ]
    if domain:
        variants.append(domain)
        variants.append(domain.replace(".", "-"))

    # 去重 + 按长度倒序（先替换长的）
    return sorted(set(v for v in variants if v), key=len, reverse=True)

# ========== 敏感信息处理 ==========

async def mask_sensitive_inputs(tab):
    """
    1. 在敏感 input 上叠加固定覆盖层
    2. 替换页面文本节点中所有出现的邮箱（包括变形版本）
    """
    variants      = get_email_variants(EMAIL)
    variants_json = json.dumps(variants)

    script = f"""
    (function() {{
        // ========= 1. 输入框遮罩 =========
        const selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[type="password"]',
            'input[name="password"]',
            'input[type="text"]'
        ];

        for (let sel of selectors) {{
            let el = document.querySelector(sel);
            if (!el) continue;
            let rect = el.getBoundingClientRect();
            if (rect.width === 0) continue;
            if (el._masked) continue;
            el._masked = true;

            let overlay = document.createElement('div');
            overlay.style.cssText = `
                position: fixed;
                left: ${{rect.left}}px;
                top: ${{rect.top}}px;
                width: ${{rect.width}}px;
                height: ${{rect.height}}px;
                background: #333333;
                z-index: 999999;
                pointer-events: none;
                border-radius: 4px;
            `;
            overlay.setAttribute('data-mask', 'sensitive');
            document.body.appendChild(overlay);
        }}

        // ========= 2. 文本节点替换（含变形版本） =========
        const variants = {variants_json};

        function escapeRegex(s) {{
            return s.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
        }}

        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT
        );
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);

        let replacedCount = 0;
        nodes.forEach(node => {{
            let text    = node.textContent;
            let changed = false;
            for (let v of variants) {{
                if (!v) continue;
                let regex = new RegExp(escapeRegex(v), 'gi');
                if (regex.test(text)) {{
                    text    = text.replace(regex, '***');
                    changed = true;
                }}
            }}
            if (changed) {{
                node.textContent = text;
                replacedCount++;
            }}
        }});

        return {{
            overlays: document.querySelectorAll('[data-mask="sensitive"]').length,
            replaced: replacedCount
        }};
    }})()
    """

    try:
        result = await tab.execute_script(script)
        if isinstance(result, dict) and "result" in result:
            data = result.get("result", {}).get("result", {}).get("value", {})
        else:
            data = result
        if isinstance(data, dict):
            log.info(f"🔒 遮罩: {data.get('overlays', 0)} 个输入框, "
                     f"替换 {data.get('replaced', 0)} 处文本")
        else:
            log.info("🔒 已应用敏感信息遮罩")
    except Exception as e:
        log.warning(f"遮罩叠加失败: {e}", exc_info=True)


async def unmask_sensitive_inputs(tab):
    """截图完成后移除所有遮罩层（注意：文本节点替换不可逆，但不影响功能）"""
    script = """
    (function() {
        document.querySelectorAll('[data-mask="sensitive"]').forEach(el => el.remove());
        document.querySelectorAll('input').forEach(el => { el._masked = false; });
        return true;
    })()
    """
    try:
        await tab.execute_script(script)
        log.info("🔓 遮罩层已移除")
    except Exception as e:
        log.warning(f"遮罩移除失败: {e}")


async def blur_sensitive_areas(tab, image_path):
    """
    PIL 二次保险：
    1. 对 input 框区域填充黑色
    2. 对仍包含敏感字符串的叶子节点元素区域填充黑色（防 JS 替换遗漏 / 框架重渲染）
    """
    try:
        variants      = get_email_variants(EMAIL)
        variants_json = json.dumps(variants)

        script = f"""
        (function() {{
            const dpr   = window.devicePixelRatio || 1;
            const rects = [];

            // 1. 输入框
            const inputSelectors = [
                'input[type="email"]', 'input[name="email"]',
                'input[type="password"]', 'input[name="password"]'
            ];
            for (let sel of inputSelectors) {{
                let el = document.querySelector(sel);
                if (el) {{
                    let r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {{
                        rects.push({{
                            x:      Math.floor(r.x      * dpr),
                            y:      Math.floor(r.y      * dpr),
                            width:  Math.ceil (r.width  * dpr),
                            height: Math.ceil (r.height * dpr),
                            type:   'input'
                        }});
                    }}
                }}
            }}

            // 2. 含敏感文本的叶子节点
            const variants = {variants_json};
            const all      = document.querySelectorAll('body *');
            for (let el of all) {{
                if (el.children.length > 0) continue;  // 仅叶子节点
                let txt = el.textContent || '';
                if (!txt) continue;
                for (let v of variants) {{
                    if (v && txt.toLowerCase().includes(v.toLowerCase())) {{
                        let r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.height < 200) {{
                            rects.push({{
                                x:      Math.floor(r.x      * dpr),
                                y:      Math.floor(r.y      * dpr),
                                width:  Math.ceil (r.width  * dpr),
                                height: Math.ceil (r.height * dpr),
                                type:   'text'
                            }});
                        }}
                        break;
                    }}
                }}
            }}

            return {{ rects: rects, dpr: dpr }};
        }})()
        """

        result = await tab.execute_script(script)
        if isinstance(result, dict) and "result" in result:
            data = result.get("result", {}).get("result", {}).get("value", {})
        else:
            data = result

        if not isinstance(data, dict):
            log.warning(f"blur_sensitive_areas: 返回格式异常 {type(data)}")
            return

        rects = data.get("rects", [])
        dpr   = data.get("dpr", 1)
        log.info(f"DPR={dpr}, 检测到 {len(rects)} 个敏感区域")

        if not rects:
            return

        img          = Image.open(image_path)
        img_w, img_h = img.size
        draw         = ImageDraw.Draw(img)

        for rect in rects:
            x = int(rect.get('x', 0))
            y = int(rect.get('y', 0))
            w = int(rect.get('width', 0))
            h = int(rect.get('height', 0))
            t = rect.get('type', '')

            if w <= 0 or h <= 0:
                continue

            padding = 4
            box = (
                max(0,     x - padding),
                max(0,     y - padding),
                min(img_w, x + w + padding),
                min(img_h, y + h + padding),
            )
            draw.rectangle(box, fill=(40, 40, 40))
            log.info(f"  [{t}] 已填充: {box}")

        img.save(image_path)
        log.info(f"🔒 PIL 二次打码完成: {image_path}")

    except Exception as e:
        log.warning(f"blur_sensitive_areas 失败: {e}", exc_info=True)


async def take_screenshot(browser, tab, name):
    """
    安全截图完整流程：
    1. JS 遮罩覆盖敏感输入框 + 文本替换
    2. 等待浏览器渲染
    3. 截图
    4. PIL 二次打码（兜底）
    5. 移除遮罩，恢复页面
    """
    path = None
    try:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")

        await mask_sensitive_inputs(tab)
        await asyncio.sleep(0.8)

        await tab.take_screenshot(path=path)
        log.info(f"📸 原始截图已保存: {path}")

        await blur_sensitive_areas(tab, path)

    except Exception as e:
        log.warning(f"截图失败: {e}", exc_info=True)

    finally:
        try:
            await unmask_sensitive_inputs(tab)
        except Exception as e:
            log.warning(f"遮罩清理失败: {e}")

# ========== 辅助函数 ==========

async def get_text(tab):
    try:
        result = await tab.execute_script("return document.body.innerText")
        if isinstance(result, dict):
            return result.get("result", {}).get("result", {}).get("value", "")
        return str(result)
    except:
        return ""

async def get_url(tab):
    try:
        result = await tab.execute_script("return window.location.href")
        if isinstance(result, dict):
            return result.get("result", {}).get("result", {}).get("value", "")
        return str(result)
    except:
        return ""

async def human_delay(min_s=0.3, max_s=0.8):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def wait_for_url_contains(tab, keyword, timeout=10):
    for _ in range(timeout * 2):
        url = await get_url(tab)
        if keyword in url:
            return True
        await asyncio.sleep(0.5)
    return False

async def wait_for_element_by_text(tab, text, timeout=10):
    for _ in range(timeout * 2):
        body = await get_text(tab)
        if text in body:
            return True
        await asyncio.sleep(0.5)
    return False

# ========== JS 工具函数 ==========

async def js_click_button_by_text(tab, *texts):
    for text in texts:
        script = f"""
        (function() {{
            var btn = Array.from(document.querySelectorAll('button')).find(function(b) {{
                return (b.innerText || b.textContent || '').indexOf({json.dumps(text)}) !== -1;
            }});
            if (btn) {{ btn.click(); return true; }}
            return false;
        }})()
        """
        result  = await tab.execute_script(script)
        clicked = False
        if isinstance(result, dict):
            val     = result.get("result", {}).get("result", {}).get("value")
            clicked = bool(val)
        elif isinstance(result, bool):
            clicked = result
        if clicked:
            log.info(f"JS 点击按钮成功: '{text}'")
            return text
    return None

async def js_fill_input(tab, value, selectors):
    for sel in selectors:
        script = f"""
        (function() {{
            var el = document.querySelector({json.dumps(sel)});
            if (!el) return false;
            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(el, {json.dumps(value)});
            el.dispatchEvent(new Event('input',  {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }})()
        """
        result = await tab.execute_script(script)
        ok     = False
        if isinstance(result, dict):
            val = result.get("result", {}).get("result", {}).get("value")
            ok  = bool(val)
        elif isinstance(result, bool):
            ok = result
        if ok:
            log.info(f"JS 填写 input 成功: selector='{sel}'")
            return True
    return False

# ========== Cloudflare 处理 ==========

async def manual_cf_click(tab, timeout=15):
    log.info("尝试手动完成 Cloudflare 验证（Shadow DOM 穿透点击）...")
    for i in range(timeout):
        body = await get_text(tab)
        if "email" in body or "login" in body or "password" in body:
            log.info("✅ Cloudflare 验证已通过")
            return True
        try:
            shadow_roots = await tab.find_shadow_roots(deep=False)
            cf_shadow    = None
            for sr in shadow_roots:
                try:
                    html = await sr.inner_html
                    if "challenges.cloudflare.com" in html:
                        cf_shadow = sr
                        break
                except:
                    pass
            if cf_shadow is None:
                await asyncio.sleep(1)
                continue
            iframe_el    = await cf_shadow.query('iframe[src*="challenges.cloudflare.com"]', timeout=3)
            body_el      = await iframe_el.find(tag_name="body", timeout=3)
            inner_shadow = await body_el.get_shadow_root(timeout=3)
            checkbox     = await inner_shadow.query("span.cb-i", timeout=3)
            await checkbox.click()
            log.info("已点击 Cloudflare checkbox，等待验证...")
            await asyncio.sleep(3)
            body2 = await get_text(tab)
            if "email" in body2 or "login" in body2 or "password" in body2:
                log.info("✅ 点击后验证通过")
                return True
        except Exception as e:
            log.info(f"第{i+1}s: {e}")
        await asyncio.sleep(1)
    log.error("Cloudflare 验证超时")
    return False

async def ensure_cf_passed(tab, url, timeout=15):
    try:
        async with tab.expect_and_bypass_cloudflare_captcha():
            await tab.go_to(url)
    except:
        await tab.go_to(url)
    for _ in range(timeout):
        body = await get_text(tab)
        if "verify you are human" not in body.lower() and "cloudflare" not in body.lower():
            return True
        await asyncio.sleep(1)
    return await manual_cf_click(tab)

# ========== 验证码识别（备用） ==========

async def fill_captcha(tab):
    for _ in range(3):
        cap_img = None
        try:
            cap_img = await tab.find(id="allow_login_email_captcha", timeout=5)
        except:
            pass
        if not cap_img:
            try:
                cap_img = await tab.find(tag_name="img", alt="captcha", timeout=5)
            except:
                pass
        if cap_img:
            src = cap_img.get_attribute("src")
            if src and src.startswith("data:image"):
                b64       = src.split(",", 1)[1]
                img_bytes = base64.b64decode(b64)
                raw       = ocr.classification(img_bytes)
                code      = re.sub(r'[^0-9]', '', raw)
                log.info(f"识别验证码: {code}")
                await tab.execute_script(f"""
                    (function() {{
                        var input =
                            document.querySelector('#captcha_allow_login_email_captcha') ||
                            document.querySelector('input[name="captcha"]') ||
                            document.querySelector('input[placeholder*="captcha"]');
                        if (input) {{
                            input.focus();
                            input.value = '{code}';
                            input.dispatchEvent(new Event('input',  {{ bubbles:true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles:true }}));
                        }}
                    }})()
                """)
                return code
        await asyncio.sleep(1)
    return ""

# ========== 浏览器创建 ==========

def _find_chromium() -> str | None:
    candidates = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            log.info(f"找到 Chromium: {p}")
            return p
    import subprocess
    try:
        result = subprocess.run(
            ["which", "chromium-browser"],
            capture_output=True, text=True, timeout=5
        )
        path = result.stdout.strip()
        if path and os.path.isfile(path):
            return path
    except:
        pass
    return None

async def create_browser():
    opts          = ChromiumOptions()
    opts.headless = False
    path          = _find_chromium()
    if path:
        opts.binary_location = path

    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-features=VizDisplayCompositor")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--exclude-switches=enable-automation")
    opts.add_argument("--disable-infobars")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_argument("--disable-save-password-bubble")
    opts.add_argument("--disable-password-generation")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")
    # 强制 DPR=1，避免高分屏坐标偏移
    opts.add_argument("--force-device-scale-factor=1")

    opts.browser_preferences = {
        "credentials_enable_service": False,
        "credentials_enable_autosign": False,
        "profile": {
            "password_manager_enabled": False,
            "default_content_setting_values": {
                "notifications": 2,
                "geolocation":   2,
            },
        },
        "autofill": {"enabled": False},
        "intl":     {"accept_languages": "en-US,en"},
    }

    browser = await Chrome(options=opts).__aenter__()
    tab     = await browser.start()

    try:
        await tab.execute_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => false });"
        )
    except:
        pass

    return browser, tab

# ========== Searcade 登录流程 ==========

async def login_searcade(browser, tab):
    log.info("开始登录 Searcade...")

    await ensure_cf_passed(tab, BASE_URL)
    await take_screenshot(browser, tab, "01_home")

    log.info("点击登录按钮")
    try:
        login_btn = await tab.find(tag_name="a",      text="Login", timeout=10)
    except:
        login_btn = await tab.find(tag_name="button", text="Login", timeout=10)
    await login_btn.click()
    await asyncio.sleep(2)

    if not await wait_for_url_contains(tab, "userveria.com", timeout=15):
        log.warning("未跳转到 userveria，当前 URL: " + await get_url(tab))
        oauth_url = (
            f"{USERVERIA_AUTH_URL}"
            f"?client_id=8305d2e2-e91f-4deb-8909-f669259bc23f"
            f"&redirect_uri={REDIRECT_URI}"
            f"&scope=profile"
            f"&response_type=code"
        )
        await tab.go_to(oauth_url)
        await asyncio.sleep(2)

    await ensure_cf_passed(tab, await get_url(tab))

    log.info("填写邮箱")
    email_filled = False
    try:
        email_input = await tab.find(tag_name="input", name="email", timeout=10)
        await email_input.click()
        await email_input.type_text(EMAIL, humanize=True)
        email_filled = True
    except:
        pass
    if not email_filled:
        try:
            email_input = await tab.find(tag_name="input", type="email", timeout=10)
            await email_input.click()
            await email_input.type_text(EMAIL, humanize=True)
            email_filled = True
        except:
            pass
    if not email_filled:
        ok = await js_fill_input(tab, EMAIL, [
            'input[name="email"]',
            'input[type="email"]',
            'input[placeholder*="email"]',
            'input[placeholder*="Email"]',
        ])
        if not ok:
            raise Exception("无法填写邮箱输入框")
    await human_delay()

    log.info("点击 Continue with email 按钮")
    matched = await js_click_button_by_text(tab, "Continue with email", "Continue")
    if not matched:
        fallback = await tab.execute_script("""
        (function() {
            var btn = document.querySelector('button[type="submit"]') ||
                      document.querySelector('button[data-slot="button"]') ||
                      document.querySelector('form button');
            if (btn) { btn.click(); return true; }
            return false;
        })()
        """)
        if not fallback:
            raise Exception("找不到 Continue with email 按钮")
        log.info("已通过 fallback 点击提交按钮")

    log.info("等待页面跳转到密码输入步骤...")
    await asyncio.sleep(2)

    log.info("等待密码输入框")
    pass_filled = False
    try:
        pass_input = await tab.find(tag_name="input", name="password", timeout=15)
        await pass_input.click()
        await pass_input.type_text(PASSWORD, humanize=True)
        pass_filled = True
    except:
        pass
    if not pass_filled:
        try:
            pass_input = await tab.find(tag_name="input", type="password", timeout=15)
            await pass_input.click()
            await pass_input.type_text(PASSWORD, humanize=True)
            pass_filled = True
        except:
            pass
    if not pass_filled:
        ok = await js_fill_input(tab, PASSWORD, [
            'input[name="password"]',
            'input[type="password"]',
        ])
        if not ok:
            raise Exception("无法填写密码输入框")
    await human_delay()

    log.info("点击登录提交")
    matched = await js_click_button_by_text(tab, "Log in", "Login", "Sign in")
    if not matched:
        fallback = await tab.execute_script("""
        (function() {
            var btn = document.querySelector('button[type="submit"]') ||
                      document.querySelector('form button');
            if (btn) { btn.click(); return true; }
            return false;
        })()
        """)
        if not fallback:
            raise Exception("找不到登录提交按钮")
        log.info("已通过 fallback 点击登录按钮")

    log.info("等待回调并确认页面加载完成...")
    signed_in = False
    for _ in range(20):
        url  = await get_url(tab)
        body = await get_text(tab)
        if "searcade.com" in url and "userveria" not in url:
            if (
                "successfully signed in" in body.lower()
                or "your servers"        in body.lower()
                or "logout"              in body.lower()
                or "sign out"            in body.lower()
                or "dashboard"           in body.lower()
                or ("welcome back"       in body.lower() and "searcade" in url)
            ):
                signed_in = True
                log.info("✅ 登录验证成功，当前 URL: " + url)
                break
        await asyncio.sleep(0.5)

    await take_screenshot(browser, tab, "02_logged_in")

    if not signed_in:
        url = await get_url(tab)
        if "searcade.com/en/admin" in url or "searcade.com/en/" in url:
            log.info("✅ URL 判断登录成功: " + url)
            signed_in = True
        else:
            log.error("❌ 登录后未找到成功标识，URL: " + url)
            return False

    log.info("✅ 登录成功，准备进入服务器页面")

    # ========== 进入服务器 ==========
    log.info("滚动页面，寻找服务器卡片...")
    await tab.execute_script("window.scrollBy(0, 400);")
    await asyncio.sleep(1)

    server_clicked = await tab.execute_script("""
    (function() {
        var links = Array.from(document.querySelectorAll('a[href]'));
        var srv = links.find(function(a) {
            return (/\\/servers\\/|admin\\/servers/).test(a.href);
        });
        if (srv) { srv.click(); return srv.href; }
        return null;
    })()
    """)

    clicked_href = None
    if isinstance(server_clicked, dict):
        clicked_href = server_clicked.get("result", {}).get("result", {}).get("value")
    elif isinstance(server_clicked, str):
        clicked_href = server_clicked

    if clicked_href:
        log.info(f"点击服务器卡片: {clicked_href}")
    else:
        log.warning("未找到 /servers/ 链接，尝试点击第一个卡片...")
        await tab.execute_script("""
        (function() {
            var card = document.querySelector('a.card, a[class*=card], main a, .servers a');
            if (card) card.click();
        })()
        """)

    log.info("等待服务器管理页面加载...")
    if await wait_for_url_contains(tab, "/servers/", timeout=15):
        log.info("✅ 已进入服务器管理页面，URL: " + await get_url(tab))
    else:
        log.warning("未检测到 /servers/ URL，当前: " + await get_url(tab))

    await asyncio.sleep(2)
    await take_screenshot(browser, tab, "03_server_manage")

    body2 = await get_text(tab)
    if "manage" in body2.lower():
        log.info("✅ 服务器 Manage 页面加载成功")
    else:
        log.warning("⚠️ 未找到 Manage 字样，请检查截图 03_server_manage")

    return True

# ========== 主流程 ==========

async def main():
    browser, tab = None, None
    try:
        browser, tab = await create_browser()
        success      = await login_searcade(browser, tab)
        if success:
            wxpush("✅ Searcade 自动登录成功")
        else:
            wxpush("❌ Searcade 登录失败，请检查截图")
    except Exception as e:
        log.exception(e)
        if browser and tab:
            await take_screenshot(browser, tab, "99_error")
        wxpush(f"❌ Searcade 登录异常: {e}")
    finally:
        if browser:
            await asyncio.sleep(5)
            await browser.__aexit__(None, None, None)
        log.info("任务结束")

if __name__ == "__main__":
    asyncio.run(main())
