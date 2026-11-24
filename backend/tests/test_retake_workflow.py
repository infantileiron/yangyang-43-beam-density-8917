import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:///:memory:')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_ROOT', str(Path(__file__).resolve().parent / 'artifacts'))

from src.constants.enums import OrderStatus, RunStatus  # noqa: E402
from src.domain.analysis import analyze, normalize_rows, seed  # noqa: E402
from src.models.base import Base  # noqa: E402
from src.models.workflow import (AnalysisRun, AuditEvent, Observation, RetakeOrder,  # noqa: E402
                                 RetakeRegistration)
from src.services.errors import BusinessError  # noqa: E402
from src.services.observations import add_observations, latest_reading_map  # noqa: E402
from src.services.retakes import (activate_order, create_order_from_run,  # noqa: E402
                                  latest_snapshot_observation_ids, prior_orphan_baseline,
                                  register_session)
from src.services.runs import create_run, run_pending  # noqa: E402

engine = create_engine(
    'sqlite+pysqlite:///:memory:',
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
)
Session = sessionmaker(engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(engine)


def base_row(ticket, dx, dy, batch_no=1, room='西弧 3 厅', day='2026-11-23'):
    return {'ticket': ticket, 'batch_no': batch_no, 'room': room, 'day': day, 'dx': dx, 'dy': dy}


def find_observation(db, ticket):
    return next(row for row in db.scalars(select(Observation)).all() if row.payload['ticket'] == ticket)


def add(db, rows, actor='analyst@demo.local', action='observation.created'):
    return add_observations(db, rows, actor, action)


def run_analysis(db, title='班次分析', actor='analyst@demo.local'):
    from src.schemas.requests import CreateRun

    request = CreateRun(title=title, parameters={})
    run = create_run(db, request, actor)
    assert run_pending(db) is True
    return db.get(AnalysisRun, run.id)


def clustered_rows(seed_orphan=True):
    """两个紧凑簇 + 可选远点，保证谱聚类稳定、远点为孤点。"""
    rows = []
    for cluster, (cx, cy) in enumerate(((-7, 4), (8, -4))):
        for i in range(6):
            rows.append(base_row(f'HY-C{cluster + 1}-{i + 1:02}', cx + (i % 3) * 0.4,
                                 cy - (i % 2) * 0.4, day=f'2026-11-2{cluster + 1}'))
    if seed_orphan:
        rows.append(base_row('HY-ISO1', 18, -11))
    return rows


# 规则 1：同编号两份坐标按批次号取新，乱序旧批次不能顶掉新坐标
def test_out_of_order_old_batch_cannot_replace_newer(db):
    add(db, [base_row('HY-X02', 18, -11, batch_no=1)])
    add(db, [base_row('HY-X02', 17.2, -10.4, batch_no=2)])
    # 批次 3 先到、批次 2 的迟到重复之外，再补一个真正晚到的旧批次 1 变体
    add(db, [base_row('HY-X02', 16.5, -9.8, batch_no=3)])
    with pytest.raises(BusinessError) as exc:
        add(db, [base_row('HY-X02', 99, 99, batch_no=1)])
    assert '旧批次' in str(exc.value)

    observations = db.scalars(select(Observation)).all()
    latest = latest_reading_map(observations)['HY-X02']
    assert latest.payload['dx'] == 16.5
    assert latest.payload['batch_no'] == 3


def test_batch_gap_is_rejected(db):
    with pytest.raises(BusinessError) as exc:
        add(db, [base_row('HY-X02', 1, 1, batch_no=2)])
    assert '批次 1' in str(exc.value)


def test_skipping_batch_in_one_import_is_rejected(db):
    add(db, [base_row('HY-X02', 1, 1, batch_no=1)])
    with pytest.raises(BusinessError) as exc:
        # 批次 1 已在库，这次直接跳来批次 3，缺少批次 2
        add(db, [base_row('HY-X02', 3, 3, batch_no=3)])
    assert '连续' in str(exc.value)
    # 整单拒绝，库里仍只有批次 1
    assert len(db.scalars(select(Observation)).all()) == 1


def test_sequential_batches_are_accepted(db):
    report = add(db, [base_row('HY-X02', 1, 1, batch_no=1),
                      base_row('HY-X02', 2, 2, batch_no=2),
                      base_row('HY-X02', 3, 3, batch_no=3)])
    assert report['corrections'] == 2
    latest = latest_reading_map(db.scalars(select(Observation)).all())['HY-X02']
    assert latest.payload['batch_no'] == 3


def test_duplicate_same_batch_is_rejected():
    with pytest.raises(ValueError) as exc:
        analyze([base_row('HY-X02', 1, 1), base_row('HY-X02', 2, 2)], {})
    assert '重复读数' in str(exc.value)


def test_legacy_rows_default_to_batch_one(db):
    legacy = {k: v for k, v in base_row('HY-X02', 1, 1).items() if k != 'batch_no'}
    add(db, [legacy])
    row = db.scalars(select(Observation)).one()
    assert row.payload['batch_no'] == 1
    assert normalize_rows([legacy])[0]['batch_no'] == 1


# 规则 2：更正与提交并发时，排队分析按原读数跑；新分析一次取齐同批更正
def test_queued_run_keeps_original_snapshot_while_new_run_sees_corrections(db):
    add(db, clustered_rows())
    from src.schemas.requests import CreateRun

    first = create_run(db, CreateRun(title='第一次', parameters={}), 'analyst@demo.local')
    frozen_ids = list(first.observation_ids)

    # 对簇内一个拍摄提交批次 2 更正（排队期间不允许影响 first）
    target = find_observation(db, 'HY-C1-01')
    add(db, [{**target.payload, 'batch_no': 2, 'dx': -7.1, 'dy': 4.2}])

    assert first.observation_ids == frozen_ids
    second = create_run(db, CreateRun(title='第二次', parameters={}), 'analyst@demo.local')
    latest_ids = latest_snapshot_observation_ids(db)
    assert second.observation_ids == latest_ids
    assert target.id not in latest_ids  # 旧批次已被排除出最新快照
    assert len(second.observation_ids) == len(frozen_ids)  # 每编号仍只有一条最新读数

    assert run_pending(db) is True
    done_first = db.get(AnalysisRun, first.id)
    first_rows = {row['ticket']: row for row in done_first.result['rows']}
    assert first_rows['HY-C1-01']['dx'] == target.payload['dx']  # 原读数
    assert done_first.status == RunStatus.SUCCEEDED

    assert run_pending(db) is True
    done_second = db.get(AnalysisRun, second.id)
    second_rows = {row['ticket']: row for row in done_second.result['rows']}
    assert second_rows['HY-C1-01']['dx'] == -7.1
    assert second_rows['HY-C1-01']['batch_no'] == 2


# 规则 3：重算识别因周围读数变化而新转孤点的拍摄
def test_recompute_flags_new_orphan_tickets(db):
    add(db, clustered_rows())
    first = run_analysis(db)
    baseline = list(first.result['orphan_tickets'])
    assert baseline == ['HY-ISO1']

    # 把 HY-C2 簇的五个邻居全部改到远处，仅留 HY-C2-06 在原位，使其失去邻居转为孤点
    stay = find_observation(db, 'HY-C2-06')
    corrections = []
    for i in range(1, 6):
        old = find_observation(db, f'HY-C2-0{i}')
        corrections.append({**old.payload, 'batch_no': 2, 'dx': -14 + i, 'dy': 12})
    add(db, corrections)

    second = run_analysis(db)
    orphans = second.result['orphan_tickets']
    new_orphans = second.result['new_orphan_tickets']
    assert 'HY-ISO1' in orphans
    assert stay.payload['ticket'] in orphans
    assert 'HY-ISO1' not in new_orphans  # 基线里已有，不算新增
    assert 'HY-C2-06' in new_orphans
    metric = next(item for item in second.result['metrics'] if item['label'] == '新增孤点')
    assert metric['value'] == len(new_orphans)


def test_first_run_has_no_new_orphan_metric_baseline(db):
    add(db, clustered_rows())
    run = run_analysis(db)
    assert run.result['new_orphan_tickets'] == []
    assert not any(item['label'] == '新增孤点' for item in run.result['metrics'])


# 规则 4：新清单算成后才接替旧清单，旧批晚算完不能换回去
def test_retake_order_sequence_activation(db):
    add(db, clustered_rows())
    run_one = run_analysis(db)
    order_one = create_order_from_run(db, run_one)
    assert order_one.status == OrderStatus.COMPUTING
    activate_order(db, order_one.id, 'supervisor@demo.local')
    assert db.get(RetakeOrder, order_one.id).status == OrderStatus.ACTIVE

    run_two = run_analysis(db)
    order_two = create_order_from_run(db, run_two)
    assert order_two.sequence == order_one.sequence + 1

    # 新清单先接替生效，旧清单晚算完后试图重新激活，必须作废且不能换回
    activate_order(db, order_two.id, 'supervisor@demo.local')
    assert db.get(RetakeOrder, order_two.id).status == OrderStatus.ACTIVE
    with pytest.raises(BusinessError) as exc:
        activate_order(db, order_one.id, 'supervisor@demo.local')
    assert '旧清单' in str(exc.value) or '更新' in str(exc.value)
    assert db.get(RetakeOrder, order_one.id).status == OrderStatus.DISCARDED
    assert db.get(RetakeOrder, order_two.id).status == OrderStatus.ACTIVE

    # 再次激活旧清单依旧被拒
    with pytest.raises(BusinessError):
        activate_order(db, order_one.id, 'supervisor@demo.local')


def test_order_created_once_per_run(db):
    add(db, clustered_rows())
    run = run_analysis(db)
    first = create_order_from_run(db, run)
    again = create_order_from_run(db, run)
    assert again.id == first.id
    assert db.scalar(select(RetakeOrder).where(RetakeOrder.run_id == run.id)) is first


# 规则 5：同批重跑不重复登记复拍场次
def test_registration_is_idempotent_per_active_order(db):
    add(db, clustered_rows())
    run = run_analysis(db)
    order = create_order_from_run(db, run)
    activate_order(db, order.id, 'supervisor@demo.local')
    ticket = order.tickets[0]

    first = register_session(db, order.id, ticket, '当班复拍', 'analyst@demo.local')
    second = register_session(db, order.id, ticket, '重复提交应忽略', 'analyst@demo.local')
    assert second.id == first.id
    count = len(db.scalars(select(RetakeRegistration).where(RetakeRegistration.order_id == order.id)).all())
    assert count == 1

    with pytest.raises(BusinessError):
        register_session(db, order.id, 'HY-NOT-IN-LIST', 'x', 'analyst@demo.local')


def test_registration_requires_active_order(db):
    add(db, clustered_rows())
    run = run_analysis(db)
    order = create_order_from_run(db, run)  # 仍为 computing
    with pytest.raises(BusinessError) as exc:
        register_session(db, order.id, order.tickets[0], 'x', 'analyst@demo.local')
    assert '生效中' in str(exc.value)


def test_audit_trail_records_retake_lifecycle(db):
    add(db, clustered_rows())
    run = run_analysis(db)
    order = create_order_from_run(db, run)
    activate_order(db, order.id, 'supervisor@demo.local')
    register_session(db, order.id, order.tickets[0], 'note', 'analyst@demo.local')
    actions = set(db.scalars(select(AuditEvent.action)))
    assert {'retake.computed', 'retake.activated', 'retake.registered'} <= actions


def test_prior_baseline_tracks_latest_succeeded_run(db):
    add(db, clustered_rows())
    first = run_analysis(db)
    run_id, baseline = prior_orphan_baseline(db)
    assert run_id == first.id
    assert baseline == first.result['orphan_tickets']


def test_seed_dataset_remains_valid():
    rows = normalize_rows(seed())
    assert all(row['batch_no'] == 1 for row in rows)
    result = analyze(rows, {})
    assert result['orphan_tickets'] == ['HY-X01']
