"""Computations on user-supplied data; never execute LLM-generated code."""

import csv
import hashlib
import math
import statistics
from pathlib import Path


def analyze_csv(path: Path) -> dict:
    if path.stat().st_size > 5_000_000:
        raise ValueError("CSV больше 5 МБ")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(stream, dialect=dialect)
        if not reader.fieldnames or len(reader.fieldnames) > 40:
            raise ValueError("Нужен заголовок CSV и не более 40 столбцов")
        rows = []
        for row in reader:
            rows.append(row)
            if len(rows) > 100_000:
                raise ValueError("Не более 100000 строк")
    result = {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "columns": {}}
    for field in reader.fieldnames:
        values, missing, invalid = [], 0, 0
        for row in rows:
            raw = row.get(field)
            if raw is None or not raw.strip():
                missing += 1
                continue
            try:
                value = float(raw.strip().replace(",", "."))
                if not math.isfinite(value):
                    raise ValueError("Non-finite")
                values.append(value)
            except ValueError:
                invalid += 1
        if values:
            result["columns"][field] = {
                "n": len(values),
                "missing": missing,
                "non_numeric": invalid,
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "std": statistics.stdev(values) if len(values) > 1 else 0.0,
                "min": min(values),
                "max": max(values),
            }
    if not result["columns"]:
        raise ValueError("CSV не содержит числовых столбцов")
    return result
