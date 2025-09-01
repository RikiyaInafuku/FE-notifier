# main.py
# FE（基本情報技術者：CBT）空席ウォッチャー
# - Actionsログに PASS/WARN/FAIL を出力
# - 「地域 → 2秒待ち → 都道府県 → 月 → 日 → 検索」で抽出
# - START_YM 以降の月 × 全日レンジを総当たり
# - Gmail通知は SEND_EMAIL=true の時のみ

import os, re, ssl, smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright

# ===== 固定URL =====
IPA_LOGIN_URL    = "https://itee.ipa.go.jp/ipa/user/public/login/"
IPA_FE_ENTRY_URL = "https://itee.ipa.go.jp/ipa/user/public/cbt_entry/fc_fe/"

# ===== 必須/任意環境変数 =====
def need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"環境変数 {name} が未設定です")
    return v

def truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")

IPA_USER_ID  = need("IPA_USER_ID")
IPA_PASSWORD = need("IPA_PASSWORD")

REGION_NAME = os.environ.get("REGION_NAME", "九州・沖縄")
PREF_NAME   = os.environ.get("PREF_NAME", "沖縄県")
START_YM    = os.environ.get("START_YM", "2025-11")
TARGET_CENTERS = [s.strip() for s in os.environ.get(
    "TARGET_CENTERS",
    "沖縄県庁前テストセンター,那覇テストセンター,OAC沖縄校テストセンター"
).split(",") if s.strip()]

SEND_EMAIL         = truthy("SEND_EMAIL")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# ===== ログ/アノテーション =====
def ts() -> str: return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
def info(msg: str): print(f"[{ts()}] {msg}", flush=True)
def pass_mark(step: str, detail: str = ""): print(f"::notice title={step}::PASS {detail}")
def warn_mark(step: str, detail: str = ""): print(f"::warning title={step}::{detail}")
def fail_mark(step: str, detail: str = ""): print(f"::error title={step}::FAIL {detail}")
def group_start(title: str): print(f"::group::{title}")
def group_end(): print("::endgroup::")
def check(cond: bool, step: str, ok: str, ng: str, critical: bool = False):
    if cond:
        pass_mark(step, ok)
    else:
        fail_mark(step, ng)
        if critical:
            raise RuntimeError(f"{step} 失敗: {ng}")

# ===== ログイン入力の候補 & フォールバック =====
LOGIN_ID_CAND = [
    "input[name='loginId']", "input[name='userId']",
    "#loginId", "#userId",
    "input[autocomplete='username']",
    "input[placeholder*='利用者ID']",
    "input[type='text']",
]
LOGIN_PW_CAND = [
    "input[name='password']", "#password",
    "input[autocomplete='current-password']",
    "input[type='password']",
]
def fill_any(page, selectors, value, step):
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count():
            try:
                loc.scroll_into_view_if_needed()
                loc.wait_for(state="visible", timeout=10000)
                loc.fill(value, timeout=5000)
                pass_mark(step, f"{sel} で入力")
                return True
            except Exception as e:
                warn_mark(step, f"{sel} 失敗: {e}")
    fail_mark(step, f"{step} 候補全滅")
    raise RuntimeError(f"{step} 失敗")

# ===== ユーティリティ =====
def send_gmail(subject: str, body: str):
    if not SEND_EMAIL:
        warn_mark("通知(メール)", "SEND_EMAIL=false のため送信スキップ"); return
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        fail_mark("通知(メール)", "GMAIL_ADDRESS/GMAIL_APP_PASSWORD 未設定"); return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject; msg["From"] = GMAIL_ADDRESS; msg["To"] = GMAIL_ADDRESS
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD); s.send_message(msg)
        pass_mark("通知(メール)", "SMTP送信成功")
    except Exception as e:
        fail_mark("通知(メール)", f"例外: {e}")

def parse_month_label(lb: str):
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", lb)
    return (int(m.group(1)), int(m.group(2))) if m else None

# ===== 導線（エリア・日程選択ページへ確実に到達） =====
def on_area_date(page) -> bool:
    if page.get_by_text("エリア・日程選択", exact=False).first.count():
        return True
    has_region = page.locator("tr", has_text="地域").first.locator("select").count() > 0
    has_pref   = page.locator("tr", has_text="都道府県").first.locator("select").count() > 0
    has_search = page.get_by_role("button", name="検索").first.count() > 0
    return has_region and has_pref and has_search

