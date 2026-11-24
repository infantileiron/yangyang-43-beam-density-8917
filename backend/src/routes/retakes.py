from fastapi import APIRouter, Depends

from src.constants.enums import Role
from src.models.base import Session
from src.schemas.requests import RetakeRegistrationInput
from src.services.auth import current_user, roles
from src.services.retakes import activate_order, order_overview, order_view, register_session
from src.utils.formatting import timestamp

router = APIRouter(prefix='/api/retakes')
editor = roles(Role.ANALYST, Role.SUPERVISOR)


@router.get('/orders')
def orders(user=Depends(current_user)):
    with Session() as db:
        items, current, registered = order_overview(db)
        return {
            'active_id': current.id if current else None,
            'items': [order_view(order, registered.get(order.id, [])) for order in items],
        }


@router.post('/orders/{order_id}/activate', status_code=201)
def activate(order_id: int, user=Depends(editor)):
    with Session.begin() as db:
        order = activate_order(db, order_id, user['sub'])
        return order_view(order, [])


@router.post('/orders/{order_id}/registrations', status_code=201)
def register(order_id: int, request: RetakeRegistrationInput, user=Depends(editor)):
    with Session.begin() as db:
        registration = register_session(db, order_id, request.ticket, request.note, user['sub'])
        return {'id': registration.id, 'order_id': registration.order_id,
                'ticket': registration.ticket, 'note': registration.note,
                'actor': registration.actor, 'at': timestamp(registration.created_at)}
