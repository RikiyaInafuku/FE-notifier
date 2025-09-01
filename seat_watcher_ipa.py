import os, re, ssl, smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

IPA_LOGIN_URL = "https://itee.ipa.go.jp/ipa/user/public/login/"
IPA_FE_ENTRY_URL = "https://itee.ipa.go.jp/ipa/user/public/cbt_entry/fc_fe/"

# ---- Secrets / env ----
def need(name):
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"環境変数 {name} が未設定です")
    return v

IPA_USER_ID        = need("IPA_USER_ID")
IPA_PASSWORD       = need("IPA_PASSWORD")
GMAIL_ADDRESS      = need("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = need("GMAIL_APP_PASSWORD")

REGION_NAME    = os.environ.get("REGION_NAME", "九州・沖縄")
PREF_NAME      = os.environ.get("PREF_NAME", "沖縄県")
START_YM       = os.environ.get("START_YM", "2025-11")  # 2025年11月以降のみ
TARGET_CENTERS = [s.strip() for s in os.environ.get(
    "TARGET_CENTERS",
    "沖縄県庁前テストセンター,那覇テストセンター,OAC沖縄校テストセンター"
).split(",") if s.strip()]

SEL = {
    "login_user":    "input[name='loginId'], input#loginId, input[type='text']",
    "login_pass":    "input[name='password'], input#password, input[type='password']",
    "login_button":  "button:has-text('ログイン'), input[type='submit']",
    "resume_button": "a:has-text('申込再開'), button:has-text('申込再開')",
    "exam_option":   "text=基本情報技術者試験(FE)科目A・科目B",
    "next_button":   "button:has-text('次へ'), a:has-text('次へ'), input[type='submit']",
    "student_radio": "label:has-text('学生') input, input[value='学生'], input[id*='student']",
    "agree_check":   "input[type='checkbox'], input[name*='agree'], input[id*='agree']",
    "region_select": "select:has(option:has-text('九州')), select",
    "pref_select":   "select:has(option:has-text('沖縄県')), select",
    "region_btn":    "button:has-text('九州・沖縄'), a:has-text('九州・沖縄'), text=九州・沖縄",
    "pref_btn":      "button:has-text('沖縄県'), a:has-text('沖縄県'), text=沖縄県",
    "search_btn":    "button:has-text('検索'), input[type='submit'], a:has-text('検索')",
    "center_block":  "table, div",
    "detail_link":   "a:has-text('受験可能な日時の確認'), a:has-text('日時の確認'), a:has-text('確認')",
    "month_header":  "text=/\\d{4}年\\s*\\d{1,2}月/",
    "next_month":    "button:has-text('翌月'), a:has-text('翌月'), button[aria-label*='次'], a[aria-label*='次'], a:has-text('>')",
    "slot_link":     "a:has-text('○'), a:has-text('空き'), a:has-text('予約'), button:has-text('○'), button:has-text('空き'), button:has-text('予約')",
    "logout":        "a:has-text('ログアウト'), button:has-text('ログアウト')"
}

def log(m): print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {m}", flush=True)

def click_first(page, selector, timeout=5000, optional=False):
    try:
        page.locator(selector).first.click(timeout=timeout); return True
    except Exception:
        if not optional: raise
        return False

def send_gmail(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject; msg["From"] = GMAIL_ADDRESS; msg["To"] = GMAIL_ADDRESS
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD); s.send_message(msg)

def ym_from_header(text):
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", text)
    return (int(m.group(1)), int(m.group(2))) if m else None

def ensure_month_from(page, start_ym="2025-11", max_steps=24):
    sy, sm = map(int, start_ym.split("-"))
    for _ in range(max_steps):
        header = page.locator(SEL["month_header"]).first
        hdr = header.inner_text() if header.count() else page.inner_text("body")
        cur = ym_from_header(hdr or "")
        if cur and (cur[0] > sy or (cur[0] == sy and cur[1] >= sm)):
            log(f"カレンダー: {cur[0]}年{cur[1]}月（{sy}年{sm}月以降OK）"); return True
        if not click_first(page, SEL["next_month"], optional=True):
            log("次月ボタン不明→現月のみ走査"); return True
        page.wait_for_load_state("domcontentloaded")
    log("開始月まで到達できず"); return False

def main():
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            # ログイン
            page.goto(IPA_LOGIN_URL, wait_until="domcontentloaded")
            page.locator(SEL["login_user"]).first.fill(IPA_USER_ID)
            page.locator(SEL["login_pass"]).first.fill(IPA_PASSWORD)
            click_first(page, SEL["login_button"])
            page.wait_for_load_state("domcontentloaded")
            log("IPA: ログイン完了")

            # FE申込ページ → 申込再開 → 試験選択 → 学生 → 同意
            page.goto(IPA_FE_ENTRY_URL, wait_until="domcontentloaded")
            click_first(page, SEL["resume_button"], optional=True)
            try:
                page.locator(SEL["exam_option"]).first.click(timeout=2500)
                click_first(page, SEL["next_button"], optional=True)
                log("試験選択: FE 科目A/B")
            except PWTimeout:
                log("試験選択画面をスキップ")

            if click_first(page, SEL["student_radio"], optional=True):
                click_first(page, SEL["next_button"], optional=True)
                log("区分: 学生")

            if click_first(page, SEL["agree_check"], optional=True):
                click_first(page, SEL["next_button"], optional=True)
                log("同意: チェック")

            # 地域→九州・沖縄 / 都道府県→沖縄県
            if not click_first(page, SEL["region_btn"], optional=True):
                try: page.locator(SEL["region_select"]).first.select_option(label=REGION_NAME)
                except Exception: log("地域選択: セレクタ調整要")
            if not click_first(page, SEL["pref_btn"], optional=True):
                try: page.locator(SEL["pref_select"]).first.select_option(label=PREF_NAME)
                except Exception: log("都道府県選択: セレクタ調整要")
            click_first(page, SEL["search_btn"], optional=True)
            page.wait_for_load_state("domcontentloaded")
            log("会場一覧 表示")

            # 指定3会場のみ監視
            blocks = page.locator(SEL["center_block"])
            for i in range(blocks.count()):
                try: text = blocks.nth(i).inner_text(timeout=1500)
                except Exception: continue
                if not any(c in text for c in TARGET_CENTERS): continue

                links = blocks.nth(i).locator(SEL["detail_link"])
                if links.count() == 0: links = page.locator(SEL["detail_link"])
                if links.count() == 0: continue
                links.first.click(); page.wait_for_load_state("domcontentloaded")

                ensure_month_from(page, START_YM)
                slots = page.locator(SEL["slot_link"])
                for j in range(slots.count()):
                    t = slots.nth(j).inner_text().strip()
                    if not any(k in t for k in ["○","空き","予約"]): continue
                    href = slots.nth(j).get_attribute("href") or ""
                    center = next((c for c in TARGET_CENTERS if c in text), text.splitlines()[0][:80])
                    found.append(f"{center} | {t} | {href}")

                page.go_back(); page.wait_for_load_state("domcontentloaded")

            # ログアウト
            if not click_first(page, SEL["logout"], optional=True):
                log("ログアウトリンク見当たらず（セッション自然失効想定）")
            else:
                log("ログアウト完了")

        finally:
            context.close(); browser.close()

    if found:
        send_gmail(
            "【CBTS/IPA】基本情報（沖縄3会場）空き枠を検出しました",
            "対象: 九州・沖縄 / 沖縄県 / 2025-11以降 / 指定3会場\n\n" + "\n".join(found)
        )
        log(f"Gmail送信: {len(found)}件")
    else:
        log("空き枠なし/検出できず")

if __name__ == "__main__":
    main()
