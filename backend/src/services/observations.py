import csv
import hashlib
import io
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.project import SPEC
from src.domain import analysis
from src.models.workflow import AuditEvent, Observation
from src.services.errors import BusinessError
from src.services.validation import validated


MAX_OBSERVATIONS = 5000
RETAKE_KEY_PREFIX = 'retake:'


def clean_observation(raw):
    row = validated(SPEC['fields'], raw)
    try:
        if hasattr(analysis, 'validate'):
            analysis.validate(row)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc
    return row


def content_key(row):
    return 'data:' + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def stored_key(row):
    """首拍沿用内容键；复拍记录加独立前缀，避免同编号不同批次互相覆盖。"""
    key = content_key(row)
    return key if int(row.get('batch_no') or 1) == 1 else RETAKE_KEY_PREFIX + key


def parse_csv(raw):
    if len(raw) > 512 * 1024:
        raise BusinessError('CSV 文件上限 512 KB', 413)
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), strict=True)
        header = reader.fieldnames or []
        known = {item['key'] for item in SPEC['fields']}
        required = {item['key'] for item in SPEC['fields'] if item.get('required', True)}
        if len(set(header)) != len(header) or not required.issubset(header) or not set(header).issubset(known):
            raise BusinessError('CSV 表头与导入模板不匹配')
        rows = []
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise BusinessError('列数与表头不匹配')
                rows.append(clean_observation(row))
            except BusinessError as exc:
                raise BusinessError(f'CSV 第 {reader.line_num} 行，{exc}') from exc
            if len(rows) > 500:
                raise BusinessError('单次导入上限 500 条')
        if not rows:
            raise BusinessError('CSV 里没有观测记录')
        return rows
    except (UnicodeDecodeError, csv.Error) as exc:
        raise BusinessError('请上传 UTF-8 编码的有效 CSV') from exc


def latest_reading_map(observations):
    """按拍摄编号保留批次号最大的读数；乱序到达的旧批次不会顶掉较新坐标。"""
    latest = {}
    for observation in observations:
        payload = observation.payload
        ticket = payload['ticket']
        batch_no = int(payload.get('batch_no') or 1)
        current = latest.get(ticket)
        if current is None or batch_no > int(current.payload.get('batch_no') or 1):
            latest[ticket] = observation
    return latest


def add_observations(db, rows, actor, action):
    clean_rows = analysis.normalize_rows(rows)
    keyed = {stored_key(row): row for row in clean_rows}
    existing = set(db.scalars(select(Observation.source_key).where(Observation.source_key.in_(keyed))))
    fresh = {key: row for key, row in keyed.items() if key not in existing}
    if db.scalar(select(func.count(Observation.id))) + len(fresh) > MAX_OBSERVATIONS:
        raise BusinessError('当前项目最多保留 5000 条观测记录', 409)

    history = list(db.scalars(select(Observation)))
    latest = latest_reading_map(history)
    known_batches = {}
    for observation in history:
        known_batches.setdefault(observation.payload['ticket'], set()).add(
            int(observation.payload.get('batch_no') or 1))

    corrections, stale, gaps = [], [], []
    # 同一次导入内部也按批次号从小到大处理，保证乱序提交时判定一致
    for row in sorted(fresh.values(), key=lambda item: (item['ticket'], item['batch_no'])):
        ticket, batch_no = row['ticket'], row['batch_no']
        batches = known_batches.setdefault(ticket, set())
        current = latest.get(ticket)
        current_batch = int(current.payload.get('batch_no') or 1) if current is not None else 0
        if batch_no <= current_batch:
            stale.append(f'{ticket}（批次 {batch_no} 旧于或等于现有批次 {current_batch}）')
            continue
        if batch_no != 1 and (batch_no - 1) not in batches:
            gaps.append(f'{ticket}（批次 {batch_no} 之前缺少批次 {batch_no - 1}）')
            continue
        if current is not None:
            corrections.append(row)
        batches.add(batch_no)
        latest[ticket] = type('PendingObservation', (), {'payload': row})()
    if stale:
        raise BusinessError(f"存在晚到的旧批次读数，不能覆盖较新坐标：{'、'.join(stale[:5])}", 409)
    if gaps:
        raise BusinessError(f"复拍批次必须按次序连续登记：{'、'.join(gaps[:5])}", 409)

    items = [Observation(source_key=key, payload=row) for key, row in fresh.items()]
    db.add_all(items)
    if items:
        parts = [f'新增 {len(items) - len(corrections)} 条观测']
        if corrections:
            parts.append(f"更正 {len(corrections)} 个拍摄编号的最新坐标（{'、'.join(row['ticket'] for row in corrections[:8])}）")
        db.add(AuditEvent(actor=actor, action=action, detail='，'.join(parts)))
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError('有相同记录刚刚写入，请刷新后重试', 409) from exc
    return {'items': items, 'corrections': len(corrections), 'skipped': len(clean_rows) - len(items)}
