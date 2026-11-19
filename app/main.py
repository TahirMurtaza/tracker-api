import logging

from fastapi import APIRouter, FastAPI

from app import errors
from app.auth.router import router as auth_router
from app.categories.router import router as categories_router
from app.expenses.receipts import router as receipts_router
from app.expenses.router import router as expenses_router
from app.imports.router import router as imports_router
from app.notifications.router import router as notifications_router
from app.reports.router import router as reports_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Expense Tracker API", version="1.0.0")
errors.install(app)

api = APIRouter(prefix="/api/v1")
for r in (auth_router, categories_router, imports_router, expenses_router, receipts_router, reports_router, notifications_router):
    api.include_router(r)
app.include_router(api)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
