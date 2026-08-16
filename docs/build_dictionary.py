"""Сборка словаря таблиц и колонок из метаданных dbt.

Словарь не пишется руками. Руками пишутся описания - в тех же yml, где стоят
проверки, рядом с колонкой, которую описывают. Отсюда следует главное его
свойство: он не может отстать от схемы, потому что собирается из нее.

Скрипт падает, если хоть у одной колонки нет описания. Это сделано намеренно:
словарь с дырами хуже отсутствующего - по нему принимают решения, считая, что
он полон. Пустая клетка в таблице читается как "тут нечего сказать", а значит
надо либо сказать, либо убрать колонку.

Читает dbt/target/catalog.json и manifest.json, пишет DICTIONARY.md.
Запуск: make dictionary
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "dbt" / "target"
OUTPUT = ROOT / "DICTIONARY.md"

# Порядок слоев - порядок движения данных, а не алфавит.
SCHEMA_ORDER = ["raw", "staging", "intermediate", "marts", "ops"]

SCHEMA_TITLES = {
    "raw": ("Сырой слой", "Выгрузка источника как она есть, вместе с дефектами. "
                          "Первичных ключей и ограничений уникальности здесь нет намеренно."),
    "staging": ("Слой staging", "Приведение формы: типы, имена, зерно. Ничего не считает "
                                "и не чинит. Материализован представлениями."),
    "intermediate": ("Промежуточный слой", "Сведение источников. Оставляет видимым все, "
                                           "включая сироты, чтобы гейту было что ловить."),
    "marts": ("Витрины", "То, что читает человек и дашборд."),
    "ops": ("Служебное", "Механика, на которой стоит журнал ревизий. "
                         "Читать руками не нужно."),
}


def load() -> tuple[dict, dict]:
    catalog = TARGET / "catalog.json"
    manifest = TARGET / "manifest.json"
    if not catalog.exists() or not manifest.exists():
        print("Нет метаданных dbt. Сначала: make docs")
        raise SystemExit(1)
    return json.loads(catalog.read_text()), json.loads(manifest.read_text())


def descriptions(manifest: dict) -> dict[str, dict]:
    """Описания из yml: по узлу - описание объекта и словарь описаний колонок."""
    out = {}
    for kind in ("nodes", "sources"):
        for key, node in manifest.get(kind, {}).items():
            out[key] = {
                "description": (node.get("description") or "").strip(),
                "columns": {
                    name.lower(): (col.get("description") or "").strip()
                    for name, col in (node.get("columns") or {}).items()
                },
            }
    return out


def clean(text: str) -> str:
    """Многострочное описание из yml - в одну строку: словарь читают таблицей."""
    return " ".join(text.split())


def main() -> int:
    catalog, manifest = load()
    docs = descriptions(manifest)

    objects = []
    for kind in ("sources", "nodes"):
        for key, node in catalog.get(kind, {}).items():
            meta = node["metadata"]
            objects.append({
                "key": key,
                "schema": meta["schema"],
                "name": meta["name"],
                "type": meta["type"],
                "rows": (node.get("stats", {}).get("row_count", {}) or {}).get("value"),
                "columns": sorted(node["columns"].values(), key=lambda c: c["index"]),
                "doc": docs.get(key, {}),
            })

    gaps = []
    for obj in objects:
        col_docs = obj["doc"].get("columns", {})
        if not obj["doc"].get("description"):
            gaps.append("%s.%s - нет описания объекта" % (obj["schema"], obj["name"]))
        for col in obj["columns"]:
            if not col_docs.get(col["name"].lower()):
                gaps.append("%s.%s.%s - нет описания колонки"
                            % (obj["schema"], obj["name"], col["name"]))

    lines = []
    add = lines.append

    total_columns = sum(len(o["columns"]) for o in objects)
    add("# Словарь таблиц и колонок")
    add("")
    add("Собран из описаний в yml командой `make dictionary`. Руками не правится:")
    add("правка потеряется при следующей сборке. Менять надо описание рядом с")
    add("колонкой - там же, где стоят ее проверки.")
    add("")
    add("Объектов: %d, колонок: %d. Колонок без описания: %d - сборка словаря"
        % (len(objects), total_columns, len(gaps)))
    add("падает, если их больше нуля.")
    add("")
    add("Линейдж - `make docs`: dbt поднимает браузер, где видно, какая модель из")
    add("чего собрана. Рисовать эту картинку руками нельзя - она разъедется с кодом")
    add("в первую же неделю.")

    for schema in SCHEMA_ORDER:
        in_schema = sorted([o for o in objects if o["schema"] == schema],
                           key=lambda o: o["name"])
        if not in_schema:
            continue
        title, intro = SCHEMA_TITLES.get(schema, (schema, ""))
        add("")
        add("---")
        add("")
        add("## %s - схема `%s`" % (title, schema))
        if intro:
            add("")
            add(intro)

        for obj in in_schema:
            kind = "представление" if obj["type"] == "VIEW" else "таблица"
            add("")
            add("### `%s.%s`" % (obj["schema"], obj["name"]))
            add("")
            meta_bits = [kind]
            if obj["rows"] is not None:
                meta_bits.append("строк %s" % f"{int(obj['rows']):,}".replace(",", " "))
            add("*%s*" % ", ".join(meta_bits))
            add("")
            add(clean(obj["doc"].get("description", "")))
            add("")
            add("| Колонка | Тип | Что это |")
            add("|---|---|---|")
            col_docs = obj["doc"].get("columns", {})
            for col in obj["columns"]:
                add("| `%s` | %s | %s |" % (
                    col["name"],
                    col["type"],
                    clean(col_docs.get(col["name"].lower(), "")) or "-",
                ))

    add("")
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("Записан %s: объектов %d, колонок %d" % (OUTPUT.name, len(objects), total_columns))

    if gaps:
        print()
        print("Словарь неполон, %d пропусков:" % len(gaps))
        for gap in gaps:
            print("  ", gap)
        return 1

    print("Пропусков нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
