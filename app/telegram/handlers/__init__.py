from aiogram import Router

from app.telegram.customer_maintenance import CustomerMaintenanceMiddleware

from . import admin, customer, renew


def build_router() -> Router:
    root = Router()
    root.include_router(admin.router)
    root.include_router(renew.router)
    return root


def build_customer_router() -> Router:
    root = Router()
    mw = CustomerMaintenanceMiddleware()
    root.message.middleware(mw)
    root.callback_query.middleware(mw)
    root.include_router(customer.router)
    return root
