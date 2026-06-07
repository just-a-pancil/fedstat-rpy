#!/usr/bin/env python3
"""
emiss_diag.py — диагностика fedstat.ru
Запустите: python emiss_diag.py
Покажет точно где ломается цепочка.
"""
import json, re, sys, time
from pathlib import Path

print("=" * 60)
print("ЕМИСС Диагностика")
print("=" * 60)

# ── Шаг 1: импорты ────────────────────────────────────────────
print("\n[1] Проверка зависимостей...")
try:
    import requests
    print(f"  ✓ requests {requests.__version__}")
except ImportError:
    print("  ✗ requests не установлен: uv add requests")

try:
    from playwright.sync_api import sync_playwright
    print("  ✓ playwright")
except ImportError:
    sys.exit("  ✗ playwright не установлен: uv add playwright && uvx playwright install chromium")

# ── Шаг 2: Playwright открывает страницу ──────────────────────
IID = "62309"
URL = f"https://www.fedstat.ru/indicator/{IID}"

print(f"\n[2] Playwright: открываю {URL}")
print("    (это займёт ~15-30 сек, ждите...)")

html = ""
scripts = []
cookies = []
page_title = ""
page_status = None
console_errors = []
network_errors = []

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        page.on("console", lambda m: console_errors.append(f"[{m.type}] {m.text}") if m.type in ("error","warning") else None)
        page.on("pageerror", lambda e: network_errors.append(f"[pageerror] {e}"))

        resp = page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page_status = resp.status if resp else None
        page_title  = page.title()
        print(f"  HTTP status: {page_status}")
        print(f"  Заголовок:   {page_title!r}")

        # Ждём загрузки JS
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except:
            print("  ⚠ networkidle timeout (продолжаем)")
        time.sleep(2)

        # Безопасное получение HTML
        try:
            html = page.evaluate("() => document.documentElement.outerHTML")
            print(f"  HTML длина: {len(html):,} символов")
        except Exception as e:
            print(f"  ✗ html evaluate: {e}")

        # Безопасное получение скриптов
        try:
            scripts = page.evaluate(
                "() => Array.from(document.querySelectorAll('script')).map(s => s.textContent||'')"
            )
            print(f"  Скриптов найдено: {len(scripts)}")
            total_js = sum(len(s) for s in scripts)
            print(f"  Суммарный JS: {total_js:,} символов")
        except Exception as e:
            print(f"  ✗ scripts evaluate: {e}")

        cookies = ctx.cookies()
        print(f"  Куков: {len(cookies)}")
        browser.close()

except Exception as e:
    print(f"  ✗ Playwright ошибка: {e}")
    sys.exit(1)

if console_errors:
    print(f"\n  Консольные ошибки ({len(console_errors)}):")
    for e in console_errors[:5]: print(f"    {e}")

# ── Шаг 3: Анализ HTML ────────────────────────────────────────
print(f"\n[3] Анализ HTML...")

# Признаки блокировки
if page_status in (403, 429, 503):
    print(f"  ✗ Сервер вернул {page_status} — возможна блокировка")
elif page_status == 200:
    print(f"  ✓ HTTP 200")

# Проверяем что страница не пустая / captcha
if len(html) < 5000:
    print(f"  ✗ HTML слишком короткий ({len(html)} символов) — возможно капча или редирект")
    print(f"  Первые 1000 символов HTML:\n{html[:1000]}")
elif "Forbidden" in html or "captcha" in html.lower():
    print("  ✗ В HTML обнаружена блокировка/капча")
    idx = html.lower().find("captcha")
    if idx < 0: idx = html.find("Forbidden")
    print(f"  Фрагмент: {html[max(0,idx-100):idx+200]}")
else:
    print(f"  ✓ HTML выглядит нормально")

# Есть ли h1 с названием индикатора?
m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S | re.I)
if m:
    title_text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    print(f"  ✓ h1 найден: {title_text[:80]!r}")
