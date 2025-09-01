# watch.py
# FE（基本情報技術者：CBT）空席ウォッチャー
# - GitHub Actions のログで各ステップの PASS/WARN/FAIL を明示
# - UIは「selectで地域/都道府県/月/日 → 検索 → 会場表（○）」に対応
# - 2025-11以降の全月 × 全日レンジを総当たりして「○」を収集
# - Gmail通知は任意（SEND_EMAIL=true のときだけ使用）

import os, re, ssl, smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ===== 固定URL =====
IPA_LOGIN_URL = "https://itee.ipa.go.jp/ipa/user/public/login/"
IPA_FE_ENTRY_URL = "https://itee.ipa.go.jp/ipa/user/public/cbt_entry/fc_fe/"

# ===== 必須/任意環境変数 =====
def need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"環境変数 {name} が未設定です")
    return v

def truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")

# 必須
IPA_USER_ID  = need("IPA_USER_ID")
IPA_PASSWORD = need("IPA_PASSWORD")

# 任意（デフォルトあり）
REGION_NAME = os.environ.get("REGION_NAME", "九州・沖縄")
PREF_NAME   = os.environ.get("PREF_NAME", "沖縄県")
START_YM    = os.environ.get("START_YM", "2025-11")  # 2025年11月以降
TARGET_CENTERS = [s.strip() for s in os.environ.get(
    "TARGET_CENTERS",
    "沖縄県庁前テストセンター,那覇テストセンター,OAC沖縄校テストセンター"
).split(",") if s.strip()]

# 通知（完全オプション）
SEND_EMAIL        = truthy("SEND_EMAIL")
GMAIL_ADDRESS     = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD= os.environ.get("GMAIL_APP_PASSWORD", "")

# ===== ログ/アノテーション =====
def ts() -> str: return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
def info(msg: str): print(f"[{ts()}] {msg}", flush=True)

def pass_mark(step: str, detail: str = ""):
    print(f"::notice title={step}::PASS {detail}")
    info(f"[PASS] {step} {('- ' + detail) if detail else ''}")

def warn_mark(step: str, detail: str = ""):
    print(f"::warning title={step}::{detail}")
    info(f"[WARN] {step} {('- ' + detail) if detail else ''}")

def fail_mark(step: str, detail: str = ""):
    print(f"::error title={step}::FAIL {detail}")
    info(f"[FAIL] {step} {('- ' + detail) if detail else ''}")

def group_start(title: str): print(f"::group::{title}")
def group_end(): print("::endgroup::")

def check(cond: bool, step: str, ok: str, ng: str, critical: bool = False):
    if cond:
        pass_mark(step, ok)
    else:
        fail_mark(step, ng)
        if critical:
            raise RuntimeError(f"{step} 失敗: {ng}")

# ===== ユーティリティ =====
def send_gmail(subject: str, body: str):
    if not SEND_EMAIL:
        warn_mark("通知(メール)", "SEND_EMAIL=false のため送信スキップ"); return
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        fail_mark("通知(メール)", "GMAIL_ADDRESS/GMAIL_APP_PASSWORD 未設定"); return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_ADDRESS
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        pass_mark("通知(メール)", "SMTP送信成功")
    except Exception as e:
        fail_mark("通知(メール)", f"例外: {e}")

def parse_month_label(lb: str):
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", lb)
    return (int(m.group(1)), int(m.group(2))) if m else None

