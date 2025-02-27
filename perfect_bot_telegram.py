import os
import json
import time
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode  # Исправлен импорт для версии 21.4
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import redis
from telegram.error import TelegramError
from collections import deque
from typing import Optional, Tuple

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')]
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CHANNEL_USERNAME = "@tpgbit"
BOT_USERNAME = "BitCurrencyBot"
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none", socket_timeout=10)

if not TELEGRAM_TOKEN or not CRYPTO_PAY_TOKEN:
    logger.critical("Missing TELEGRAM_TOKEN or CRYPTO_PAY_TOKEN")
    exit(1)

AD_MESSAGE = "\n\n📢 Подпишись на @tpgbit для новостей о крипте!"
FREE_REQUEST_LIMIT = 5
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 300
ADMIN_IDS = {"1058875848", "6403305626"}
HISTORY_LIMIT = 20
MAX_RETRIES = 3
HIGH_PRECISION_CURRENCIES = {'BTC', 'ETH', 'XRP', 'DOGE', 'ADA', 'SOL', 'LTC', 'BNB', 'TRX', 'DOT', 'MATIC'}

# API endpoints
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price"
WHITEBIT_API_URL = "https://whitebit.com/api/v1/public/ticker"

# Поддерживаемые валюты
CURRENCIES = {
    'usd': {'code': 'USDT'}, 'uah': {'code': 'UAH'}, 'eur': {'code': 'EUR'},
    'rub': {'code': 'RUB'}, 'jpy': {'code': 'JPY'}, 'cny': {'code': 'CNY'},
    'gbp': {'code': 'GBP'}, 'kzt': {'code': 'KZT'}, 'try': {'code': 'TRY'},
    'btc': {'code': 'BTC'}, 'eth': {'code': 'ETH'}, 'xrp': {'code': 'XRP'},
    'doge': {'code': 'DOGE'}, 'ada': {'code': 'ADA'}, 'sol': {'code': 'SOL'},
    'ltc': {'code': 'LTC'}, 'usdt': {'code': 'USDT'}, 'bnb': {'code': 'BNB'},
    'trx': {'code': 'TRX'}, 'dot': {'code': 'DOT'}, 'matic': {'code': 'MATIC'}
}
UAH_TO_USDT_FALLBACK = 0.0239
USDT_TO_UAH_FALLBACK = 41.84

def init_redis_connection() -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            redis_client.ping()
            logger.info("Connected to Redis")
            return True
        except redis.ConnectionError as e:
            logger.warning(f"Redis connection attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
            time.sleep(2 ** attempt)
    logger.critical("Failed to connect to Redis")
    return False

if not init_redis_connection():
    exit(1)

async def set_bot_commands(application):
    await application.bot.set_my_commands([
        ("start", "Главное меню"), ("currencies", "Список валют"), ("stats", "Статистика"),
        ("subscribe", "Подписка"), ("alert", "Уведомления"), ("referrals", "Рефералы"),
        ("history", "История запросов")
    ])
    logger.info("Bot commands set")

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except TelegramError as e:
        logger.error(f"Failed to check subscription for {user_id}: {e}")
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = str(update.effective_user.id)
    if await check_subscription(context, user_id):
        return True
    await update.effective_message.reply_text(
        "🚫 Подпишись на @tpgbit, чтобы продолжить!", parse_mode=ParseMode.MARKDOWN_V2
    )
    return False

def save_stats(user_id: str, request_type: str):
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        current_day = time.strftime("%Y-%m-%d")
        users = stats.setdefault("users", {})
        user_data = users.setdefault(user_id, {"requests": 0, "last_reset": current_day})
        if user_data["last_reset"] != current_day:
            user_data.update(requests=0, last_reset=current_day)
        user_data["requests"] += 1
        stats["total_requests"] = stats.get("total_requests", 0) + 1
        stats.setdefault("request_types", {}).setdefault(request_type, 0)
        stats["request_types"][request_type] += 1
        redis_client.setex('stats', 24 * 60 * 60, json.dumps(stats))
    except Exception as e:
        logger.error(f"Error saving stats for {user_id}: {e}")

def save_history(user_id: str, from_currency: str, to_currency: str, amount: float, result: float):
    try:
        history = deque(json.loads(redis_client.get(f"history:{user_id}") or '[]'), maxlen=HISTORY_LIMIT)
        history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from": from_currency, "to": to_currency,
            "amount": amount, "result": result
        })
        redis_client.setex(f"history:{user_id}", 30 * 24 * 60 * 60, json.dumps(list(history)))
    except Exception as e:
        logger.error(f"Error saving history for {user_id}: {e}")

