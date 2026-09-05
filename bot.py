import os
import html
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

from geopy.geocoders import Nominatim, OpenCage
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.error import RetryAfter, Forbidden
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

from database import (
    init_db,
    get_user_preferences,
    update_user_preference,
    get_all_active_users,
    get_unsent_jobs_for_user,
    mark_jobs_sent,
    upsert_jobs,
    prune_old_sent_jobs,
)
from scraper import fetch_all_jobs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
SCRAPE_INTERVAL = max(1800, int(os.getenv("SCRAPE_INTERVAL_SECONDS", "1800")))
DIGEST_SIZE = min(10, max(1, int(os.getenv("DIGEST_SIZE", "5"))))
RETENTION_DAYS = max(7, int(os.getenv("JOB_RETENTION_DAYS", "90")))


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


threading.Thread(target=run_health_server, daemon=True).start()

OPENCAGE_API_KEY = os.getenv("OPENCAGE_API_KEY", "")
geolocator = (
    OpenCage(api_key=OPENCAGE_API_KEY)
    if OPENCAGE_API_KEY
    else Nominatim(user_agent="job_notifier_bot")
)

COUNTRY_NAMES = {
    "US": "🇺🇸 United States",
    "CA": "🇨🇦 Canada",
    "UK": "🇬🇧 United Kingdom",
    "AU": "🇦🇺 Australia",
}
TIER_NAMES = {
    "non_tech": "🛠 Non-Technical / Shifts / Trades",
    "white_collar": "💻 Corporate / Technical / Desk",
    "all": "🌐 All Job Types",
}
MAJOR_CITIES = {
    "US": ["New York", "Los Angeles", "Chicago", "Houston", "Miami"],
    "CA": ["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa"],
    "UK": ["London", "Manchester", "Birmingham", "Glasgow", "Leeds"],
    "AU": ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide"],
}


def get_country_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇺🇸 United States", callback_data="set_country_US")],
            [InlineKeyboardButton("🇨🇦 Canada", callback_data="set_country_CA")],
            [InlineKeyboardButton("🇬🇧 United Kingdom", callback_data="set_country_UK")],
            [InlineKeyboardButton("🇦🇺 Australia", callback_data="set_country_AU")],
        ]
    )


def get_tier_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛠 Non-Technical / Shifts / Trades", callback_data="set_tier_non_tech")],
            [InlineKeyboardButton("💻 Corporate / Technical / Desk", callback_data="set_tier_white_collar")],
            [InlineKeyboardButton("🌐 All Jobs", callback_data="set_tier_all")],
        ]
    )


