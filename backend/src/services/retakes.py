from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import OrderStatus, RunStatus
from src.models.base import utc_now
from src.models.workflow import AnalysisRun, AuditEvent, Observation, RetakeOrder, RetakeRegistration
from src.services.errors import BusinessError
from src.services.observations import latest_reading_map
from src.utils.formatting import timestamp


def active_order(db):
    return db.scalar(select(RetakeOrder).where(RetakeOrder.status == OrderStatus.ACTIVE)
                     .order_by(RetakeOrder.sequence.desc()).limit(1))


def create_order_from_run(db, run):
    """成功分析算完后生成复拍清单；同一分析重复执行不会重复登记。"""
    if run.status != RunStatus.SUCCEEDED:
        raise BusinessError('只有成功的分析才能生成复拍清单', 409)
    existing = db.scalar(select(RetakeOrder).where(RetakeOrder.run_id == run.id))
    if existing is not None:
        return existing
    tickets = list(run.result.get('orphan_tickets', []))
    sequence = (db.scalar(select(func.max(RetakeOrder.sequence))) or 0) + 1
    order = RetakeOrder(sequence=sequence, run_id=run.id, tickets=tickets, status=OrderStatus.COMPUTING)
    db.add(order)
    db.flush()
    run.order_id = order.id
    db.add(AuditEvent(actor='worker', action='retake.computed',
                      detail=f'复拍清单 #{order.id}（序号 {sequence}）含 {len(tickets)} 个待复拍拍摄'))
    return order


def activate_order(db, order_id, actor):
    """新清单算成后才接替旧清单；序号较小的旧批晚算完也不能换回去。"""
    order = db.get(RetakeOrder, order_id, with_for_update=True)
    if order is None:
        raise BusinessError('复拍清单不存在', 404)
    if order.status == OrderStatus.ACTIVE:
        return order
    if order.status == OrderStatus.DISCARDED:
        raise BusinessError('这份复拍清单已被更新的清单接替，不能重新生效', 409)
    current = active_order(db)
    if current is not None and order.sequence <= current.sequence:
        order.status = OrderStatus.DISCARDED
        db.add(AuditEvent(actor=actor, action='retake.superseded',
                          detail=f'清单 #{order.id}（序号 {order.sequence}）晚于生效清单 #{current.id}，作废处理'))
        raise BusinessError(f'生效中的清单 #{current.id} 序号更新，旧清单不能接替', 409)
    if current is not None:
        current.status = OrderStatus.DISCARDED
        db.add(current)
    order.status = OrderStatus.ACTIVE
    order.activated_at = utc_now()
    db.add(AuditEvent(actor=actor, action='retake.activated',
                      detail=f'复拍清单 #{order.id}（序号 {order.sequence}）接替生效'))
    db.flush()
    return order


def register_session(db, order_id, ticket, note, actor):
    """在生效清单上登记复拍场次，同一清单同一拍摄编号幂等。"""
    order = db.get(RetakeOrder, order_id, with_for_update=True)
    if order is None:
        raise BusinessError('复拍清单不存在', 404)
    if order.status != OrderStatus.ACTIVE:
        raise BusinessError('只有生效中的复拍清单可以登记复拍场次', 409)
    if ticket not in order.tickets:
        raise BusinessError(f'拍摄编号「{ticket}」不在生效复拍清单内', 409)
    duplicate = db.scalar(select(RetakeRegistration).where(
        RetakeRegistration.order_id == order_id, RetakeRegistration.ticket == ticket))
    if duplicate is not None:
        return duplicate
    registration = RetakeRegistration(order_id=order_id, ticket=ticket, note=note.strip(), actor=actor)
    db.add(registration)
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError('该拍摄编号已登记复拍场次', 409) from exc
    db.add(AuditEvent(actor=actor, action='retake.registered',
                      detail=f'清单 #{order_id} 登记复拍场次 {ticket}'))
    return registration


def order_overview(db):
    orders = list(db.scalars(select(RetakeOrder).order_by(RetakeOrder.sequence.desc()).limit(50)))
    registered = {}
    if orders:
        registration_rows = db.scalars(select(RetakeRegistration).where(
            RetakeRegistration.order_id.in_([order.id for order in orders]))).all()
        for row in registration_rows:
            registered.setdefault(row.order_id, []).append(row)
    current = active_order(db)
    return orders, current, registered


def latest_snapshot_observation_ids(db):
    """新分析一次取齐同批更正：每编号只取批次号最大的读数。"""
    observations = list(db.scalars(select(Observation).order_by(Observation.id)))
    latest = latest_reading_map(observations)
    return [observation.id for observation in sorted(latest.values(), key=lambda item: item.id)]


def prior_orphan_baseline(db):
    """上一份成功分析的孤点集合，作为重算时识别新增孤点的基线。"""
    run = db.scalar(select(AnalysisRun).where(AnalysisRun.status == RunStatus.SUCCEEDED)
                    .order_by(AnalysisRun.id.desc()).limit(1))
    if run is None or not run.result:
        return None, None
    return run.id, list(run.result.get('orphan_tickets', []))


def order_view(order, registrations):
    return {
        'id': order.id,
        'sequence': order.sequence,
        'run_id': order.run_id,
        'status': order.status,
        'tickets': list(order.tickets),
        'created_at': timestamp(order.created_at),
        'activated_at': timestamp(order.activated_at),
        'registrations': [
            {'ticket': row.ticket, 'note': row.note, 'actor': row.actor,
             'at': timestamp(row.created_at)} for row in registrations
        ],
    }
