import os, re, ssl, smtplib, sys
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ===== 固定URL =====
IPA_LOGIN_URL = "https://itee.ipa.go.jp/ipa/user/public/login/"
IPA_FE_ENTRY_URL = "https://itee.ipa.go.jp/ipa/user/public/cbt_entry/fc_fe/"

# ===== 必須環境変数の取得 =====
def need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"環境変数 {name} が未設定です")
    return v

IPA_USER_ID        = need("IPA_USER_ID")
IPA_PASSWORD       = need("IPA_PASSWORD")
GMAIL_ADDRESS      = need("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = need("GMAIL_APP_PASSWORD")

# ===== 監視条件（未設定ならデフォルト） =====
REGION_NAME    = os.environ.get("REGION_NAME", "九州・沖縄")
PREF_NAME      = os.environ.get("PREF_NAME", "沖縄県")
START_YM       = os.environ.get("START_YM", "2025-11")  # 2025年11月以降のみ
TARGET_CENTERS = [s.strip() for s in os.environ.get(
    "TARGET_CENTERS",
    "沖縄県庁前テストセンター,那覇テストセンター,OAC沖縄校テストセンター"
).split(",") if s.strip()]

# ===== セレクタ候補 =====
SEL = {
    # ログイン
    "login_user":    "input[name='loginId'], input#loginId, input[type='text']",
    "login_pass":    "input[name='password'], input#password, input[type='password']",
    "login_button":  "button:has-text('ログイン'), input[type='submit']",

    # FE申込導線
    "resume_button": "a:has-text('申込再開'), button:has-text('申込再開')",
    "exam_option":   "text=基本情報技術者試験(FE)科目A・科目B",
    "next_button":   "button:has-text('次へ'), a:has-text('次へ'), input[type='submit']",
    "student_radio": "label:has-text('学生') input, input[value='学生'], input[id*='student']",
    "agree_check":   "input[type='checkbox'], input[name*='agree'], input[id*='agree']",

    # エリア・県
    "region_select": "select:has(option:has-text('九州')), select",
    "pref_select":   "select:has(option:has-text('沖縄県')), select",
    "region_btn":    "button:has-text('九州・沖縄'), a:has-text('九州・沖縄'), text=九州・沖縄",
    "pref_btn":      "button:has-text('沖縄県'), a:has-text('沖縄県'), text=沖縄県",
    "search_btn":    "button:has-text('検索'), input[type='submit'], a:has-text('検索')",

    # 会場 → 詳細
    "center_block":  "table, div",
    "detail_link":   "a:has-text('受験可能な日時の確認'), a:has-text('日時の確認'), a:has-text('確認')",

    # カレンダー
    "month_header":  "text=/\\d{4}年\\s*\\d{1,2}月/",
    "next_month":    "button:has-text('翌月'), a:has-text('翌月'), button[aria-label*='次'], a[aria-label*='次'], a:has-text('>')",
    "slot_link":     "a:has-text('○'), a:has-text('空き'), a:has-text('予約'), button:has-text('○'), button:has-text('空き'), button:has-text('予約')",

    # ログアウト
    "logout":        "a:has-text('ログアウト'), button:has-text('ログアウト')",
}

# ===== ログ/計測ユーティリティ =====
def ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

def info(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)

def pass_mark(step: str, detail: str = "") -> None:
    # 緑系のアノテーション（notice）
    print(f"::notice title={step}::PASS {detail}")
    info(f"[PASS] {step} {('- ' + detail) if detail else ''}")

def warn_mark(step: str, detail: str = "") -> None:
    print(f"::warning title={step}::{detail}")
    info(f"[WARN] {step} {('- ' + detail) if detail else ''}")

def fail_mark(step: str, detail: str = "") -> None:
    # 赤いアノテーション（error）
    print(f"::error title={step}::FAIL {detail}")
    info(f"[FAIL] {step} {('- ' + detail) if detail else ''}")

def group_start(title: str) -> None:
    print(f"::group::{title}")

def group_end() -> None:
    print("::endgroup::")

def check(cond: bool, step: str, ok: str, ng: str, critical: bool = False) -> None:
    if cond:
        pass_mark(step, ok)
    else:
        fail_mark(step, ng)
        if critical:
            raise RuntimeError(f"{step} 失敗: {ng}")

# ===== Gmail送信（空き検出時のみ/ログ目的でも使える） =====
def send_gmail(subject: str, body: str) -> None:
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = GMAIL_ADDRESS
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        pass_mark("Gmail送信", "SMTP送信に成功")
    except Exception as e:
        fail_mark("Gmail送信", f"SMTPエラー: {e}")

# ===== カレンダーの月合わせ =====
def ym_from_header(text: str):
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", text)
    return (int(m.group(1)), int(m.group(2))) if m else None

def ensure_month_from(page, start_ym: str = "2025-11", max_steps: int = 24) -> bool:
    sy, sm = map(int, start_ym.split("-"))
    for _ in range(max_steps):
        header = page.locator(SEL["month_header"]).first
        hdr = header.inner_text() if header.count() else page.inner_text("body")
        cur = ym_from_header(hdr or "")
        if cur and (cur[0] > sy or (cur[0] == sy and cur[1] >= sm)):
            pass_mark("月合わせ", f"{cur[0]}年{cur[1]}月（開始 {sy}-{sm} 以降）")
            return True
        if not page.locator(SEL["next_month"]).first.count():
            warn_mark("月合わせ", "『翌月』ボタン不明 → 現月のみ走査")
            return True
        page.locator(SEL["next_month"]).first.click()
        page.wait_for_load_state("domcontentloaded")
    fail_mark("月合わせ", "指定開始月まで到達できませんでした")
    return False

# ===== メイン =====
def main():
    found_lines = []
    centers_matched = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # --- ログイン ---
            group_start("IPAログイン")
            page.goto(IPA_LOGIN_URL, wait_until="domcontentloaded")
            check(page.locator(SEL["login_user"]).first.count() > 0, "ログインページ",
                  "ログインフォーム検出", "ログインフォームが見つからない", critical=True)

            page.locator(SEL["login_user"]).first.fill(IPA_USER_ID)
            page.locator(SEL["login_pass"]).first.fill(IPA_PASSWORD)
            page.locator(SEL["login_button"]).first.click()
            page.wait_for_load_state("domcontentloaded")

            # ログイン成功チェック（マイページ内にいる想定：FE申込ページへ飛べる）
            # 厳密にタイトル等を見れない場合があるので、フォームが消えていることを以て成功とする
            check(page.locator(SEL["login_user"]).first.count() == 0, "ログイン",
                  "ログイン成功っぽい（フォーム消失）", "ログインに失敗した可能性", critical=True)
            group_end()

            # --- FE申込導線 ---
            group_start("FE申込ページ")
            page.goto(IPA_FE_ENTRY_URL, wait_until="domcontentloaded")
            pass_mark("FE申込ページ", "到達")

            # 申込再開（無い場合もあるのでoptional）
            if page.locator(SEL["resume_button"]).first.count():
                page.locator(SEL["resume_button"]).first.click()
                page.wait_for_load_state("domcontentloaded")
                pass_mark("申込再開", "クリック成功")
            else:
                warn_mark("申込再開", "ボタンなし → スキップ")

            # 試験選択（出ない画面ならスキップ）
            try:
                if page.locator(SEL["exam_option"]).first.count():
                    page.locator(SEL["exam_option"]).first.click(timeout=2500)
                    if page.locator(SEL["next_button"]).first.count():
                        page.locator(SEL["next_button"]).first.click()
                        page.wait_for_load_state("domcontentloaded")
                    pass_mark("試験選択", "FE 科目A/B を選択")
                else:
                    warn_mark("試験選択", "画面出ず → スキップ")
            except PWTimeout:
                warn_mark("試験選択", "タイムアウト → スキップ")

            # 学生選択
            if page.locator(SEL["student_radio"]).first.count():
                page.locator(SEL["student_radio"]).first.click()
                if page.locator(SEL["next_button"]).first.count():
                    page.locator(SEL["next_button"]).first.click()
                    page.wait_for_load_state("domcontentloaded")
                pass_mark("区分選択", "学生 を選択")
            else:
                warn_mark("区分選択", "学生ラジオなし → スキップ")

            # 同意
            if page.locator(SEL["agree_check"]).first.count():
                page.locator(SEL["agree_check"]).first.check()
                if page.locator(SEL["next_button"]).first.count():
                    page.locator(SEL["next_button"]).first.click()
                    page.wait_for_load_state("domcontentloaded")
                pass_mark("同意確認", "同意して次へ")
            else:
                warn_mark("同意確認", "チェックボックスなし → スキップ")
            group_end()

            # --- 地域/都道府県 選択 ---
            group_start("エリア選択")
            # 地域
            used_btn = False
            if page.locator(SEL["region_btn"]).first.count():
                page.locator(SEL["region_btn"]).first.click()
                used_btn = True
                pass_mark("地域選択", f"ボタンで選択: {REGION_NAME}")
            else:
                if page.locator(SEL["region_select"]).first.count():
                    try:
                        page.locator(SEL["region_select"]).first.select_option(label=REGION_NAME)
                        pass_mark("地域選択", f"セレクトで選択: {REGION_NAME}")
                    except Exception:
                        warn_mark("地域選択", "セレクトで選択失敗")
                else:
                    warn_mark("地域選択", "UI見つからず")

            # 都道府県
            if page.locator(SEL["pref_btn"]).first.count():
                page.locator(SEL["pref_btn"]).first.click()
                pass_mark("都道府県選択", f"ボタンで選択: {PREF_NAME}")
            else:
                if page.locator(SEL["pref_select"]).first.count():
                    try:
                        page.locator(SEL["pref_select"]).first.select_option(label=PREF_NAME)
                        pass_mark("都道府県選択", f"セレクトで選択: {PREF_NAME}")
                    except Exception:
                        warn_mark("都道府県選択", "セレクトで選択失敗")
                else:
                    warn_mark("都道府県選択", "UI見つからず")

            # 検索
            if page.locator(SEL["search_btn"]).first.count():
                page.locator(SEL["search_btn"]).first.click()
                page.wait_for_load_state("domcontentloaded")
                pass_mark("会場検索", "検索ボタンを押下")
            else:
                warn_mark("会場検索", "検索ボタンなし")
            group_end()

            # --- 会場一覧 → 詳細 ---
            group_start("会場走査")
            blocks = page.locator(SEL["center_block"])
            n_blocks = blocks.count()
            info(f"会場ブロック総数: {n_blocks}")

            for i in range(n_blocks):
                try:
                    text = blocks.nth(i).inner_text(timeout=1500)
                except Exception:
                    continue
                if not any(c in text for c in TARGET_CENTERS):
                    continue

                centers_matched += 1
                center_name = next((c for c in TARGET_CENTERS if c in text), text.splitlines()[0][:80])
                pass_mark("会場一致", f"{center_name}")

                # 詳細リンクへ
                links = blocks.nth(i).locator(SEL["detail_link"])
                if links.count() == 0:
                    links = page.locator(SEL["detail_link"])
                check(links.count() > 0, "詳細リンク", "リンク発見", "リンクが見つからない", critical=False)

                if links.count() > 0:
                    links.first.click()
                    page.wait_for_load_state("domcontentloaded")

                    # 月合わせ（2025-11以降）
                    ensure_month_from(page, START_YM)

                    # スロット抽出
                    slots = page.locator(SEL["slot_link"])
                    cnt = 0
                    for j in range(slots.count()):
                        t = slots.nth(j).inner_text().strip()
                        if not any(k in t for k in ["○", "空き", "予約"]):
                            continue
                        href = slots.nth(j).get_attribute("href") or ""
                        found_lines.append(f"{center_name} | {t} | {href}")
                        cnt += 1
                    if cnt > 0:
                        pass_mark("枠抽出", f"{center_name}: {cnt}件")
                    else:
                        warn_mark("枠抽出", f"{center_name}: 0件（該当表示なし）")

                    page.go_back()
                    page.wait_for_load_state("domcontentloaded")

            if centers_matched == 0:
                warn_mark("会場一致", "指定3会場に一致するブロックが見つからず")
            else:
                pass_mark("会場一致", f"一致会場数: {centers_matched}")
            group_end()

            # --- ログアウト ---
            group_start("ログアウト")
            if page.locator(SEL["logout"]).first.count():
                page.locator(SEL["logout"]).first.click()
                page.wait_for_load_state("domcontentloaded")
                pass_mark("ログアウト", "成功")
            else:
                warn_mark("ログアウト", "リンク/ボタン見当たらず（セッション自然失効想定）")
            group_end()

        finally:
            context.close()
            browser.close()

    # --- 実行まとめ ---
    group_start("実行まとめ")
    info(f"検出件数: {len(found_lines)}")
    if len(found_lines) > 0:
        pass_mark("実行結果", f"空き枠 {len(found_lines)}件を検出")
        # 本番運用でメール不要ならコメントアウトしてOK
        try:
            subject = "【CBTS/IPA】基本情報（沖縄3会場）空き枠を検出しました"
            body = "対象: 九州・沖縄 / 沖縄県 / 2025-11以降 / 指定3会場\n\n" + "\n".join(found_lines)
            send_gmail(subject, body)
        except Exception as e:
            fail_mark("通知", f"Gmail送信で例外: {e}")
    else:
        # 空きが無いのは正常動作なので FAIL にはしない
        warn_mark("実行結果", "空き枠は検出されませんでした")
    group_end()

if __name__ == "__main__":
    try:
        main()
        # 最後にジョブ全体の健康状態を一言
        print("::notice title=Job Summary::スクリプトは最後まで完了しました（空き枠が無い場合も成功扱い）。")
    except Exception as e:
        # ここに来たら“致命的な失敗”としてジョブを失敗させる
        print(f"::error title=Job Summary::致命的な例外で終了: {e}")
        raise