def get_city_keyboard(country_code):
    cities = MAJOR_CITIES.get(country_code, [])
    rows = [[InlineKeyboardButton(cities[i], callback_data=f"set_city_{cities[i]}"),
             InlineKeyboardButton(cities[i + 1], callback_data=f"set_city_{cities[i + 1]}")]
            for i in range(0, len(cities) - 1, 2)]
    if len(cities) % 2:
        rows.append([InlineKeyboardButton(cities[-1], callback_data=f"set_city_{cities[-1]}")])
    rows.extend(
        [
            [InlineKeyboardButton("✍️ Type Custom City", callback_data="prompt_custom_city")],
            [InlineKeyboardButton("📍 Share Live Location", callback_data="prompt_share_location")],
            [InlineKeyboardButton("🌐 Entire Country", callback_data="set_city_CLEAR")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(update_user_preference, user_id, active=1)
    await update.message.reply_text(
        "👋 **Welcome to the Global Job Alert Bot!**\n\n"
        "Step 1: Select your target country below:",
        reply_markup=get_country_keyboard(),
        parse_mode="Markdown",
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prefs = await asyncio.to_thread(get_user_preferences, update.effective_user.id)
    city_label = prefs["city"] or "Entire Country (Nationwide)"
    await update.message.reply_text(
        "⚙️ **Your Current Job Alert Settings**\n\n"
        f"• **Country:** {COUNTRY_NAMES.get(prefs['country'], prefs['country'])}\n"
        f"• **City / Location:** {city_label}\n"
        f"• **Job Category:** {TIER_NAMES.get(prefs['tier'], prefs['tier'])}\n\n"
        "Select what you would like to change:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🌐 Change Country", callback_data="menu_country")],
                [InlineKeyboardButton("📍 Change Location / City", callback_data="menu_city")],
                [InlineKeyboardButton("🛠 Change Job Category", callback_data="menu_tier")],
            ]
        ),
        parse_mode="Markdown",
    )


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    prefs = await asyncio.to_thread(get_user_preferences, user_id)

    if data == "menu_country":
        await query.message.edit_text("🌐 Select your target country:", reply_markup=get_country_keyboard())
    elif data == "menu_tier":
        await query.message.edit_text("🛠 Select your preferred job category:", reply_markup=get_tier_keyboard())
    elif data == "menu_city":
        await query.message.edit_text(
            "📍 Select a city, share your location, or type a custom name:",
            reply_markup=get_city_keyboard(prefs["country"]),
        )
    elif data.startswith("set_country_"):
        code = data.removeprefix("set_country_")
        if code not in COUNTRY_NAMES:
            return
        await asyncio.to_thread(update_user_preference, user_id, country=code)
        await query.message.edit_text(
            f"Country set to **{COUNTRY_NAMES[code]}**!\n\nStep 2: Select your job category:",
            reply_markup=get_tier_keyboard(),
            parse_mode="Markdown",
        )
    elif data.startswith("set_tier_"):
        tier = data.removeprefix("set_tier_")
        if tier not in TIER_NAMES:
            return
        await asyncio.to_thread(update_user_preference, user_id, tier=tier)
        prefs = await asyncio.to_thread(get_user_preferences, user_id)
        await query.message.edit_text(
            f"Job Category set to **{TIER_NAMES[tier]}**!\n\nStep 3: Select your city or location:",
            reply_markup=get_city_keyboard(prefs["country"]),
            parse_mode="Markdown",
        )
    elif data.startswith("set_city_"):
        city = data.removeprefix("set_city_")
        await asyncio.to_thread(update_user_preference, user_id, city=city)
        prefs = await asyncio.to_thread(get_user_preferences, user_id)
        await query.message.edit_text(
            "🎉 **Setup Complete!**\n\n"
            f"• **Country:** {COUNTRY_NAMES.get(prefs['country'], prefs['country'])}\n"
            f"• **Location:** {prefs['city'] or 'Entire Country'}\n"
            f"• **Category:** {TIER_NAMES.get(prefs['tier'], prefs['tier'])}\n\n"
            "You will now receive automatic notifications for new matching jobs.\n"
            "Use `/settings` anytime to change your filters.",
            parse_mode="Markdown",
        )
    elif data == "prompt_custom_city":
        context.user_data["awaiting_city_input"] = True
        await query.message.reply_text("✍️ Please type the name of your city in the chat:")
    elif data == "prompt_share_location":
        await query.message.reply_text(
            "Tap the button below to send your location:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Share Current Location", request_location=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_city_input"):
        return
    context.user_data["awaiting_city_input"] = False
    city = update.message.text.strip().title()
    if city:
        await asyncio.to_thread(update_user_preference, update.effective_user.id, city=city)
        await update.message.reply_text(
            f"✅ City set to **{city}**!\nType `/settings` anytime to change your preferences.",
            parse_mode="Markdown",
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    location_msg = update.message.location
    try:
        location = await asyncio.to_thread(
            geolocator.reverse,
            (location_msg.latitude, location_msg.longitude),
            exactly_one=True,
            language="en",
            timeout=10,
        )
        raw = location.raw if location else {}
        address = raw.get("address") or raw.get("components") or {}
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("state")
            or "Unknown"
        )
        await asyncio.to_thread(update_user_preference, user_id, city=city)
        await update.message.reply_text(
            f"✅ Reverse-geocoded location to **{city}**!\n"
            "Your job alerts are now filtered for this area. Type `/settings` to modify.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Reverse geocoding failed for user %s", user_id)
        await update.message.reply_text(
            "Could not process your location. Please select or type your city manually using `/settings`.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def scrape_once():
    users = await asyncio.to_thread(get_all_active_users)
    if not users:
        return 0

    combinations = sorted({
        (u["country"], u["city"] or "", u["tier"])
        for u in users
    })

    logger.info("Scrape cycle: %d unique filter combinations", len(combinations))
    total = 0
    for country, city, tier in combinations:
        try:
            jobs = await asyncio.to_thread(fetch_all_jobs, country, city, tier)
            total += await asyncio.to_thread(upsert_jobs, jobs)
        except Exception:
            logger.exception("Failed scraping %s/%s/%s", country, city, tier)
    return total


async def notification_once(context: ContextTypes.DEFAULT_TYPE):
    users = await asyncio.to_thread(get_all_active_users)
    for user in users:
        user_id = user["user_id"]
        jobs = await asyncio.to_thread(get_unsent_jobs_for_user, user_id, DIGEST_SIZE)
        if not jobs:
            continue

        lines = ["🚨 <b>New Job Alerts</b>\n"]
        for job in jobs:
            lines.append(
                f"• <b>{html.escape(job['title'])}</b>\n"
                f"  {html.escape(job['company'])} · {html.escape(job['location'] or 'Remote')}\n"
                f"  {html.escape(job['source'])} · "
                f'<a href="{html.escape(job["url"], quote=True)}">Apply</a>\n'
            )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await asyncio.to_thread(mark_jobs_sent, user_id, [j["id"] for j in jobs])
            await asyncio.sleep(0.05)
        except RetryAfter as exc:
            logger.warning("Telegram rate limited user %s for %ss", user_id, exc.retry_after)
            await asyncio.sleep(float(exc.retry_after))
        except Forbidden:
            logger.info("User %s blocked the bot; disabling alerts", user_id)
            await asyncio.to_thread(update_user_preference, user_id, active=0)
        except Exception:
            logger.exception("Failed sending digest to %s", user_id)


async def scrape_job(context: ContextTypes.DEFAULT_TYPE):
    inserted = await scrape_once()
    logger.info("Scrape cycle complete; %s new/updated jobs processed", inserted)


async def prune_job(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(prune_old_sent_jobs, RETENTION_DAYS)


async def post_init(application: Application):
    await scrape_once()


async def force_scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden command to manually trigger a scrape cycle."""
    await update.message.reply_text("🔄 Forcing a background job scrape... this might take a few seconds.")
    inserted = await scrape_once()
    await update.message.reply_text(f"✅ Scrape complete! {inserted} new/updated jobs processed.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("scrape", force_scrape_command))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(scrape_job, interval=SCRAPE_INTERVAL, first=SCRAPE_INTERVAL)
    app.job_queue.run_repeating(notification_once, interval=30, first=15)
    app.job_queue.run_repeating(prune_job, interval=86400, first=86400)

    logger.info("Job notifier starting: scrape=%ss digest=%s", SCRAPE_INTERVAL, DIGEST_SIZE)
    app.run_polling()


if __name__ == "__main__":
    main()