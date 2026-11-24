import json
from datetime import date, timedelta
from math import isfinite
from pathlib import Path
import numpy as np
from collections import Counter

_SPEC = json.loads(Path(__file__).with_name("spec.json").read_text(encoding="utf-8"))


def _validate_fields(values, fields, *, defaults=False):
    result = {}
    for field in fields:
        key = field["key"]
        value = values.get(key, field.get("default") if defaults else None)
        kind = field["type"]
        if kind == "number" or kind == "integer":
            valid = type(value) in (int, float) and isfinite(value) and field["min"] <= value <= field["max"]
            valid = valid and (kind != "integer" or int(value) == value)
            if valid:
                value = int(value) if kind == "integer" else float(value)
        elif kind == "choice":
            valid = value in field["choices"]
        else:
            valid = isinstance(value, str) and 0 < len(value.strip()) <= 300
            if valid and kind == "date":
                try:
                    valid = date.fromisoformat(value).isoformat() == value
                except ValueError:
                    valid = False
            if valid:
                value = value.strip()
        if not valid:
            raise ValueError(f"「{field['label']}」格式或范围无效")
        result[key] = value
    return result


def validate(row):
    _validate_fields(row, _SPEC["fields"])


def _inputs(rows, parameters, identity="ticket"):
    clean = [_validate_fields(row, _SPEC["fields"]) for row in rows]
    if len({row[identity] for row in clean}) != len(clean):
        raise ValueError(f"存在重复的{next(field['label'] for field in _SPEC['fields'] if field['key'] == identity)}，请检查单据")
    params = _validate_fields(parameters, _SPEC["parameters"], defaults=True)
    return sorted(clean, key=lambda row: (row.get("day", ""), row[identity])), params


def _day(start, offset):
    return (date.fromisoformat(start) + timedelta(days=offset)).isoformat()


def _shown(value, places=2):
    return "—" if value is None else round(float(value), places)


def _output(headline, summary, columns, rows, metrics, chart_name, chart_unit, chart_data):
    return {
        "headline": headline,
        "summary": summary,
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": rows,
        "metrics": [{"label": label, "value": value, "unit": unit} for label, value, unit in metrics],
        "chart": {
            "label": chart_name, "unit": chart_unit,
            "points": [{"label": str(label), "value": value} for label, value in chart_data],
        },
    }


COLUMNS = (("ticket", "拍摄编号"), ("room", "放映室"), ("dx", "横移（mm）"), ("dy", "纵移（mm）"),
           ("degree", "亲和度"), ("group", "偏移工况"), ("decision", "校光建议"))


def _graph(points, bandwidth):
    delta = points[:, None, :] - points[None, :, :]
    weights = np.exp(-np.einsum("ijk,ijk->ij", delta, delta) / (2 * bandwidth**2))
    np.fill_diagonal(weights, 0.0)
    return weights


def _partition(embedding, count):
    centers = [embedding[0]]
    for _ in range(count - 1):
        distances = np.min(np.sum((embedding[:, None] - np.asarray(centers)) ** 2, axis=2), axis=1)
        centers.append(embedding[np.argmax(distances)])
    for _ in range(60):
        distances = np.sum((embedding[:, None] - np.asarray(centers)) ** 2, axis=2)
        labels = distances.argmin(axis=1)
        if len(set(labels)) != count:
            raise ValueError("谱空间有效中心数少于期望簇数，请降低簇数")
        updated = [embedding[labels == i].mean(axis=0) for i in range(count)]
        if np.allclose(updated, centers, rtol=0, atol=1e-9):
            return labels
        centers = updated
    raise ValueError("聚类未收敛，请调整高斯图带宽或簇数")


def analyze(rows, parameters):
    rows, options = _inputs(rows, parameters)
    if not 5 <= len(rows) <= 600:
        raise ValueError("谱聚类可计算 5 至 600 条同批记录")
    points = np.array([(row["dx"], row["dy"]) for row in rows])
    weights = _graph(points, options["bandwidth"])
    degrees = weights.sum(axis=1)
    active = degrees >= options["minimum_degree"]
    classes = options["classes"]
    if int(active.sum()) <= classes:
        raise ValueError("亲和度下限太高或簇数过多，无法建立聚类")
    submatrix = weights[active][:, active]
    inner_degree = submatrix.sum(axis=1)
    if np.any(inner_degree == 0):
        raise ValueError("保留样本在亲和图内仍有孤点，请增大高斯图带宽")
    scale = 1 / np.sqrt(inner_degree)
    laplacian = np.eye(len(inner_degree)) - submatrix * scale[:, None] * scale[None, :]
    _, eigenvectors = np.linalg.eigh(laplacian)
    embedding = eigenvectors[:, :classes]
    embedding /= np.linalg.norm(embedding, axis=1)[:, None]
    raw_labels = _partition(embedding, classes)
    ordered = sorted(range(classes), key=lambda i: tuple(points[active][raw_labels == i].mean(axis=0)))
    labels = ["孤点"] * len(rows)
    for position, raw in zip(np.nonzero(active)[0], raw_labels):
        labels[position] = f"偏移簇 {ordered.index(raw) + 1}"
    detail = [row | {"group": group, "degree": _shown(degree, 3),
                     "decision": "现场复拍" if group == "孤点" else "比较同簇摆位"}
              for row, group, degree in zip(rows, labels, degrees)]
    counts = Counter(labels)
    return _output("放映校光偏移簇", f"以 {options['bandwidth']} mm 带宽衡量光斑坐标的相似性。{counts['孤点']} 条亲和度不足的校光拍摄需要复拍，工位可按簇核查偏移。",
                   COLUMNS, sorted(detail, key=lambda row: (row["group"] != "孤点", row["group"], row["ticket"])),
                   [("可归簇拍摄", int(active.sum()), "张"), ("检测工况", classes, "类"), ("待复拍", counts["孤点"], "张")],
                   "各工况拍摄次数", "张", sorted(counts.items()))


def seed():
    offsets = ((0.8, -0.8), (1.6, 0.3), (0.2, 1.1), (-1.2, -0.5), (1.2, 0.1), (0.1, -1.5))
    rows = []
    for cluster, (x, y) in enumerate(((-7, 4), (8, -4))):
        for i, (u, v) in enumerate(offsets):
            rows.append({"ticket": f"HY-{cluster + 1}-{i + 1:02}", "room": f"{'西弧' if cluster == 0 else '东台'} {i % 3 + 1} 厅",
                         "day": _day("2026-11-16", i), "dx": round(x + u, 2), "dy": round(y + v, 2)})
    return rows + [{"ticket": "HY-X01", "room": "西弧 3 厅", "day": "2026-11-22", "dx": -17, "dy": -13}]
