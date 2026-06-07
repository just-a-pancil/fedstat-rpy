#!/usr/bin/env python3
"""
emiss_download.py — CLI загрузка данных ЕМИСС через fedstatAPIr.

Использование:
  python emiss_download.py 62309 config_62309.json
  python emiss_download.py 62309 config_62309.json --output result.csv
  python emiss_download.py 62309 --list-filters

Формат конфига (генерируется emiss-configurator.html):
{
  "indicator_id": "62309",
  "period": { "year_from": 2024, "year_to": 2026 },
  "filters": {
    "видам продукции": { "selected": ["Алкогольная продукция...", "Пиво..."] },
    "ОКАТО (межведомственный)": { "selected": ["*"] }
  }
}

Требования:
  R >= 4.0
  Rscript -e 'install.packages(c("fedstatAPIr","data.table","jsonlite"))'
"""
import argparse, json, os, subprocess, sys, tempfile
from datetime import datetime
from pathlib import Path


# ─── R: список фильтров ───────────────────────────────────────────────────────

R_LIST = """\
suppressPackageStartupMessages({{
  library(fedstatAPIr); library(data.table)
}})
iid <- "{indicator_id}"
cat(sprintf("Загружаем фильтры для %s ...\\n", iid))

data_ids <- tryCatch(
  fedstat_get_data_ids(iid),
  error = function(e) {{ cat("[ERROR]", conditionMessage(e), "\\n"); quit(status=1) }}
)
setDT(data_ids)

cat(sprintf("\\n=== Индикатор %s ===\\n", iid))
for (fld in unique(data_ids$filter_field_title)) {{
  sub <- data_ids[filter_field_title == fld]
  cat(sprintf("\\n[%s] (%d значений)\\n", fld, nrow(sub)))
  for (i in seq_len(min(nrow(sub), 30))) {{
    cat(sprintf("  %s\\n", sub$filter_value_title[i]))
  }}
  if (nrow(sub) > 30) cat(sprintf("  ... ещё %d\\n", nrow(sub) - 30))
}}
"""


# ─── R: загрузка данных ───────────────────────────────────────────────────────

R_DOWNLOAD = """\
suppressPackageStartupMessages({{
  library(fedstatAPIr); library(data.table); library(jsonlite)
}})
iid        <- "{indicator_id}"
output_csv <- "{output_csv}"
config     <- fromJSON('{config_json}', simplifyVector=FALSE)

# 1. Получаем доступные значения через fedstat_get_data_ids
cat(sprintf("[R] fedstat_get_data_ids(%s)...\\n", iid))
data_ids <- tryCatch(
  fedstat_get_data_ids(iid),
  error = function(e) {{ cat("[ERROR]", conditionMessage(e), "\\n"); quit(status=1) }}
)
setDT(data_ids)
all_fields <- unique(data_ids$filter_field_title)
cat(sprintf("[R] Поля: %s\\n", paste(all_fields, collapse=" | ")))

# 2. Строим filters list (ключи = filter_field_title, значения = filter_value_title)
filters <- list()

# 2a. Год — из config$period
year_from    <- as.integer(config$period$year_from)
year_to_raw  <- config$period$year_to
year_to      <- if (is.null(year_to_raw) || identical(year_to_raw, "today")) {{
  as.integer(format(Sys.Date(), "%Y"))
}} else {{
  as.integer(year_to_raw)
}}
years_wanted <- as.character(seq(year_from, year_to))
cat(sprintf("[R] Запрошены годы: %s\\n", paste(years_wanted, collapse=", ")))

# Найти поле года (начинается на Год или содержит Период)
year_field <- all_fields[grepl("^[Гг]од", all_fields, perl=TRUE)]
if (length(year_field) == 0)
  year_field <- all_fields[grepl("[Пп]ериод", all_fields, perl=TRUE)]

if (length(year_field) > 0) {{
  year_field   <- year_field[1]
  avail_years  <- data_ids[filter_field_title == year_field, filter_value_title]
  sel_years    <- years_wanted[years_wanted %in% avail_years]
  if (length(sel_years) == 0) {{
    cat(sprintf("[WARN] Годы [%s] не найдены, берём все доступные\\n",
        paste(years_wanted, collapse=",")))
    sel_years <- avail_years
  }}
  filters[[year_field]] <- sel_years
  cat(sprintf("[R] %-40s = %s\\n", year_field, paste(sel_years, collapse=", ")))
}} else {{
  cat("[WARN] Поле года не найдено в data_ids\\n")
}}

# 2b. Остальные фильтры из config$filters
for (fld in names(config$filters)) {{
  fc  <- config$filters[[fld]]
  sel <- unlist(fc[["selected"]])

  # Пропускаем поля года — они управляются через period
  if (grepl("^[Гг]од", fld, perl=TRUE) || grepl("[Пп]ериод", fld, perl=TRUE)) {{
    cat(sprintf("[R] Пропускаем поле года: %s\\n", fld))
    next
  }}

  # Проверяем что поле есть в data_ids
  if (!fld %in% all_fields) {{
    cat(sprintf("[WARN] Поле '%s' не найдено в data_ids, пропускаем\\n", fld))
    next
  }}

  if ("*" %in% sel || length(sel) == 0) {{
    filters[[fld]] <- "*"
    cat(sprintf("[R] %-40s = *\\n", fld))
  }} else {{
    avail <- data_ids[filter_field_title == fld, filter_value_title]
    valid <- sel[sel %in% avail]
    if (length(valid) == 0) {{
      cat(sprintf("[WARN] Ни одно значение не найдено для '%s', берём *\\n", fld))
      filters[[fld]] <- "*"
    }} else {{
      if (length(valid) < length(sel)) {{
        missing <- sel[!sel %in% avail]
        cat(sprintf("[WARN] Не найдены в data_ids: %s\\n", paste(missing, collapse="; ")))
      }}
      filters[[fld]] <- valid
      cat(sprintf("[R] %-40s = [%s]\\n", fld, paste(valid, collapse="; ")))
    }}
  }}
}}

cat(sprintf("[R] Итого фильтров: %d\\n", length(filters)))

# 3. Загружаем данные
cat("[R] fedstat_data_load_with_filters...\\n")
result <- tryCatch(
  fedstat_data_load_with_filters(iid, filters=filters),
  error = function(e) {{ cat("[ERROR]", conditionMessage(e), "\\n"); quit(status=1) }}
)
setDT(result)
cat(sprintf("[R] Получено: %d строк x %d колонок\\n", nrow(result), ncol(result)))

# 4. Сохраняем
fwrite(result, output_csv, bom=TRUE)
cat(sprintf("[R] Сохранено: %s\\n", output_csv))
"""


