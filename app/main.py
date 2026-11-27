"""Run Telethon NetPlus listener + admin bot (+ optional customer bot + admin web)."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonCommands

from app.config import ROOT, load_settings
from app.database.database import get_session, init_db
from app.database import repositories as repo
from app.monitoring.message_listener import NetPlusListener, make_finearts_listener
from app.services.customer_payments import CustomerPaymentWatcher
from app.services.lifecycle_alerts import LifecycleWatcher
from app.services.renew_queue import RenewQueue
from app.telegram import customer_bridge
from app.telegram.bot_client import build_bot, build_dispatcher
from app.telegram.handlers import build_customer_router, build_router
from app.telegram.keyboards import admin_reply_menu
from app.telegram.user_client import build_user_client, ensure_authorized
from app.utils.logger import setup_logging
from app.vendors.finearts_driver import FINEARTS_CAPABILITIES, FineArtsRenewDriver
from app.vendors.netplus_driver import NETPLUS_CAPABILITIES, NetPlusRenewDriver
from app.vendors.registry import register_vendor_driver

log = logging.getLogger(__name__)


def _ensure_session_file(settings) -> None:
    target = Path(str(settings.user_session) + ".session")
    if target.exists():
        return
    probe = ROOT / "probe" / "main_account.session"
    if probe.exists():
        shutil.copy2(probe, target)
        journal = Path(str(probe) + "-journal")
        if journal.exists():
            shutil.copy2(journal, Path(str(target) + "-journal"))
        log.info("Copied main account session to %s", target)
    else:
        raise SystemExit(
            f"Session file missing: {target}. Log in with probe tools first."
        )


async def run() -> None:
    settings = load_settings(require_bot=True)
    setup_logging(settings.log_level, settings.logs_dir)
    _ensure_session_file(settings)
    init_db(settings.db_url)
    with get_session() as session:
        repo.ensure_default_price_plans(session)
        repo.ensure_default_bot_messages(session)
        repo.ensure_default_education(session)

    bot = build_bot(settings)
    dp = build_dispatcher()
    dp["settings"] = settings
    dp.include_router(build_router())

    user = build_user_client(settings)
    await ensure_authorized(user)

    async def notify(text: str, markup=None) -> None:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text, reply_markup=markup)
            except Exception:
                log.exception("Could not notify admin %s", admin_id)

    driver = NetPlusRenewDriver(user, settings.netplus_bot)
    register_vendor_driver(driver, NETPLUS_CAPABILITIES)
    finearts_driver = FineArtsRenewDriver(user, settings.finearts_bot)
    # Same Telethon session: share the NetPlus lock so UI walks never interleave.
    finearts_driver.lock = driver.lock
    finearts_driver._ui.lock = driver.lock
    register_vendor_driver(finearts_driver, FINEARTS_CAPABILITIES)
    renew_queue = RenewQueue(driver, settings, notify)
    dp["renew_queue"] = renew_queue
    customer_bridge.admin_bot = bot
    customer_bridge.renew_queue = renew_queue
    customer_bridge.set_bot_loop(asyncio.get_running_loop())

    listener = NetPlusListener(user, settings, notify, renew_queue=renew_queue)
    await listener.start()
    finearts_listener = make_finearts_listener(
        user, settings, notify, renew_queue=renew_queue
    )
    await finearts_listener.start()
    renew_queue.start()
    pay_watcher = CustomerPaymentWatcher(settings, notify)
    pay_watcher.start()
    lifecycle_watcher = LifecycleWatcher()
    lifecycle_watcher.start()

    admin_web_server = None
    admin_web_url = None
    web_password = (os.getenv("ADMIN_WEB_PASSWORD") or "").strip()
    web_flag = (os.getenv("ADMIN_WEB_ENABLED") or "").strip().lower()
    if web_flag in {"0", "false", "no", "off"}:
        web_enabled = False
    elif web_flag in {"1", "true", "yes", "on"}:
        web_enabled = True
    else:
        web_enabled = bool(web_password)
    if web_enabled and web_password:
        try:
            import threading

            import uvicorn
            from app.web.main import create_app as create_admin_web_app

            web_host = (os.getenv("ADMIN_WEB_HOST") or "127.0.0.1").strip()
            web_port = int(os.getenv("ADMIN_WEB_PORT") or "8787")
            admin_web_url = f"http://{web_host}:{web_port}"
            web_config = uvicorn.Config(
                app=create_admin_web_app(),
                host=web_host,
                port=web_port,
                log_level=settings.log_level.lower(),
                access_log=False,
            )
            admin_web_server = uvicorn.Server(web_config)
            # Embedded alongside aiogram: avoid uvicorn taking over process signals.
            admin_web_server.install_signal_handlers = False

            def _run_admin_web() -> None:
                try:
                    asyncio.run(admin_web_server.serve())
                except Exception:
                    log.exception("Admin web panel crashed")

            threading.Thread(
                target=_run_admin_web, name="admin-web", daemon=True
            ).start()
            log.info("Admin web panel listening on %s", admin_web_url)
        except Exception:
            log.exception("Admin web panel failed to start; bots continue")
            admin_web_server = None
            admin_web_url = None
    elif web_enabled and not web_password:
        log.warning(
            "Admin web enabled but ADMIN_WEB_PASSWORD is empty — panel not started"
        )

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="🏠 شروع مجدد"),
                BotCommand(command="menu", description="📋 منوی پایین"),
                BotCommand(command="services", description="✨ لیست سرویس‌ها"),
                BotCommand(command="search", description="🔍 جستجوی سرویس"),
            ]
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        log.exception("Admin set_my_commands failed; continuing")

    if settings.bot_proxy:
        log.info("Bot HTTP proxy enabled (%s)", settings.bot_proxy.split("@")[-1])
    else:
        log.warning(
            "BOT_PROXY not set — Telegram Bot API may be slow/timeout on restricted networks"
        )

    try:
        me = await bot.get_me()
        log.info("Admin bot ready as @%s", me.username)
    except Exception:
        log.exception("Admin get_me failed; continuing")
    boot_text = (
        "✅ سیستم مدیریت روشن شد.\n"
        "هشدارها، صف تمدید/خرید و منوی پایین فعال است."
    )
    if admin_web_url:
        boot_text += f"\n🌐 پلن ادمین: {admin_web_url}"
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                boot_text,
                reply_markup=admin_reply_menu(),
            )
        except Exception:
            log.exception("Could not notify admin %s", admin_id)

    customer_bot = None
    customer_dp = None
    if settings.customer_bot_token:
        session = (
            AiohttpSession(proxy=settings.bot_proxy, timeout=120) if settings.bot_proxy else AiohttpSession(timeout=120)
        )
        customer_bot = Bot(
            token=settings.customer_bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        customer_bridge.customer_bot = customer_bot
        customer_dp = Dispatcher()
        customer_dp["settings"] = settings
        from app.telegram.channel_gate import ChannelMembershipMiddleware
        from app.telegram.customer_busy import CustomerBusyMiddleware

        customer_dp.message.middleware(ChannelMembershipMiddleware())
        customer_dp.callback_query.middleware(ChannelMembershipMiddleware())
        # After channel gate: serialize taps and show «لطفاً صبر نمایید…» while busy.
        customer_dp.message.middleware(CustomerBusyMiddleware())
        customer_dp.callback_query.middleware(CustomerBusyMiddleware())
        customer_dp.include_router(build_customer_router())
        try:
            await customer_bot.set_my_commands(
                [
                    BotCommand(command="start", description="🏠 شروع مجدد"),
                    BotCommand(command="menu", description="📋 نمایش منوی پایین"),
                    BotCommand(command="buy", description="🛒 خرید اشتراک"),
                    BotCommand(
                        command="services",
                        description="🛍 سرویس های من | تمدید",
                    ),
                    BotCommand(
                        command="wallet",
                        description="💰 کیف پول (افزایش موجودی)",
                    ),
                    BotCommand(
                        command="help",
                        description="📚 راهنمای اتصال",
                    ),
                    BotCommand(command="support", description="💬 پشتیبانی"),
                ]
            )
            await customer_bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            cme = await customer_bot.get_me()
            log.info("Customer bot ready as @%s", cme.username)
        except Exception:
            log.exception("Customer bot menu/get_me failed; continuing")
        with get_session() as session:
            card, _ = repo.get_payment_card(
                session, settings.reseller_card_number, settings.reseller_card_holder
            )
        from app.services.aban import aban_enabled
        from app.services.variz import variz_enabled

        provider = (settings.payment_provider or "").lower()
        if provider == "aban" and aban_enabled(settings):
            log.info(
                "Aban Gateway active — customer checkout uses invoice + status poll (no webhook)"
            )
        elif provider == "variz" and variz_enabled(settings):
            log.info("Variz active — customer checkout uses create+verify poll (no webhook)")
        elif not card:
            log.warning(
                "Customer pay card not set — use admin menu «کارت مشتریان»"
            )

    try:
        tasks = [asyncio.create_task(dp.start_polling(bot), name="admin-poll")]
        if customer_bot and customer_dp:
            tasks.append(
                asyncio.create_task(
                    customer_dp.start_polling(customer_bot), name="customer-poll"
                )
            )
        await asyncio.gather(*tasks)
    finally:
        if admin_web_server is not None:
            admin_web_server.should_exit = True
        await pay_watcher.stop()
        await lifecycle_watcher.stop()
        await renew_queue.stop()
        await user.disconnect()
        await bot.session.close()
        if customer_bot:
            await customer_bot.session.close()



def main() -> None:
    try:
        from setproctitle import setproctitle

        setproctitle("vpn-manager")
    except Exception:
        pass
    asyncio.run(run())


if __name__ == "__main__":
    main()