def check_limit(user_id: str) -> Tuple[bool, str]:
    try:
        if user_id in ADMIN_IDS:
            return True, "∞"
        stats = json.loads(redis_client.get('stats') or '{}')
        if stats.get("subscriptions", {}).get(user_id):
            return True, "∞"
        users = stats.get("users", {})
        user_data = users.get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
        remaining = FREE_REQUEST_LIMIT - user_data["requests"]
        return remaining > 0, str(remaining)
    except Exception as e:
        logger.error(f"Error checking limit for {user_id}: {e}")
        return False, "0"

def get_exchange_rate(from_currency: str, to_currency: str, amount: float = 1.0) -> Tuple[Optional[float], str]:
    from_key, to_key = from_currency.lower(), to_currency.lower()
    cache_key = f"rate:{from_key}_{to_key}"
    cached = redis_client.get(cache_key)
    if cached:
        rate = float(cached)
        return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (cached)"

    from_data = CURRENCIES.get(from_key)
    to_data = CURRENCIES.get(to_key)
    if not from_data or not to_data:
        return None, "Неподдерживаемая валюта"
    from_code, to_code = from_data['code'], to_data['code']

    if from_key == to_key:
        redis_client.setex(cache_key, CACHE_TIMEOUT, 1.0)
        return amount, f"1 {from_key.upper()} = 1 {to_key.upper()}"

    def fetch_rate(url: str, key: str, reverse: bool = False, api_name: str = "API") -> Optional[float]:
        try:
            response = requests.get(url, timeout=5).json()
            rate = float(response[key if not reverse else 'price'])
            return 1 / rate if reverse and rate > 0 else rate if rate > 0 else None
        except (requests.RequestException, ValueError, KeyError):
            return None

    sources = [
        (BINANCE_API_URL, f"{from_code}{to_code}", False, "Binance direct"),
        (BINANCE_API_URL, f"{to_code}{from_code}", True, "Binance reverse"),
        (WHITEBIT_API_URL, f"{from_code}_{to_code}", False, "WhiteBIT direct"),
        (WHITEBIT_API_URL, f"{to_code}_{from_code}", True, "WhiteBIT reverse"),
    ]

    for url, pair, reverse, source in sources:
        rate = fetch_rate(url, 'price' if 'binance' in url else pair, reverse, source.split()[0])
        if rate:
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, f"1 {from_code} = {rate if not reverse else 1/rate} {to_code} ({source})"

    for bridge in ('USDT', 'BTC'):
        if from_key != bridge.lower() or to_key != bridge.lower():
            rate_from = fetch_rate(BINANCE_API_URL, f"{from_code}{bridge}") or fetch_rate(BINANCE_API_URL, f"{bridge}{from_code}", True)
            rate_to = fetch_rate(BINANCE_API_URL, f"{bridge}{to_code}") or fetch_rate(BINANCE_API_URL, f"{to_code}{bridge}", True)
            if rate_from and rate_to:
                rate = rate_from / rate_to if to_key != bridge.lower() else rate_from
                if rate > 0:
                    redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
                    return amount * rate, f"1 {from_code} = {rate} {to_code} (Binance via {bridge})"

    if from_key == 'uah' and to_key == 'usdt':
        rate = UAH_TO_USDT_FALLBACK
    elif from_key == 'usdt' and to_key == 'uah':
        rate = USDT_TO_UAH_FALLBACK
    else:
        return None, "Курс недоступен"
    redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
    return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (fallback)"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    save_stats(user_id, "start")
    keyboard = [
        [InlineKeyboardButton("💱 Конвертер", callback_data="converter"), InlineKeyboardButton("📈 Курсы", callback_data="price")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats"), InlineKeyboardButton("💎 Подписка", callback_data="subscribe")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="alert"), InlineKeyboardButton("👥 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton("📜 История", callback_data="history")]
    ]
    await update.effective_message.reply_text(
        f"👋 *Привет!* Я {BOT_USERNAME} — твой помощник для конвертации валют!\n"
        f"🔑 *Бесплатно:* {FREE_REQUEST_LIMIT} запросов в сутки\n"
        f"🌟 *Безлимит:* /subscribe за {SUBSCRIPTION_PRICE} USDT{AD_MESSAGE}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    await update.effective_message.reply_text(
        f"💱 *Поддерживаемые валюты:*\n{', '.join(sorted(CURRENCIES.keys()))}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")]]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    args = context.args
    if len(args) != 3 or not args[2].replace('.', '', 1).isdigit():
        keyboard = [
            [InlineKeyboardButton("🔔 USD → BTC", callback_data="alert_example_usd_btc")],
            [InlineKeyboardButton("🔔 EUR → UAH", callback_data="alert_example_eur_uah")],
            [InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        await update.effective_message.reply_text(
            "🔔 *Настрой уведомления!* Формат: `/alert <валюта1> <валюта2> <курс>`\nПримеры ниже:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        await update.effective_message.reply_text("❌ Ошибка: валюта не поддерживается", parse_mode=ParseMode.MARKDOWN_V2)
        return

    alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
    alerts.append({"from": from_currency, "to": to_currency, "target": target_rate})
    redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(alerts))
    await update.effective_message.reply_text(
        f"🔔 *Уведомление:* {from_currency.upper()} → {to_currency.upper()} при курсе {target_rate}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Добавить ещё", callback_data="alert"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
    text = (f"📊 *Админ-статистика:*\n"
            f"👥 Пользователей: {len(stats.get('users', {}))}\n"
            f"📈 Запросов: {stats.get('total_requests', 0)}\n"
            f"💰 Доход: {stats.get('revenue', 0.0)} USDT") if user_id in ADMIN_IDS else \
           f"📊 *Твоя статистика:*\n📈 Запросов сегодня: {stats.get('users', {}).get(user_id, {}).get('requests', 0)}"
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    if stats.get("subscriptions", {}).get(user_id):
        await update.effective_message.reply_text("💎 Ты уже подписан!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        response = requests.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN},
            json={"asset": "USDT", "amount": str(SUBSCRIPTION_PRICE), "description": f"Подписка для {user_id}"},
            timeout=15
        ).json()
        if response.get("ok"):
            invoice_id = response["result"]["invoice_id"]
            pay_url = response["result"]["pay_url"]
            context.user_data[user_id] = {"invoice_id": invoice_id}
            await update.effective_message.reply_text(
                f"💎 Оплати *{SUBSCRIPTION_PRICE} USDT* для безлимита:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)],
                    [InlineKeyboardButton("🔙 Назад", callback_data="start")]
                ]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await update.effective_message.reply_text(f"❌ Ошибка платежа: {response.get('error', 'Неизвестно')}", parse_mode=ParseMode.MARKDOWN_V2)
    except requests.RequestException as e:
        logger.error(f"Subscribe error for {user_id}: {e}")
        await update.effective_message.reply_text("❌ Ошибка связи с платежной системой", parse_mode=ParseMode.MARKDOWN_V2)

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
    await update.effective_message.reply_text(
        f"👥 *Реф. ссылка:* `{ref_link}`\n👤 Приглашено: *{refs}*\n🌟 Бонусы скоро будут!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Копировать", callback_data="copy_ref"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    history = json.loads(redis_client.get(f"history:{user_id}") or '[]')
    if not history:
        await update.effective_message.reply_text(
            "📜 *История пуста.*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")]]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    response = "📜 *История запросов:*\n" + "\n".join(
        f"⏰ {entry['time']}: *{entry['amount']} {entry['from']}* → *{entry['result']} {entry['to']}*"
        for entry in reversed(history)
    )
    await update.effective_message.reply_text(
        response, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")]]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if context.args and context.args[0].startswith("ref_"):
        referrer_id = context.args[0].replace("ref_", "")
        if referrer_id.isdigit() and user_id not in json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]'):
            referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
            referrals.append(user_id)
            redis_client.setex(f"referrals:{referrer_id}", 30 * 24 * 60 * 60, json.dumps(referrals))
            await update.effective_message.reply_text("👥 Спасибо за присоединение по реф. ссылке!", parse_mode=ParseMode.MARKDOWN_V2)

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    for user_id, data in list(context.user_data.items()):
        if "invoice_id" not in data:
            continue
        try:
            response = requests.get(
                f"https://pay.crypt.bot/api/getInvoices?invoice_ids={data['invoice_id']}",
                headers={'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN},
                timeout=15
            ).json()
            if response.get("ok") and response["result"]["items"][0]["status"] == "paid":
                stats = json.loads(redis_client.get('stats') or '{}')
                stats.setdefault("subscriptions", {})[user_id] = True
                stats["revenue"] = stats.get("revenue", 0.0) + SUBSCRIPTION_PRICE
                redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps(stats))
                del context.user_data[user_id]
                await context.bot.send_message(user_id, "💎 Оплата прошла! Безлимит активирован.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Payment check error for {user_id}: {e}")

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    stats = json.loads(redis_client.get('stats') or '{}')
    for user_id in stats.get("users", {}):
        alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
        if not alerts:
            continue
        updated_alerts = []
        for alert in alerts:
            result, rate_info = get_exchange_rate(alert["from"], alert["to"])
            if result and float(rate_info.split()[2]) <= alert["target"]:
                from_code, to_code = CURRENCIES[alert["from"]]['code'], CURRENCIES[alert["to"]]['code']
                await context.bot.send_message(
                    user_id, f"🔔 *Уведомление!* {from_code} → {to_code}: {float(rate_info.split()[2]):.8f} (цель: {alert['target']})",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                updated_alerts.append(alert)
        redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(updated_alerts))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id)
    delay = 1 if is_subscribed else 5

    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await update.effective_message.reply_text(f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await update.effective_message.reply_text(f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. /subscribe", parse_mode=ParseMode.MARKDOWN_V2)
        return

    context.user_data['last_request'] = time.time()
    text = update.effective_message.text.lower().split()
    try:
        amount = float(text[0]) if text[0].replace('.', '', 1).isdigit() else 1.0
        from_currency, to_currency = text[1 if amount != 1.0 else 0], text[2 if amount != 1.0 else 1]
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate_info = get_exchange_rate(from_currency, to_currency, amount)
        if result is None:
            raise ValueError(rate_info)

        from_code, to_code = CURRENCIES[from_currency.lower()]['code'], CURRENCIES[to_currency.lower()]['code']
        precision = 8 if to_code in HIGH_PRECISION_CURRENCIES else 6
        await update.effective_message.reply_text(
            f"💰 *{amount:.1f} {from_code}* = *{result:.{precision}f} {to_code}*\n"
            f"📈 {rate_info}\n🔄 Осталось: *{'∞' if is_subscribed else remaining}*{AD_MESSAGE}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                [InlineKeyboardButton("💱 Другая пара", callback_data="converter"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        save_history(user_id, from_code, to_code, amount, result)
    except (IndexError, ValueError) as e:
        await update.effective_message.reply_text(
            f"❌ Ошибка: {str(e) if isinstance(e, ValueError) else 'Неверный формат'}\nПример: `100 uah usdt`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💱 Попробовать снова", callback_data="converter")]]),
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    if not await enforce_subscription(update, context):
        await query.edit_message_text("🚫 Подпишись на @tpgbit!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id)
    delay = 1 if is_subscribed else 5
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await query.edit_message_text(f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await query.edit_message_text(f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. /subscribe", parse_mode=ParseMode.MARKDOWN_V2)
        return

    context.user_data['last_request'] = time.time()
    action = query.data

    if action == "start":
        await start(update, context)
    elif action == "converter":
        await query.edit_message_text(
            "💱 *Выбери пару или введи вручную (например, '100 uah usdt'):*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 USD → BTC", callback_data="convert:usd:btc"), InlineKeyboardButton("💶 EUR → UAH", callback_data="convert:eur:uah")],
                [InlineKeyboardButton("₿ BTC → ETH", callback_data="convert:btc:eth"), InlineKeyboardButton("₴ UAH → USDT", callback_data="convert:uah:usdt")],
                [InlineKeyboardButton("🔄 Ввести вручную", callback_data="manual_convert"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    elif action.startswith("convert:"):
        _, from_currency, to_currency = action.split(":")
        result, rate_info = get_exchange_rate(from_currency, to_currency)
        if result:
            from_code, to_code = CURRENCIES[from_currency]['code'], CURRENCIES[to_currency]['code']
            precision = 8 if to_code in HIGH_PRECISION_CURRENCIES else 6
            await query.edit_message_text(
                f"💰 *1.0 {from_code}* = *{result:.{precision}f} {to_code}*\n"
                f"📈 {rate_info}\n🔄 Осталось: *{'∞' if is_subscribed else remaining}*{AD_MESSAGE}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                    [InlineKeyboardButton("💱 Другая пара", callback_data="converter"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
                ]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            save_history(user_id, from_code, to_code, 1.0, result)
        else:
            await query.edit_message_text(f"❌ Ошибка: {rate_info}", parse_mode=ParseMode.MARKDOWN_V2)
    elif action == "manual_convert":
        await query.edit_message_text("💱 *Введи запрос вручную:* например, '100 uah usdt'", parse_mode=ParseMode.MARKDOWN_V2)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("currencies", currencies))
    app.add_handler(CommandHandler("alert", alert))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("referrals", referrals))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button))

    app.job_queue.run_repeating(check_payment_job, interval=60, name="check_payment")
    app.job_queue.run_repeating(check_alerts_job, interval=60, name="check_alerts")
    app.post_init = set_bot_commands

    if not redis_client.exists('stats'):
        redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps({"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, timeout=30)

if __name__ == "__main__":
    while True:
        try:
            main()
        except TelegramError as e:
            logger.error(f"Telegram error: {e}. Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            logger.critical(f"Fatal error: {e}. Retrying in 10 seconds...")
            time.sleep(10)