def goto_area_date_page(page) -> bool:
    group_start("FE申込導線")
    try:
        # マイページ中央のタイル
        link = page.get_by_role("link", name=re.compile(r"基本情報技術者試験\(FE\)\s*CBT試験申込"))
        if link.first.count():
            link.first.click(); page.wait_for_load_state("domcontentloaded")
        else:
            fe = page.get_by_role("link", name=re.compile(r"基本情報技術者試験\(FE\)"))
            if fe.first.count():
                fe.first.click(); page.wait_for_load_state("domcontentloaded")
                l2 = page.get_by_role("link", name=re.compile(r"CBT試験申込"))
                if l2.first.count():
                    l2.first.click(); page.wait_for_load_state("domcontentloaded")
            else:
                page.goto(IPA_FE_ENTRY_URL, wait_until="domcontentloaded")
        info(f"到達1: {page.url}")
        if on_area_date(page):
            pass_mark("導線", "到達(エリア・日程)"); return True

        # ページ最下部の「申込再開」
        btn = page.get_by_role("button", name=re.compile(r"申込再開"))
        if not btn.first.count():
            btn = page.locator("a:has-text('申込再開'), button:has-text('申込再開')")
        if btn.first.count():
            btn.first.scroll_into_view_if_needed()
            btn.first.click(); page.wait_for_load_state("domcontentloaded")
        info(f"到達2: {page.url}")
        if on_area_date(page):
            pass_mark("導線", "申込再開→到達"); return True

        # Step1 の赤い「選択する/入力はこちらから」
        selbtn = page.get_by_role("button", name=re.compile(r"選択する|入力はこちらから"))
        if not selbtn.first.count():
            selbtn = page.locator("a:has-text('選択する'), a:has-text('入力はこちらから'), button:has-text('選択する')")
        if selbtn.first.count():
            selbtn.first.click(); page.wait_for_load_state("domcontentloaded")
        info(f"到達3: {page.url}")

        # 試験一覧 → FE行の「次へ」
        row = page.locator("tr").filter(has_text=re.compile(r"基本情報技術者試験\(FE\).*科目A.*科目B"))
        if row.count() and row.first.get_by_role("button", name="次へ").count():
            row.first.get_by_role("button", name="次へ").click()
            page.wait_for_load_state("domcontentloaded")
        else:
            nx = page.get_by_role("button", name="次へ")
            if nx.first.count():
                nx.first.click(); page.wait_for_load_state("domcontentloaded")
        info(f"到達4: {page.url}")

        # アンケ：学生 + 同意する → 次へ
        if page.get_by_label("学生", exact=True).first.count():
            page.get_by_label("学生", exact=True).first.check(); pass_mark("区分選択", "学生")
        if page.get_by_label("同意する", exact=True).first.count():
            page.get_by_label("同意する", exact=True).first.check(); pass_mark("同意確認", "同意する")
        nx = page.get_by_role("button", name="次へ")
        if nx.first.count():
            nx.first.click(); page.wait_for_load_state("domcontentloaded")
        info(f"到達5: {page.url}")

        ok = on_area_date(page)
        if ok: pass_mark("導線", "手順どおり到達")
        else:  warn_mark("導線", "エリア・日程に未到達")
        return ok
    finally:
        group_end()

