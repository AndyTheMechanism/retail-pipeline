"""Builds the table and column dictionary out of dbt metadata.

The dictionary is not written by hand. The descriptions are — in the same yml
files where the checks live, next to the column they describe. Hence its main
property: it cannot fall behind the schema, because it is built from it.

The script fails if even one column has no description. That is deliberate: a
dictionary with holes is worse than no dictionary — people make decisions on it
believing it to be complete. An empty cell reads as "there is nothing to say
here", so either say something or drop the column.

Reads dbt/target/catalog.json and manifest.json, writes DICTIONARY.md.
Run: make dictionary
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "dbt" / "target"
OUTPUT = ROOT / "DICTIONARY.md"

# The order of the layers is the order the data moves in, not the alphabet.
SCHEMA_ORDER = ["raw", "staging", "intermediate", "marts", "ops"]

SCHEMA_TITLES = {
    "raw": ("Raw layer", "The source export as it is, defects included. "
                         "There are deliberately no primary keys and no uniqueness "
                         "constraints here."),
    "staging": ("Staging layer", "Fixes the shape: types, names, grain. Counts nothing "
                                 "and repairs nothing. Materialised as views."),
    "intermediate": ("Intermediate layer", "Brings the sources together. Leaves everything "
                                           "visible, orphans included, so the gate has "
                                           "something to catch."),
    "marts": ("Marts", "What a human and a dashboard read."),
    "ops": ("Internals", "The machinery the revision log rests on. "
                         "Not meant to be read by hand."),
}


def load() -> tuple[dict, dict]:
    catalog = TARGET / "catalog.json"
    manifest = TARGET / "manifest.json"
    if not catalog.exists() or not manifest.exists():
        print("No dbt metadata. Run make docs first.")
        raise SystemExit(1)
    return json.loads(catalog.read_text()), json.loads(manifest.read_text())


def descriptions(manifest: dict) -> dict[str, dict]:
    """Descriptions from yml: per node, the object's own plus one per column."""
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
    """A yml description squashed onto one line: the dictionary is read as a table."""
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
            gaps.append("%s.%s - no object description" % (obj["schema"], obj["name"]))
        for col in obj["columns"]:
            if not col_docs.get(col["name"].lower()):
                gaps.append("%s.%s.%s - no column description"
                            % (obj["schema"], obj["name"], col["name"]))

    lines = []
    add = lines.append

    total_columns = sum(len(o["columns"]) for o in objects)
    add("# Table and column dictionary")
    add("")
    add("Built from the descriptions in yml by `make dictionary`. Not edited by hand:")
    add("an edit is lost on the next build. Edit the description next to the column")
    add("instead — in the same place as its checks.")
    add("")
    add("Objects: %d, columns: %d. Columns with no description: %d — building the"
        % (len(objects), total_columns, len(gaps)))
    add("dictionary fails if that is more than zero.")
    add("")
    add("Lineage is `make docs`: dbt opens a browser showing what each model is built")
    add("from. That picture cannot be drawn by hand — it would part ways with the code")
    add("within the first week.")

    for schema in SCHEMA_ORDER:
        in_schema = sorted([o for o in objects if o["schema"] == schema],
                           key=lambda o: o["name"])
        if not in_schema:
            continue
        title, intro = SCHEMA_TITLES.get(schema, (schema, ""))
        add("")
        add("---")
        add("")
        add("## %s — schema `%s`" % (title, schema))
        if intro:
            add("")
            add(intro)

        for obj in in_schema:
            kind = "view" if obj["type"] == "VIEW" else "table"
            add("")
            add("### `%s.%s`" % (obj["schema"], obj["name"]))
            add("")
            meta_bits = [kind]
            if obj["rows"] is not None:
                meta_bits.append("rows %s" % f"{int(obj['rows']):,}")
            add("*%s*" % ", ".join(meta_bits))
            add("")
            add(clean(obj["doc"].get("description", "")))
            add("")
            add("| Column | Type | What it is |")
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

    print("Wrote %s: objects %d, columns %d" % (OUTPUT.name, len(objects), total_columns))

    if gaps:
        print()
        print("The dictionary is incomplete, %d gaps:" % len(gaps))
        for gap in gaps:
            print("  ", gap)
        return 1

    print("No gaps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