# ─── helpers ──────────────────────────────────────────────────────────────────

def find_rscript() -> str:
    import shutil
    r = shutil.which("Rscript")
    if r:
        return r
    for p in ["/usr/bin/Rscript", "/usr/local/bin/Rscript", "/opt/R/bin/Rscript"]:
        if Path(p).exists():
            return p
    sys.exit("[ERROR] Rscript не найден. Установите R:\n  sudo apt install r-base")


def run_r(code: str, timeout: int = 300) -> int:
    """Запускает R-код, стримит вывод в stdout, возвращает returncode."""
    rscript = find_rscript()
    with tempfile.NamedTemporaryFile(suffix=".R", mode="w", encoding="utf-8", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        proc = subprocess.Popen(
            [rscript, "--vanilla", tmp],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8"
        )
        for line in proc.stdout:
            print(line, end="", flush=True)
        proc.wait(timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        sys.exit(f"[ERROR] R завис (timeout {timeout}s)")
    finally:
        os.unlink(tmp)


# ─── команды ──────────────────────────────────────────────────────────────────

def cmd_list(indicator_id: str):
    code = R_LIST.format(indicator_id=indicator_id)
    rc = run_r(code)
    if rc != 0:
        sys.exit(f"[ERROR] R завершился с кодом {rc}")


def cmd_download(indicator_id: str, config_path: str, output: str | None):
    cfg_text = Path(config_path).read_text(encoding="utf-8")
    config   = json.loads(cfg_text)

    # Берём indicator_id из конфига если явно не задан
    if not indicator_id:
        indicator_id = str(config.get("indicator_id", ""))
    if not indicator_id:
        sys.exit("[ERROR] indicator_id не задан ни в аргументах, ни в конфиге")

    if not output:
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = f"emiss_{indicator_id}_{ts}.csv"
    output = str(Path(output).resolve())

    # Экранирование для встраивания JSON в R-строку в одинарных кавычках
    cfg_json = json.dumps(config, ensure_ascii=False) \
                   .replace("\\", "\\\\") \
                   .replace("'",  "\\'")

    code = R_DOWNLOAD.format(
        indicator_id=indicator_id,
        config_json=cfg_json,
        output_csv=output.replace("\\", "/"),
    )

    print(f"[INFO] Индикатор : {indicator_id}")
    print(f"[INFO] Конфиг    : {config_path}")
    print(f"[INFO] Выход     : {output}")
    print(f"[INFO] Период    : {config.get('period', {})}")
    print()

    rc = run_r(code)
    if rc != 0:
        sys.exit(f"[ERROR] R завершился с кодом {rc}")

    if Path(output).exists():
        size = Path(output).stat().st_size
        print(f"\n[OK] Сохранено: {output}  ({size:,} байт)")
    else:
        sys.exit("[ERROR] CSV не создан — проверьте вывод R выше")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="emiss_download.py",
        description="Загрузка данных ЕМИСС через R-пакет fedstatAPIr",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python emiss_download.py 62309 config_62309.json\n"
            "  python emiss_download.py 62309 config_62309.json --output data.csv\n"
            "  python emiss_download.py 62309 --list-filters\n"
        ),
    )
    ap.add_argument("indicator",        help="ID индикатора ЕМИСС (например 62309)")
    ap.add_argument("config", nargs="?",help="Путь к JSON-конфигу (из emiss-configurator)")
    ap.add_argument("--output", "-o",   help="Имя выходного CSV (по умолчанию: emiss_ID_TIMESTAMP.csv)")
    ap.add_argument("--list-filters", "-l", action="store_true", dest="lf",
                    help="Показать все доступные фильтры и их значения")

    args = ap.parse_args()

    if args.lf:
        cmd_list(args.indicator)
        return

    if not args.config:
        ap.error("Укажите путь к конфигу или используйте --list-filters")

    if not Path(args.config).is_file():
        sys.exit(f"[ERROR] Файл не найден: {args.config}")

    cmd_download(args.indicator, args.config, args.output)


if __name__ == "__main__":
    main()
