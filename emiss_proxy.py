#!/usr/bin/env python3
"""
emiss_proxy.py — локальный прокси для ЕМИСС конфигуратора.
Использует fedstatAPIr::fedstat_get_data_ids() для получения фильтров,
fedstatAPIr::fedstat_data_load_with_filters() для загрузки данных.

Запуск:  python emiss_proxy.py
Браузер: http://127.0.0.1:8765/
"""
import json, os, subprocess, sys, tempfile, threading, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT      = 8765
DIR       = Path(__file__).parent
HTML_FILE = DIR / "emiss-configurator.html"
CACHE_DIR = DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)


# ─── R helpers ────────────────────────────────────────────────────────────────

def find_rscript():
    import shutil
    for c in [None, "/usr/bin/Rscript", "/usr/local/bin/Rscript"]:
        r = shutil.which("Rscript") if c is None else (c if Path(c).exists() else None)
        if r: return r
    return None

def run_r(code: str, timeout: int = 120):
    rscript = find_rscript()
    if not rscript:
        return 1, "", "Rscript не найден"
    with tempfile.NamedTemporaryFile(suffix=".R", mode="w", encoding="utf-8", delete=False) as f:
        f.write(code); tmp = f.name
    try:
        p = subprocess.run(
            [rscript, "--vanilla", tmp],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Timeout {timeout}s"
    finally:
        os.unlink(tmp)


# ─── R: получить фильтры через fedstat_get_data_ids() ────────────────────────
# Возвращает JSON:
# { indicator_id, filters: [ {field_title, values:[{title},...] }, ... ] }
# НЕТ числовых id — только titles, как ожидает fedstatAPIr.

R_GET_FILTERS = """\
suppressPackageStartupMessages({{
  library(fedstatAPIr); library(data.table); library(jsonlite)
}})
iid      <- "{indicator_id}"
out_json <- "{out_json}"

cat("[R] fedstat_get_data_ids", iid, "\\n")
data_ids <- tryCatch(
  fedstat_get_data_ids(iid),
  error = function(e) {{ cat(stderr(), "[ERROR]", conditionMessage(e), "\\n"); quit(status=1) }}
)

# data_ids — data.frame с колонками:
# filter_field_id, filter_field_title, filter_value_id, filter_value_title
setDT(data_ids)
cat(sprintf("[R] data_ids: %d строк, поля: %s\\n",
    nrow(data_ids), paste(names(data_ids), collapse=", ")))

fields <- unique(data_ids$filter_field_title)
cat(sprintf("[R] Полей: %d: %s\\n", length(fields), paste(fields, collapse=" | ")))

filters_list <- list()
for (fld in fields) {{
  sub <- data_ids[filter_field_title == fld]
  # data.frame гарантирует сериализацию как JSON-массив даже при 1 строке
  vals_df <- data.frame(
    id    = sub$filter_value_id,
    title = sub$filter_value_title,
    stringsAsFactors = FALSE
  )
  filters_list[[length(filters_list)+1]] <- list(
    field_title = fld,
    values      = vals_df
  )
}}

# dataframe="rows" сериализует каждую строку как объект {{id,title}}
# always -> filters всегда массив даже если 1 фильтр
writeLines(toJSON(list(
  indicator_id = iid,
  filters      = filters_list
), auto_unbox=TRUE, dataframe="rows", pretty=TRUE), out_json)
cat("[R] dict OK\\n")
"""


# ─── R: загрузка данных ───────────────────────────────────────────────────────
# config.filters ключи = field_title (строки)
# config.filters[field_title].selected = ["*"] | ["title1", "title2", ...]
# Год управляется через period: year_from..year_to

R_DOWNLOAD = """\
suppressPackageStartupMessages({{
  library(fedstatAPIr); library(data.table); library(jsonlite)
}})
iid        <- "{indicator_id}"
output_csv <- "{output_csv}"
config     <- fromJSON('{config_json}', simplifyVector=FALSE)

# Шаг 1: получаем доступные года из data_ids
cat("[R] fedstat_get_data_ids", iid, "\\n")
data_ids <- tryCatch(
  fedstat_get_data_ids(iid),
  error = function(e) {{ cat(stderr(), "[ERROR]", conditionMessage(e), "\\n"); quit(status=1) }}
)
setDT(data_ids)
cat(sprintf("[R] Поля: %s\\n", paste(unique(data_ids$filter_field_title), collapse=" | ")))

# Шаг 2: строим filters list с TITLES в качестве ключей
filters <- list()

# 2a. Год — из period
year_from <- as.integer(config$period$year_from)
yr_raw    <- config$period$year_to
year_to   <- if (is.null(yr_raw) || identical(yr_raw, "today")) {{
  as.integer(format(Sys.Date(), "%Y"))
}} else {{
  as.integer(yr_raw)
}}
years_wanted <- as.character(seq(year_from, year_to))
cat(sprintf("[R] Запрошены годы: %s\\n", paste(years_wanted, collapse=", ")))

# Найти правильное название поля "Год" в data_ids
year_field <- data_ids[grepl("^[Гг]од", filter_field_title, perl=TRUE), unique(filter_field_title)]
if (length(year_field) == 0) {{
  # Если нет поля "Год" — попробуем "Период"
  year_field <- data_ids[grepl("[Пп]ериод", filter_field_title, perl=TRUE), unique(filter_field_title)]
}}

if (length(year_field) > 0) {{
  year_field <- year_field[1]
  avail_years <- data_ids[filter_field_title == year_field, filter_value_title]
  sel_years   <- years_wanted[years_wanted %in% avail_years]
  if (length(sel_years) == 0) {{
    cat(sprintf("[R][WARN] Ни один год из [%s] не найден в [%s], берём все\\n",
        paste(years_wanted, collapse=","), paste(avail_years, collapse=",")))
    sel_years <- avail_years
  }}
  filters[[year_field]] <- sel_years
  cat(sprintf("[R] %s -> %s\\n", year_field, paste(sel_years, collapse=", ")))
}} else {{
  cat("[R][WARN] Поле года не найдено в data_ids\\n")
}}

# 2b. Остальные фильтры из config$filters
cfg_filters <- config$filters
for (fld in names(cfg_filters)) {{
  fc  <- cfg_filters[[fld]]
  sel <- unlist(fc[["selected"]])

  # Проверяем, что поле существует в data_ids
  if (!fld %in% data_ids$filter_field_title) {{
    cat(sprintf("[R][WARN] Поле '%s' не найдено в data_ids, пропускаем\\n", fld))
    next
  }}

  if ("*" %in% sel || length(sel) == 0) {{
    filters[[fld]] <- "*"
    cat(sprintf("[R] %s -> *\\n", fld))
  }} else {{
    avail <- data_ids[filter_field_title == fld, filter_value_title]
    valid <- sel[sel %in% avail]
    if (length(valid) == 0) {{
      cat(sprintf("[R][WARN] Значения [%s] не найдены для поля '%s', берём *\\n",
          paste(sel, collapse=","), fld))
      filters[[fld]] <- "*"
    }} else {{
      filters[[fld]] <- valid
      cat(sprintf("[R] %s -> %s\\n", fld, paste(valid, collapse=" | ")))
    }}
  }}
}}

cat(sprintf("[R] Итого фильтров: %d\\n", length(filters)))
for (nm in names(filters)) {{
  v <- filters[[nm]]
  if (identical(v, "*")) cat(sprintf("  %-40s = *\\n", nm))
  else cat(sprintf("  %-40s = [%s]\\n", nm, paste(v, collapse=", ")))
}}

# Шаг 3: загрузка
cat("[R] fedstat_data_load_with_filters...\\n")
result <- tryCatch(
  fedstat_data_load_with_filters(iid, filters=filters),
  error = function(e) {{ cat(stderr(), "[ERROR]", conditionMessage(e), "\\n"); quit(status=1) }}
)
setDT(result)
cat(sprintf("[R] Получено: %d строк x %d колонок\\n", nrow(result), ncol(result)))

fwrite(result, output_csv, bom=TRUE)
cat(sprintf("[R] Сохранено: %s\\n", output_csv))
"""


# ─── Кэш фильтров ────────────────────────────────────────────────────────────

def get_filters(indicator_id: str, force: bool = False):
    """Возвращает (data | None, error_str)."""
    cache = CACHE_DIR / f"{indicator_id}_filters.json"
    if cache.exists() and not force:
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("filters"):
                n = len(d["filters"])
                print(f"[CACHE] {indicator_id}: {n} фильтров")
                return d, ""
        except Exception:
            pass

    out = str(cache.resolve()).replace("\\", "/")
    code = R_GET_FILTERS.format(indicator_id=indicator_id, out_json=out)
    print(f"[R] Загружаем фильтры {indicator_id}...")
    rc, stdout, stderr = run_r(code, timeout=60)
    print(stdout.strip())
    if stderr.strip(): print("[STDERR]", stderr.strip())
    if rc != 0:
        return None, (stderr.strip() or stdout.strip() or "R error")
    try:
        return json.loads(cache.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, str(e)


# ─── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print(f"[HTTP] {fmt % args}")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(body)

    def _bytes(self, data, ctype, filename):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p    = urlparse(self.path)
        path = p.path.rstrip("/")
        qs   = parse_qs(p.query)

        if path in ("", "/"):
            if HTML_FILE.exists():
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            else:
                self._json({"error": "emiss-configurator.html не найден"}, 404)
            return

        if path == "/api/status":
            self._json({"r_available": bool(find_rscript()),
                        "rscript": find_rscript() or "not found"})
            return

        if path == "/api/indicator" or path.startswith("/api/indicator/"):
            iid   = path.split("/api/indicator/", 1)[1].strip("/") \
                    if "/api/indicator/" in path else qs.get("id", [""])[0].strip()
            force = "force" in qs
            if not iid:
                self._json({"error": "id обязателен"}, 400); return
            data, err = get_filters(iid, force=force)
            if err: self._json({"error": err}, 502)
            else:   self._json(data)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path   = urlparse(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if path == "/api/download":
            try:
                self._download(json.loads(body))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/save_config":
            try:
                cfg = json.loads(body)
                iid = str(cfg.get("indicator_id", "unknown"))
                out = DIR / f"config_{iid}.json"
                out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                self._json({"saved": str(out)})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        self._json({"error": "not found"}, 404)

    def _download(self, payload):
        iid    = str(payload.get("indicator_id", ""))
        config = payload.get("config", {})
        if not iid:
            self._json({"error": "indicator_id обязателен"}, 400); return

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            out_csv = f.name

        # Экранируем одинарные кавычки в JSON для встраивания в R-строку
        cfg_json = json.dumps(config, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")

        code = R_DOWNLOAD.format(
            indicator_id=indicator_id_from(iid),
            config_json=cfg_json,
            output_csv=out_csv.replace("\\", "/"),
        )

        print(f"[R] Загружаем данные {iid}...")
        rc, stdout, stderr = run_r(code, timeout=300)
        print(stdout.strip())
        if stderr.strip(): print("[STDERR]", stderr.strip())

        if rc != 0 or not Path(out_csv).exists():
            try: os.unlink(out_csv)
            except: pass
            msg = "\n".join(l for l in (stderr + "\n" + stdout).splitlines()
                            if "[ERROR]" in l or "Error" in l)
            self._json({"error": msg or "R ошибка"}, 502)
            return

        try:
            data = Path(out_csv).read_bytes()
            self._bytes(data, "text/csv; charset=utf-8", f"emiss_{iid}.csv")
        finally:
            try: os.unlink(out_csv)
            except: pass

def indicator_id_from(s):
    """Гарантируем строку-число без пробелов."""
    return str(s).strip()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    rs = find_rscript()
    if rs:
        print(f"[OK] Rscript: {rs}")
    else:
        print("[WARN] Rscript не найден!\n"
              "  sudo apt install r-base\n"
              "  Rscript -e 'install.packages(c(\"fedstatAPIr\",\"data.table\",\"jsonlite\"))'")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[START] http://127.0.0.1:{PORT}/  (Ctrl+C — стоп)")
    try: webbrowser.open(f"http://127.0.0.1:{PORT}/")
    except: pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP]")

if __name__ == "__main__":
    main()