# ===== メイン =====
def main():
    found_lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # --- ログイン ---
            group_start("IPAログイン")
            page.goto(IPA_LOGIN_URL, wait_until="domcontentloaded")
            check(page.locator("form").first.count() > 0, "ログインページ", "フォーム検出", "フォーム見当たらず", True)

            # ラベル優先で入力、ダメならフォールバック
            try:
                page.get_by_label("利用者ID", exact=True).fill(IPA_USER_ID)
                pass_mark("ID入力", "label=利用者ID")
            except Exception:
                page.locator("input[name='loginId'], #loginId, input[autocomplete='username']").first.fill(IPA_USER_ID)
                pass_mark("ID入力", "fallbackセレクタ")

            try:
                page.get_by_label("パスワード", exact=True).fill(IPA_PASSWORD)
                pass_mark("PW入力", "label=パスワード")
            except Exception:
                page.locator("input[name='password'], #password, input[type='password'], input[autocomplete='current-password']").first.fill(IPA_PASSWORD)
                pass_mark("PW入力", "fallbackセレクタ")

            if page.get_by_role("button", name="ログイン").first.count():
                page.get_by_role("button", name="ログイン").first.click()
            else:
                page.locator("button:has-text('ログイン'), input[type='submit']").first.click()
            page.wait_for_load_state("domcontentloaded")

            # 成功判定：ID入力欄が消えている
            check(page.get_by_label("利用者ID", exact=True).first.count() == 0, "ログイン", "成功", "失敗の可能性", True)
            group_end()

            # --- FE申込導線 ---
            group_start("FE申込導線")
            page.goto(IPA_FE_ENTRY_URL, wait_until="domcontentloaded")
            pass_mark("FE申込ページ", "到達")

            # 申込再開（あれば）
            if page.locator("a:has-text('申込再開'), button:has-text('申込再開')").first.count():
                page.locator("a:has-text('申込再開'), button:has-text('申込再開')").first.click()
                page.wait_for_load_state("domcontentloaded")
                pass_mark("申込再開", "クリック")
            else:
                warn_mark("申込再開", "ボタンなし→スキップ")

            # 試験選択（行内の「次へ」）
            try:
                rows = page.locator("tr").filter(has_text="基本情報技術者試験(FE)科目A・科目B")
                if rows.count():
                    rows.first.get_by_role("button", name="次へ").click()
                    page.wait_for_load_state("domcontentloaded")
                    pass_mark("試験選択", "FE 科目A/B 行の『次へ』")
                else:
                    warn_mark("試験選択", "画面出ず/行なし→スキップ")
            except PWTimeout:
                warn_mark("試験選択", "タイムアウト→スキップ")
            group_end()

            # --- 区分/同意 ---
            group_start("区分/同意")
            if page.get_by_label("学生", exact=True).first.count():
                page.get_by_label("学生", exact=True).first.check()
                pass_mark("区分選択", "学生")
            else:
                warn_mark("区分選択", "学生ラジオなし")

            if page.get_by_label("同意する", exact=True).first.count():
                page.get_by_label("同意する", exact=True).first.check()
                pass_mark("同意確認", "同意する")
            else:
                warn_mark("同意確認", "同意UIなし")

            if page.get_by_role("button", name="次へ").first.count():
                page.get_by_role("button", name="次へ").first.click()
                page.wait_for_load_state("domcontentloaded")
                pass_mark("次へ", "アンケートから遷移")
            else:
                warn_mark("次へ", "ボタンなし（画面分岐）")
            group_end()

            # --- エリア/日程選択（select） ---
            group_start("エリア/日程選択")

            def select_in_row(row_label: str, option_label: str) -> bool:
                row = page.locator("tr").filter(has_text=row_label)
                if not row.count():
                    warn_mark("選択行", f"'{row_label}' 行なし"); return False
                sel = row.first.locator("select")
                if not sel.count():
                    warn_mark("選択UI", f"'{row_label}' に select なし"); return False
                try:
                    sel.first.select_option(label=option_label)
                    pass_mark("選択", f"{row_label}: {option_label}")
                    return True
                except Exception as e:
                    fail_mark("選択", f"{row_label}: '{option_label}' 選択失敗 ({e})")
                    return False

            def get_select(row_label: str):
                row = page.locator("tr").filter(has_text=row_label)
                if not row.count(): return None
                sel = row.first.locator("select")
                return sel.first if sel.count() else None

            # 地域/都道府県を固定
            select_in_row("地域", REGION_NAME)   # 例: 九州・沖縄
            select_in_row("都道府県", PREF_NAME) # 例: 沖縄県

            # 月/日 セレクト
            month_sel = get_select("月")
            day_sel   = get_select("日")
            check(bool(month_sel and day_sel), "セレクト取得", "月/日を取得", "月/日セレクトが無い", True)

            sy, sm = map(int, START_YM.split("-"))

            # 月候補（START_YM 以降）
            month_opts = []
            for i in range(month_sel.locator("option").count()):
                lb = (month_sel.locator("option").nth(i).inner_text() or "").strip()
                pm = parse_month_label(lb)
                if not pm:  # 「選択してください」など
                    continue
                if (pm[0] > sy) or (pm[0] == sy and pm[1] >= sm):
                    month_opts.append(lb)
            if not month_opts: warn_mark("月", f"{START_YM} 以降の候補なし")

            # 日候補（先頭のプレースホルダ除外）
            day_opts = []
            for i in range(day_sel.locator("option").count()):
                lb = (day_sel.locator("option").nth(i).inner_text() or "").strip()
                if "選択" in lb or lb == "":
                    continue
                day_opts.append((i, lb))
            if not day_opts: warn_mark("日", "有効な日レンジが見つからない")

            group_end()

            # --- 検索・抽出ループ ---
            group_start("検索・抽出ループ")

            def click_search() -> bool:
                if page.get_by_role("button", name="検索").first.count():
                    page.get_by_role("button", name="検索").first.click()
                    page.wait_for_load_state("domcontentloaded")
                    pass_mark("会場検索", "検索押下")
                    return True
                warn_mark("会場検索", "ボタンなし"); return False

            def extract_table_slots(selected_month: str, selected_day: str):
                # 最も上の table を対象（画面上1つ想定）
                tables = page.locator("table")
                if tables.count() == 0:
                    warn_mark("会場表", "tableなし"); return

                rows = tables.first.locator("tr")
                matched = 0
                for i in range(rows.count()):
                    r = rows.nth(i)
                    # 会場名（a要素優先）
                    name = ""
                    if r.locator("a").count():
                        name = (r.locator("a").first.inner_text() or "").strip()
                    else:
                        try: name = (r.locator("td").first.inner_text() or "").strip()
                        except Exception: name = ""
                    if not name or not any(c in name for c in TARGET_CENTERS):
                        continue

                    matched += 1
                    pass_mark("会場一致", name)

                    cells = r.locator("a:has-text('○'), button:has-text('○'), td:has-text('○')")
                    cnt = cells.count()
                    if cnt == 0:
                        warn_mark("枠抽出", f"{name}: 0件")
                        continue

                    for j in range(cnt):
                        t = (cells.nth(j).inner_text() or "").strip()
                        href = ""
                        try: href = cells.nth(j).get_attribute("href") or ""
                        except: pass
                        line = f"{name} | {selected_month} | {selected_day} | {t}"
                        if href: line += f" | {href}"
                        found_lines.append(line)
                    pass_mark("枠抽出", f"{name}: {cnt}件")

                if matched == 0:
                    warn_mark("会場一致", "指定会場ヒットなし（表記ぶれの可能性）")

            # 総当たり：月 × 日レンジ
            loop_months = month_opts if month_opts else [""]
            loop_days   = day_opts   if day_opts   else [(1, "任意")]

            for m_lb in loop_months:
                if m_lb:
                    try:
                        month_sel.select_option(label=m_lb)
                        pass_mark("月選択", m_lb)
                    except Exception as e:
                        warn_mark("月選択", f"'{m_lb}' 選択失敗: {e}")
                        continue
                for day_index, day_lb in loop_days:
                    try:
                        day_sel.select_option(index=day_index)
                        pass_mark("日選択", day_lb)
                    except Exception as e:
                        warn_mark("日選択", f"'{day_lb}' 選択失敗: {e}")
                        continue

                    if click_search():
                        extract_table_slots(m_lb or "(指定なし)", day_lb)

            group_end()

            # --- ログアウト（任意） ---
            group_start("ログアウト")
            if page.locator("a:has-text('ログアウト'), button:has-text('ログアウト')").first.count():
                page.locator("a:has-text('ログアウト'), button:has-text('ログアウト')").first.click()
                page.wait_for_load_state("domcontentloaded")
                pass_mark("ログアウト", "成功")
            else:
                warn_mark("ログアウト", "UIなし（自然失効想定）")
            group_end()

        finally:
            context.close(); browser.close()

    # --- 実行まとめ ---
    group_start("実行まとめ")
    info(f"検出件数: {len(found_lines)}")
    if found_lines:
        pass_mark("実行結果", f"空き枠 {len(found_lines)}件 検出")
        body = "対象: 地域={0} / 都道府県={1} / 開始月={2}\n\n".format(REGION_NAME, PREF_NAME, START_YM) + "\n".join(found_lines)
        send_gmail("【CBTS/IPA】基本情報（沖縄3会場）空き枠を検出しました", body)
    else:
        warn_mark("実行結果", "空き枠は検出されませんでした")
    group_end()

if __name__ == "__main__":
    try:
        main()
        print("::notice title=Job Summary::スクリプトは正常終了（空き無しも成功扱い）")
    except Exception as e:
        print(f"::error title=Job Summary::致命的な例外で終了: {e}")
        raise