else:
    print("  ✗ h1 не найден — страница, возможно, не загрузилась")

# DataRange
dr = re.search(r'start="(\d{4})"[^>]*end="(\d{4})"', html, re.I)
if dr:
    print(f"  ✓ DataRange: {dr.group(1)}–{dr.group(2)}")
else:
    print("  ⚠ DataRange не найден в HTML")

# ── Шаг 4: Анализ скриптов ───────────────────────────────────
print(f"\n[4] Анализ JS-скриптов...")

found_filters = False
for i, s in enumerate(scripts):
    has_f  = bool(re.search(r'filters\s*:', s))
    has_lc = bool(re.search(r'left_columns\s*:', s))
    has_gi = "grid.init" in s

    if has_f or has_lc or has_gi:
        print(f"  Скрипт #{i:2d}: len={len(s):6,}  filters={'✓' if has_f else '·'}  left_columns={'✓' if has_lc else '·'}  grid.init={'✓' if has_gi else '·'}")

    if has_f:
        found_filters = True
        # Пробуем распарсить
        m2 = re.search(r'filters\s*:\s*', s)
        if m2:
            # Извлечь блок
            depth, start, result_block = 0, None, ""
            for j in range(m2.end()-1, len(s)):
                ch = s[j]
                if start is None:
                    if ch == '{': start = j; depth = 1
                    continue
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        result_block = s[start:j+1]
                        break

            if result_block:
                print(f"    → Блок filters: {len(result_block)} символов")
                print(f"    → Первые 400 символов:")
                print("      " + result_block[:400].replace('\n', '\n      '))

                # Попытка JSON
                cleaned = result_block
                cleaned = re.sub(r"//[^\n]*", "", cleaned)
                cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
                cleaned = cleaned.replace("'", '"')
                cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
                try:
                    parsed = json.loads(cleaned)
                    keys = [k for k in parsed.keys() if k != "0"]
                    print(f"    ✓ JSON распарсен! field_id ключи: {keys[:10]}")
                    for k in keys[:3]:
                        v = parsed[k]
                        print(f"      [{k}] title={v.get('title','?')!r}  values={len(v.get('values',{}))}")
                except Exception as e:
                    print(f"    ✗ JSON ошибка: {e}")
                    print(f"    → cleaned (первые 300):\n      {cleaned[:300]}")

if not found_filters:
    print("  ✗ Ни в одном скрипте нет 'filters:' !")
    print("\n  Скрипты по длине (топ-5):")
    indexed = sorted(enumerate(scripts), key=lambda x: len(x[1]), reverse=True)
    for i, s in indexed[:5]:
        print(f"    #{i}: {len(s):,} символов  head: {s[:100]!r}")

# ── Шаг 5: requests с куками ──────────────────────────────────
print(f"\n[5] Тест requests.Session с куками Playwright...")
try:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.fedstat.ru",
    })
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ".fedstat.ru"))
    r2 = sess.get(f"https://www.fedstat.ru/indicator/{IID}", timeout=20)
    print(f"  requests status: {r2.status_code}")
    print(f"  requests HTML длина: {len(r2.text):,}")
    if r2.status_code == 200 and len(r2.text) > 5000:
        print("  ✓ requests с куками работает!")
    else:
        print("  ✗ requests вернул короткий ответ или не 200")
except Exception as e:
    print(f"  ✗ requests ошибка: {e}")

# ── Итог ──────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Готово. Скопируйте весь вывод выше и пришлите.")
print("=" * 60)

# Сохраняем HTML для анализа
out = Path("diag_page.html")
out.write_text(html, encoding="utf-8")
print(f"\nHTML страницы сохранён в: {out.absolute()}")
if scripts:
    out2 = Path("diag_scripts.txt")
    out2.write_text("\n\n---SCRIPT---\n\n".join(scripts), encoding="utf-8")
    print(f"Скрипты сохранены в:     {out2.absolute()}")