# ===== メイン =====
def main():
    found_lines = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            # --- ログイン ---
            group_start("IPAログイン")
            page.goto(IPA_LOGIN_URL, wait_until="domcontentloaded")
            check(page.locator("form").first.count() > 0, "ログインページ", "フォーム検出", "フォーム見当たらず", True)

            try:
                page.get_by_label("利用者ID", exact=True).fill(IPA_USER_ID, timeout=3000)
                pass_mark("ID入力", "label=利用者ID")
            except Exception:
                fill_any(page, LOGIN_ID_CAND, IPA_USER_ID, "ID入力")

            try:
                page.get_by_label("パスワード", exact=True).fill(IPA_PASSWORD, timeout=3000)
                pass_mark("PW入力", "label=パスワード")
            except Exception:
                fill_any(page, LOGIN_PW_CAND, IPA_PASSWORD, "PW入力")

            if page.get_by_role("button", name="ログイン").first.count():
                page.get_by_role("button", name="ログイン").first.click()
            else:
                page.locator("button:has-text('ログイン'), input[type='submit']").first.click()
            page.wait_for_load_state("domcontentloaded")

            logged_in = page.locator("a:has-text('ログアウト'), button:has-text('ログアウト')").first.count() > 0
            check(logged_in, "ログイン", "成功", "失敗の可能性", True)
            group_end()

            # --- エリア・日程選択ページへ ---
            ok = goto_area_date_page(page)
            check(ok, "導線確認", "エリア・日程選択に到達", "ページ到達に失敗", True)

            # --- エリア・日程選択（select取得＋2秒ディレイ） ---
            group_start("エリア/日程選択")

            def row_select(row_label: str):
                row = page.locator("tr").filter(has_text=row_label)
                if not row.count():
                    fail_mark("選択行", f"'{row_label}' 行なし"); return None
                sel = row.first.locator("select").first
                if not sel.count():
                    fail_mark("選択UI", f"'{row_label}' に select なし"); return None
                sel.scroll_into_view_if_needed()
                sel.wait_for(state="visible", timeout=10000)
                return sel

            def select_label(sel, label: str) -> bool:
                if not sel: return False
                try:
                    sel.select_option(label=label, timeout=30000)
                    pass_mark("選択", f"{label}")
                    return True
                except Exception as e:
                    fail_mark("選択", f"'{label}' 選択失敗 ({e})")
                    return False

            # 1) 地域 → 2秒待つ（都道府県候補が反映されるまで）
            region_sel = row_select("地域")
            select_label(region_sel, REGION_NAME)
            page.wait_for_timeout(2000)

            # 2) 都道府県（DOMが差し替わる可能性があるので取り直す）
            pref_sel = row_select("都道府県")  # 取り直し
            select_label(pref_sel, PREF_NAME)

            # 3) 月・日 select を取得
            month_sel = row_select("月")
            day_sel   = row_select("日")
            check(bool(month_sel and day_sel), "セレクト取得", "月/日を取得", "月/日セレクトが無い", True)

            # START_YM 以降の月に絞る
            sy, sm = map(int, START_YM.split("-"))
            month_opts = []
            for i in range(month_sel.locator("option").count()):
                lb = (month_sel.locator("option").nth(i).inner_text() or "").strip()
                pm = parse_month_label(lb)
                if pm and ((pm[0] > sy) or (pm[0] == sy and pm[1] >= sm)):
                    month_opts.append(lb)
            if not month_opts:
                warn_mark("月", f"{START_YM} 以降の候補なし")

            # 日レンジ（プレースホルダ除外）
            day_opts = []
            for i in range(day_sel.locator("option").count()):
                lb = (day_sel.locator("option").nth(i).inner_text() or "").strip()
                if "選択" in lb or lb == "":
                    continue
                day_opts.append((i, lb))
            if not day_opts:
                warn_mark("日", "有効な日レンジが見つからない")

            group_end()

            # --- 検索・抽出ループ ---
            group_start("検索・抽出ループ")

            def click_search() -> bool:
                btn = page.get_by_role("button", name="検索").first
                if btn.count():
                    btn.click(); page.wait_for_load_state("domcontentloaded")
                    pass_mark("会場検索", "検索押下"); return True
                warn_mark("会場検索", "ボタンなし"); return False

            def extract_table_slots(selected_month: str, selected_day: str):
                tables = page.locator("table")
                if tables.count() == 0:
                    warn_mark("会場表", "tableなし"); return
                rows = tables.first.locator("tr")
                matched = 0
                for i in range(rows.count()):
                    r = rows.nth(i)
                    name = ""
                    if r.locator("a").count():
                        name = (r.locator("a").first.inner_text() or "").strip()
                    else:
                        try: name = (r.locator("td").first.inner_text() or "").strip()
                        except Exception: name = ""
                    if not name or not any(c in name for c in TARGET_CENTERS): continue

                    matched += 1; pass_mark("会場一致", name)
                    cells = r.locator("a:has-text('○'), button:has-text('○'), td:has-text('○')")
                    cnt = cells.count()
                    if cnt == 0:
                        warn_mark("枠抽出", f"{name}: 0件"); continue
                    for j in range(cnt):
                        t = (cells.nth(j).inner_text() or "").strip()
                        href = ""
                        try: href = cells.nth(j).get_attribute("href") or ""
                        except: pass
                        line = f"{name} | {selected_month} | {selected_day} | {t}"
                        if href: line += f" | {href}"
                        found_lines.append(line)
                    pass_mark("枠抽出", f"{name}: {cnt}件")
                if matched == 0: warn_mark("会場一致", "指定会場ヒットなし（表記ぶれの可能性）")

            # 総当たり：月 × 日
            loop_months = month_opts if month_opts else [""]
            loop_days   = day_opts   if day_opts   else [(1, "任意")]

            for m_lb in loop_months:
                if m_lb:
                    try: month_sel.select_option(label=m_lb); pass_mark("月選択", m_lb)
                    except Exception as e: warn_mark("月選択", f"'{m_lb}' 選択失敗: {e}"); continue
                for day_index, day_lb in loop_days:
                    try: day_sel.select_option(index=day_index); pass_mark("日選択", day_lb)
                    except Exception as e: warn_mark("日選択", f"'{day_lb}' 選択失敗: {e}"); continue
                    if click_search(): extract_table_slots(m_lb or "(指定なし)", day_lb)

            group_end()

            # --- ログアウト（任意） ---
            group_start("ログアウト")
            lg = page.locator("a:has-text('ログアウト'), button:has-text('ログアウト')").first
            if lg.count():
                lg.click(); page.wait_for_load_state("domcontentloaded"); pass_mark("ログアウト", "成功")
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
        body = f"対象: 地域={REGION_NAME} / 都道府県={PREF_NAME} / 開始月={START_YM}\n\n" + "\n".join(found_lines)
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
