from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse

from app.deps import CurrentUser, DbSession
from app.errors import AppError
from app.expenses import idempotency, service
from app.expenses.schemas import ExpenseCreate, ExpenseOut, ExpensePage, ExpensePatch

router = APIRouter(prefix="/expenses", tags=["expenses"])


def etag(version: int) -> str:
    return f'"{version}"'


def _parse_if_match(value: str) -> int:
    v = value.strip()
    if v.startswith("W/"):
        v = v[2:]
    try:
        return int(v.strip('"'))
    except ValueError:
        return -1  # can never match a real version -> 412


def _created(payload: dict, replayed: bool = False) -> JSONResponse:
    headers = {"ETag": etag(payload["version"]), "Location": f"/api/v1/expenses/{payload['id']}"}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(payload, 201, headers=headers)


@router.post("", status_code=201, response_model=ExpenseOut)
async def create_expense(
    body: ExpenseCreate,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
):
    if idempotency_key is None:
        out = await service.create(session, user, body)
        await session.commit()
        return _created(out.model_dump(mode="json"))

    replay = await idempotency.claim(session, user.id, idempotency_key, idempotency.request_hash(body.model_dump(mode="json")))
    if replay is not None:
        return _created(replay[1], replayed=True)
    out = await service.create(session, user, body)
    payload = out.model_dump(mode="json")
    await idempotency.store(session, user.id, idempotency_key, 201, payload)
    await session.commit()  # expense + outbox row + idempotency record: one atomic commit
    return _created(payload)


@router.get("", response_model=ExpensePage)
async def list_expenses(
    user: CurrentUser,
    session: DbSession,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    category_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_amount: Annotated[Decimal | None, Query(ge=0)] = None,
    max_amount: Annotated[Decimal | None, Query(ge=0)] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
):
    items, next_cursor = await service.list_expenses(
        session, user.id, limit=limit, cursor=cursor, category_id=category_id, date_from=date_from, date_to=date_to,
        min_amount=min_amount, max_amount=max_amount, currency=currency, q=q,
    )  # fmt: skip
    return ExpensePage(items=items, next_cursor=next_cursor)


@router.get("/{expense_id}", response_model=ExpenseOut)
async def get_expense(expense_id: int, user: CurrentUser, session: DbSession, response: Response):
    exp = await service.get_owned(session, user.id, expense_id)
    response.headers["ETag"] = etag(exp.version)
    return exp


@router.patch("/{expense_id}", response_model=ExpenseOut)
async def patch_expense(
    expense_id: int,
    body: ExpensePatch,
    user: CurrentUser,
    session: DbSession,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
):
    if if_match is None:
        raise AppError(428, "precondition_required", "If-Match header is required")
    out = await service.update_expense(session, user, expense_id, _parse_if_match(if_match), body)
    await session.commit()
    response.headers["ETag"] = etag(out.version)
    return out


@router.delete("/{expense_id}", status_code=204)
async def delete_expense(expense_id: int, user: CurrentUser, session: DbSession):
    await service.delete_expense(session, user.id, expense_id)
    await session.commit()
    return Response(status_code=204)
