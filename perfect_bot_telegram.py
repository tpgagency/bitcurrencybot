import os
import json
import time
import logging
import requests
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import redis
from telegram.error import NetworkError, RetryAfter, TelegramError
from collections import deque
from telegram.constants import ParseMode

# Настройка логирования с записью в файл для удобства анализа
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Логи в консоль
        logging.FileHandler('bot.log')  # Логи в файл для проверки
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация (сохранена без изменений, но добавлены комментарии)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CHANNEL_USERNAME = "@tpgbit"  # Обязательный канал для подписки
BOT_USERNAME = "BitCurrencyBot"
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none")

if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN not set")
    exit(1)
if not CRYPTO_PAY_TOKEN:
    logger.error("CRYPTO_PAY_TOKEN not set")
    exit(1)

AD_MESSAGE = "\n\n📢 Подпишись на @tpgbit для новостей о крипте!"  # Сообщение с каналом
FREE_REQUEST_LIMIT = 5  # Лимит 5 запросов для не-подписчиков
SUBSCRIPTION_PRICE = 5  # Цена подписки
CACHE_TIMEOUT = 5  # Время кэша курсов
ADMIN_IDS = ["1058875848", "6403305626"]  # Безлимит для этих пользователей
HISTORY_LIMIT = 10  # Лимит истории

# API endpoints (сохранены без изменений)
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price"
WHITEBIT_API_URL = "https://whitebit.com/api/v1/public/ticker"

# Поддерживаемые валюты (сохранены без изменений)
CURRENCIES = {
    'usd': {'code': 'USDT'},
    'uah': {'code': 'UAH'},
    'eur': {'code': 'EUR'},
    'rub': {'code': 'RUB'},
    'jpy': {'code': 'JPY'},
    'cny': {'code': 'CNY'},
    'gbp': {'code': 'GBP'},
    'kzt': {'code': 'KZT'},
    'try': {'code': 'TRY'},
    'btc': {'code': 'BTC'},
    'eth': {'code': 'ETH'},
    'xrp': {'code': 'XRP'},
    'doge': {'code': 'DOGE'},
    'ada': {'code': 'ADA'},
    'sol': {'code': 'SOL'},
    'ltc': {'code': 'LTC'},
    'usdt': {'code': 'USDT'},
    'bnb': {'code': 'BNB'},
    'trx': {'code': 'TRX'},
    'dot': {'code': 'DOT'},
    'matic': {'code': 'MATIC'}
}

# Fallback курсы (сохранены без изменений)
UAH_TO_USDT_FALLBACK = 0.0239
USDT_TO_UAH_FALLBACK = 41.84

# Проверка подключения к Redis
try:
    redis_client.ping()  # Проверяем, работает ли Redis
    logger.info("Connected to Redis successfully")
except redis.ConnectionError as e:
    logger.error(f"Failed to connect to Redis: {e}")
    exit(1)

async def set_bot_commands(application: Application):
    """Установка команд с обработкой ошибок."""
    commands = [
        ("start", "Главное меню"),
        ("currencies", "Список валют"),
        ("stats", "Статистика"),
        ("subscribe", "Подписка"),
        ("alert", "Уведомления"),
        ("referrals", "Рефералы"),
        ("history", "История запросов")
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands set successfully")
    except TelegramError as e:
        logger.error(f"Failed to set bot commands: {e}")

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка подписки на @tpgbit с детальным логированием."""
    user_id = str(update.effective_user.id)
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = chat_member.status in ['member', 'administrator', 'creator']
        logger.debug(f"User {user_id} subscription status: {is_subscribed} ({chat_member.status})")
        return is_subscribed
    except TelegramError as e:
        logger.error(f"Error checking subscription for {user_id}: {e}")
        await update.effective_message.reply_text(
            "❌ Не могу проверить подписку. Убедись, что бот — админ в @tpgbit, и попробуй снова.",
            parse_mode=ParseMode.MARKDOWN
        )
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обязательная проверка подписки (сохранена логика)."""
    if await check_subscription(update, context):
        return True
    await update.effective_message.reply_text(
        "🚫 Чтобы пользоваться ботом, подпишись на @tpgbit!\nПосле подписки повтори запрос.",
        parse_mode=ParseMode.MARKDOWN
    )
    return False

def save_stats(user_id: str, request_type: str):
    """Сохранение статистики с TTL для Redis."""
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        current_day = time.strftime("%Y-%m-%d")
        users = stats.setdefault("users", {})
        user_data = users.setdefault(user_id, {"requests": 0, "last_reset": current_day})
        
        if user_data["last_reset"] != current_day:
            user_data["requests"] = 0
            user_data["last_reset"] = current_day
        
        user_data["requests"] += 1
        stats["total_requests"] = stats.get("total_requests", 0) + 1
        stats["request_types"] = stats.get("request_types", {})
        stats["request_types"][request_type] = stats["request_types"].get(request_type, 0) + 1
        redis_client.setex('stats', 24 * 60 * 60, json.dumps(stats))  # Кэш на 24 часа
        logger.debug(f"Stats updated: {user_id} - {request_type}")
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def save_history(user_id: str, from_currency: str, to_currency: str, amount: float, result: float):
    """Сохранение истории с TTL."""
    try:
        history = json.loads(redis_client.get(f"history:{user_id}") or '[]')
        history = deque(history, maxlen=HISTORY_LIMIT)
        history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from": from_currency,
            "to": to_currency,
            "amount": amount,
            "result": result
        })
        redis_client.setex(f"history:{user_id}", 30 * 24 * 60 * 60, json.dumps(list(history)))  # Кэш на 30 дней
        logger.debug(f"History updated for {user_id}")
    except Exception as e:
        logger.error(f"Error saving history for {user_id}: {e}")

def check_limit(user_id: str) -> tuple[bool, str]:
    """Проверка лимита (сохранена логика для ADMIN_IDS)."""
    try:
        if user_id in ADMIN_IDS:  # Безлимит для 1058875848, 6403305626
            logger.debug(f"Admin {user_id} - unlimited access")
            return True, "∞"
        
        stats = json.loads(redis_client.get('stats') or '{}')
        if stats.get("subscriptions", {}).get(user_id, False):
            logger.debug(f"Subscribed user {user_id} - unlimited access")
            return True, "∞"
        
        users = stats.get("users", {})
        user_data = users.get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
        remaining = FREE_REQUEST_LIMIT - user_data["requests"]
        logger.debug(f"User {user_id} has {remaining} requests left")
        return remaining > 0, str(remaining)
    except Exception as e:
        logger.error(f"Error checking limit: {e}")
        return False, "0"

def get_exchange_rate(from_currency: str, to_currency: str, amount: float = 1.0) -> tuple[float, float] | tuple[None, str]:
    """Получение курса с улучшенной обработкой ошибок."""
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"rate:{from_key}_{to_key}"
    
    cached = redis_client.get(cache_key)
    if cached:
        rate = float(cached)
        logger.info(f"Cache hit (real-time): {from_key} to {to_key} = {rate}")
        return amount * rate, rate
    
    from_data = CURRENCIES.get(from_key)
    to_data = CURRENCIES.get(to_key)
    if not from_data or not to_data:
        logger.error(f"Invalid currency: {from_key} or {to_key}")
        return None, "Неподдерживаемая валюта"
    
    from_code = from_data['code']
    to_code = to_data['code']

    if from_key == to_key:
        rate = 1.0
        redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
        return amount * rate, rate

    # Binance API с таймаутом и обработкой ошибок
    try:
        pair = f"{from_code}{to_code}"
        response = requests.get(f"{BINANCE_API_URL}?symbol={pair}", timeout=5).json()
        if 'price' in response:
            rate = float(response['price'])
            if rate <= 0:
                raise ValueError(f"Invalid Binance rate for {pair}: {rate}")
            logger.info(f"Binance direct rate (real-time): {pair} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning(f"Binance API failed for {from_key} to {to_key}: {e}")

    # WhiteBIT API с таймаутом и обработкой ошибок
    try:
        response = requests.get(WHITEBIT_API_URL, timeout=5).json()
        pair_key = f"{from_code}_{to_code}"
        if pair_key in response:
            rate = float(response[pair_key]['last_price'])
            if rate <= 0:
                raise ValueError(f"Invalid WhiteBIT rate for {pair_key}: {rate}")
            logger.info(f"WhiteBIT direct rate (real-time): {pair_key} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning(f"WhiteBIT API failed for {from_key} to {to_key}: {e}")

    # Fallback для UAH-USDT
    try:
        if from_key == 'uah' and to_key == 'usdt':
            rate = UAH_TO_USDT_FALLBACK
            logger.info(f"Using fallback: {from_key} to {to_key} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        elif from_key == 'usdt' and to_key == 'uah':
            rate = USDT_TO_UAH_FALLBACK
            logger.info(f"Using fallback: {from_key} to {to_key} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        logger.error(f"No real-time rate found for {from_key} to {to_key}")
        return None, "Курс недоступен: данные отсутствуют"
    except Exception as e:
        logger.error(f"Fallback error: {e}")
        return None, "Курс недоступен: внутренняя ошибка"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Функция старта с улучшенным логированием ошибок."""
    if not await enforce_subscription(update, context):
        logger.debug(f"User {update.effective_user.id} blocked by subscription")
        return
    user_id = str(update.effective_user.id)
    save_stats(user_id, "start")
    logger.info(f"User {user_id} started bot")
    
    keyboard = [
        [InlineKeyboardButton("💱 Конвертер", callback_data="converter"),
         InlineKeyboardButton("📈 Курсы", callback_data="price")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats"),
         InlineKeyboardButton("💎 Подписка", callback_data="subscribe")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="alert"),
         InlineKeyboardButton("👥 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton("📜 История", callback_data="history")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.effective_message.reply_text(
            "👋 *Привет!* Я BitCurrencyBot — твой идеальный помощник для конвертации валют в реальном времени!\n"
            "🌟 Выбери действие ниже или напиши запрос (например, \"usd btc\" или \"100 uah usdt\").\n"
            f"🔑 *Бесплатно:* {FREE_REQUEST_LIMIT} запросов в сутки.\n"
            f"🌟 *Безлимит:* /subscribe за {SUBSCRIPTION_PRICE} USDT.{AD_MESSAGE}",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as e:
        logger.error(f"Error sending start message to {user_id}: {e}")

async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список валют с улучшенным логированием ошибок."""
    if not await enforce_subscription(update, context):
        return
    currency_list = ", ".join(sorted(CURRENCIES.keys()))
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.effective_message.reply_text(
            f"💱 *Поддерживаемые валюты:*\n{currency_list}",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as e:
        logger.error(f"Error sending currencies to {update.effective_user.id}: {e}")

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Уведомления с улучшенной обработкой ошибок."""
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
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await update.effective_message.reply_text(
                "🔔 *Настрой уведомления!* Введи в формате: `/alert <валюта1> <валюта2> <курс>`\n"
                "Примеры доступны ниже:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending alert menu to {user_id}: {e}")
        return
    
    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        try:
            await update.effective_message.reply_text(
                "❌ Ошибка: одна из валют не поддерживается",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending currency error to {user_id}: {e}")
        return
    
    alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
    alerts.append({"from": from_currency, "to": to_currency, "target": target_rate})
    redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(alerts))  # TTL на 30 дней
    keyboard = [
        [InlineKeyboardButton("🔔 Добавить ещё", callback_data="alert"),
         InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.effective_message.reply_text(
            f"🔔 *Уведомление установлено:* {from_currency.upper()} → {to_currency.upper()} при курсе *{target_rate}*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as e:
        logger.error(f"Error sending alert confirmation to {user_id}: {e}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика с улучшенным логированием ошибок."""
    user_id = str(update.effective_user.id)
    if not await enforce_subscription(update, context):
        return
    
    stats = json.loads(redis_client.get('stats') or '{}')
    users = len(stats.get("users", {}))
    requests = stats.get("total_requests", 0)
    revenue = stats.get("revenue", 0.0)
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if user_id in ADMIN_IDS:  # Сохранена логика для администраторов
            await update.effective_message.reply_text(
                f"📊 *Админ-статистика:*\n👥 Пользователей: *{users}*\n📈 Запросов: *{requests}*\n💰 Доход: *{revenue} USDT*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.effective_message.reply_text(
                f"📊 *Твоя статистика:*\n📈 Запросов сегодня: *{stats.get('users', {}).get(user_id, {}).get('requests', 0)}*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    except TelegramError as e:
        logger.error(f"Error sending stats to {user_id}: {e}")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подписка с улучшенной обработкой ошибок."""
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    if stats.get("subscriptions", {}).get(user_id, False):
        logger.info(f"User {user_id} already subscribed")
        try:
            await update.effective_message.reply_text(
                "💎 Ты уже подписан!",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending subscription status to {user_id}: {e}")
        return
    
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(SUBSCRIPTION_PRICE),
        "description": f"Подписка для {user_id}"
    }
    try:
        response = requests.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload, timeout=15).json()
        logger.info(f"Invoice response for {user_id}: {json.dumps(response)}")
        if response.get("ok"):
            invoice_id = response["result"]["invoice_id"]
            pay_url = response["result"]["pay_url"]
            keyboard = [
                [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)],
                [InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]
            context.user_data[user_id] = {"invoice_id": invoice_id}
            try:
                await update.effective_message.reply_text(
                    f"💎 Оплати *{SUBSCRIPTION_PRICE} USDT* для безлимита:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            except TelegramError as e:
                logger.error(f"Error sending payment message to {user_id}: {e}")
        else:
            logger.error(f"Invoice failed for {user_id}: {response}")
            await update.effective_message.reply_text(
                f"❌ Ошибка платежа: {response.get('error', 'Неизвестная ошибка')}",
                parse_mode=ParseMode.MARKDOWN
            )
    except requests.RequestException as e:
        logger.error(f"Subscribe error for {user_id}: {e}")
        await update.effective_message.reply_text(
            "❌ Ошибка связи с платежной системой",
            parse_mode=ParseMode.MARKDOWN
        )

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рефералы с улучшенным логированием ошибок."""
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
    keyboard = [
        [InlineKeyboardButton("🔗 Копировать ссылку", callback_data="copy_ref"),
         InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.effective_message.reply_text(
            f"👥 *Твоя реферальная ссылка:* `{ref_link}`\n"
            f"👤 Приглашено пользователей: *{refs}*\n"
            "🌟 Приглашай друзей и получай бонусы (скоро будет доступно)!",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as e:
        logger.error(f"Error sending referrals to {user_id}: {e}")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """История запросов с улучшенным логированием ошибок."""
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    history = json.loads(redis_client.get(f"history:{user_id}") or '[]')
    if not history:
        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await update.effective_message.reply_text(
                "📜 *История запросов пуста.*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending empty history to {user_id}: {e}")
        return
    
    response = "📜 *История твоих запросов:*\n"
    for entry in reversed(history):
        response += f"⏰ {entry['time']}: *{entry['amount']} {entry['from']}* → *{entry['result']} {entry['to']}*\n"
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.effective_message.reply_text(response, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except TelegramError as e:
        logger.error(f"Error sending history to {user_id}: {e}")

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка рефералов с улучшенным логированием ошибок."""
    user_id = str(update.effective_user.id)
    args = context.args
    if len(args) == 1 and args[0].startswith("ref_"):
        referrer_id = args[0].replace("ref_", "")
        if referrer_id.isdigit():
            referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
            if user_id not in referrals:
                referrals.append(user_id)
                redis_client.setex(f"referrals:{referrer_id}", 30 * 24 * 60 * 60, json.dumps(referrals))  # TTL на 30 дней
                logger.info(f"New referral: {user_id} for {referrer_id}")
                try:
                    await update.effective_message.reply_text(
                        "👥 Ты был приглашён через реферальную ссылку! Спасибо!",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except TelegramError as e:
                    logger.error(f"Error sending referral message to {user_id}: {e}")

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверка платежей с улучшенным логированием и обработкой ошибок."""
    try:
        if not hasattr(context, 'user_data') or not context.user_data:
            logger.debug("No user_data available in context, skipping payment check")
            return
        
        for user_id, data in list(context.user_data.items()):
            if "invoice_id" not in data:
                continue
            invoice_id = data["invoice_id"]
            headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
            try:
                url = f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}"
                response = requests.get(url, headers=headers, timeout=15).json()
                logger.info(f"Payment check for {user_id}: {json.dumps(response)}")
                if response.get("ok") and response["result"]["items"]:
                    status = response["result"]["items"][0]["status"]
                    if status == "paid":
                        stats = json.loads(redis_client.get('stats') or '{}')
                        stats.setdefault("subscriptions", {})[user_id] = True
                        stats["revenue"] = stats.get("revenue", 0.0) + SUBSCRIPTION_PRICE
                        redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps(stats))  # TTL на 30 дней
                        del context.user_data[user_id]
                        logger.info(f"Payment confirmed for {user_id}")
                        try:
                            await context.bot.send_message(
                                user_id,
                                "💎 Оплата прошла! У тебя безлимит.",
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except TelegramError as e:
                            logger.error(f"Error sending payment confirmation to {user_id}: {e}")
            except requests.RequestException as e:
                logger.warning(f"Payment check failed for {user_id}: {e}")
    except Exception as e:
        logger.error(f"Payment check error: {e}")

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверка алертов с улучшенным логированием и обработкой ошибок."""
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        for user_id in stats.get("users", {}):
            alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
            if not alerts:
                continue
            updated_alerts = []
            for alert in alerts:
                from_currency, to_currency, target_rate = alert["from"], alert["to"], alert["target"]
                result, current_rate = get_exchange_rate(from_currency, to_currency)
                if result and current_rate <= target_rate:
                    from_code = CURRENCIES[from_currency]['code']
                    to_code = CURRENCIES[to_currency]['code']
                    try:
                        await context.bot.send_message(
                            user_id,
                            f"🔔 *Уведомление!* Курс *{from_code} → {to_code}* достиг *{current_rate:.8f}* (цель: {target_rate})",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except TelegramError as e:
                        logger.error(f"Error sending alert to {user_id}: {e}")
                else:
                    updated_alerts.append(alert)
            redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(updated_alerts))  # TTL на 30 дней
            logger.debug(f"Checked alerts for user {user_id}")
    except Exception as e:
        logger.error(f"Alerts check error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений с улучшенной обработкой ошибок."""
    if not await enforce_subscription(update, context):
        logger.debug(f"User {update.effective_user.id} blocked by subscription")
        return
    
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        try:
            await update.effective_message.reply_text(
                f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}!",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending delay message to {user_id}: {e}")
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        try:
            await update.effective_message.reply_text(
                f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending limit exceeded message to {user_id}: {e}")
        return
    
    context.user_data['last_request'] = time.time()
    text = update.effective_message.text.lower()
    logger.info(f"Message from {user_id}: {text}")
    
    try:
        parts = text.split()
        if len(parts) < 2:
            raise ValueError("Недостаточно аргументов")
        if parts[0].replace('.', '', 1).isdigit():
            amount = float(parts[0])
            from_currency, to_currency = parts[1], parts[2]
            logger.debug(f"Parsed: amount={amount}, from={from_currency}, to={to_currency}")
        else:
            amount = 1.0
            from_currency, to_currency = parts[0], parts[1]
            logger.debug(f"Parsed: amount={amount}, from={from_currency}, to={to_currency}")
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        if result is not None:
            from_code = CURRENCIES[from_currency.lower()]['code']
            to_code = CURRENCIES[to_currency.lower()]['code']
            remaining_display = "∞" if is_subscribed else remaining
            precision = 8 if to_code in ['BTC', 'ETH', 'XRP', 'DOGE', 'ADA', 'SOL', 'LTC', 'BNB', 'TRX', 'DOT', 'MATIC'] else 6
            keyboard = [
                [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                [InlineKeyboardButton("💱 Другая пара", callback_data="converter"),
                 InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await update.effective_message.reply_text(
                    f"💰 *{amount:.1f} {from_code}* = *{result:.{precision}f} {to_code}*\n"
                    f"📈 Курс: 1 {from_code} = *{rate:.{precision}f} {to_code}*\n"
                    f"🔄 Осталось запросов: *{remaining_display}*{AD_MESSAGE}",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            except TelegramError as e:
                logger.error(f"Error sending conversion result to {user_id}: {e}")
            save_history(user_id, from_code, to_code, amount, result)
        else:
            try:
                await update.effective_message.reply_text(
                    f"❌ Ошибка: {rate}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except TelegramError as e:
                logger.error(f"Error sending error message to {user_id}: {e}")
    except Exception as e:
        logger.error(f"Message error for {user_id}: {e}")
        keyboard = [
            [InlineKeyboardButton("💱 Попробовать снова", callback_data="converter"),
             InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await update.effective_message.reply_text(
                '📝 *Примеры:* `"usd btc"` или `"100 uah usdt"`\nИли используй меню через /start',
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending example message to {user_id}: {e}")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок с улучшенным логированием и обработкой ошибок."""
    query = update.callback_query
    try:
        await query.answer(text="🌟 Обработка... 🌟", show_alert=True)
    except TelegramError as e:
        logger.error(f"Error answering callback for {query.from_user.id}: {e}")
    user_id = str(query.from_user.id)
    logger.debug(f"Processing callback: {query.data} for user {user_id}")
    
    if not await enforce_subscription(update, context):
        logger.debug(f"User {user_id} blocked by subscription")
        return
    
    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        try:
            await query.edit_message_text(
                f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}!",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending delay message to {user_id} in button: {e}")
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        try:
            await query.edit_message_text(
                f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending limit exceeded message to {user_id} in button: {e}")
        return
    
    context.user_data['last_request'] = time.time()
    action = query.data

    if action == "converter":
        keyboard = [
            [InlineKeyboardButton("💰 USD → BTC", callback_data="convert:usd:btc"),
             InlineKeyboardButton("💶 EUR → UAH", callback_data="convert:eur:uah")],
            [InlineKeyboardButton("₿ BTC → ETH", callback_data="convert:btc:eth"),
             InlineKeyboardButton("₴ UAH → USDT", callback_data="convert:uah:usdt")],
            [InlineKeyboardButton("🔄 Ввести вручную", callback_data="manual_convert"),
             InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(
                "💱 *Выбери валютную пару или введи вручную (например, \"100 uah usdt\"):*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending converter menu to {user_id}: {e}")
    elif action == "price":
        try:
            await query.edit_message_text(
                "📈 *Введи валюту для проверки текущей цены, например: \"btc usd\"*",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending price prompt to {user_id}: {e}")
    elif action == "stats":
        users = len(stats.get("users", {}))
        requests = stats.get("total_requests", 0)
        revenue = stats.get("revenue", 0.0)
        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            if user_id in ADMIN_IDS:  # Сохранена логика для администраторов
                await query.edit_message_text(
                    f"📊 *Админ-статистика:*\n👥 Пользователей: *{users}*\n📈 Запросов: *{requests}*\n💰 Доход: *{revenue} USDT*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text(
                    f"📊 *Твоя статистика:*\n📈 Запросов сегодня: *{stats.get('users', {}).get(user_id, {}).get('requests', 0)}*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        except TelegramError as e:
            logger.error(f"Error sending stats to {user_id}: {e}")
    elif action == "subscribe":
        try:
            await subscribe(update, context)
        except Exception as e:
            logger.error(f"Error processing subscribe for {user_id}: {e}")
    elif action == "alert":
        keyboard = [
            [InlineKeyboardButton("🔔 USD → BTC", callback_data="alert_example_usd_btc")],
            [InlineKeyboardButton("🔔 EUR → UAH", callback_data="alert_example_eur_uah")],
            [InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(
                "🔔 *Настрой уведомления!* Введи в формате: `/alert <валюта1> <валюта2> <курс>`\n"
                "Примеры доступны ниже:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending alert menu to {user_id}: {e}")
    elif action == "referrals":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
        keyboard = [
            [InlineKeyboardButton("🔗 Копировать ссылку", callback_data="copy_ref"),
             InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(
                f"👥 *Твоя реферальная ссылка:* `{ref_link}`\n"
                f"👤 Приглашено пользователей: *{refs}*\n"
                "🌟 Приглашай друзей и получай бонусы (скоро будет доступно)!",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending referrals to {user_id}: {e}")
    elif action == "history":
        try:
            await history(update, context)
        except Exception as e:
            logger.error(f"Error processing history for {user_id}: {e}")
    elif action == "alert_example_usd_btc":
        try:
            await query.edit_message_text(
                "🔔 Пример: `/alert usd btc 0.000015` — уведомит, когда 1 USD = 0.000015 BTC",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending USD-BTC alert example to {user_id}: {e}")
    elif action == "alert_example_eur_uah":
        try:
            await query.edit_message_text(
                "🔔 Пример: `/alert eur uah 45.0` — уведомит, когда 1 EUR = 45 UAH",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending EUR-UAH alert example to {user_id}: {e}")
    elif action == "manual_convert":
        try:
            await query.edit_message_text(
                "💱 *Введи запрос вручную:* например, \"100 uah usdt\"",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending manual convert prompt to {user_id}: {e}")
    elif action == "copy_ref":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        try:
            await query.answer(
                text=f"🌟 Скопировано: {ref_link} 🌟",
                show_alert=True
            )
        except TelegramError as e:
            logger.error(f"Error answering copy_ref callback for {user_id}: {e}")
        refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
        keyboard = [
            [InlineKeyboardButton("🔗 Копировать ссылку", callback_data="copy_ref"),
             InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(
                f"👥 *Реферальная ссылка скопирована:* `{ref_link}`\n"
                f"👤 Приглашено пользователей: *{refs}*\n"
                "🌟 Приглашай друзей и получай бонусы (скоро будет доступно)!",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Error sending referral link to {user_id}: {e}")
    elif action.startswith("convert:"):
        _, from_currency, to_currency = action.split(":")
        result, rate = get_exchange_rate(from_currency, to_currency)
        if result is not None:
            from_code = CURRENCIES[from_currency]['code']
            to_code = CURRENCIES[to_currency]['code']
            precision = 8 if to_code in ['BTC', 'ETH', 'XRP', 'DOGE', 'ADA', 'SOL', 'LTC', 'BNB', 'TRX', 'DOT', 'MATIC'] else 6
            keyboard = [
                [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                [InlineKeyboardButton("💱 Другая пара", callback_data="converter"),
                 InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await query.edit_message_text(
                    f"💰 *1.0 {from_code}* = *{result:.{precision}f} {to_code}*\n"
                    f"📈 Курс: 1 {from_code} = *{rate:.{precision}f} {to_code}*\n"
                    f"🔄 Осталось запросов: *{remaining}*{AD_MESSAGE}",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            except TelegramError as e:
                logger.error(f"Error sending conversion result to {user_id} in button: {e}")
            save_history(user_id, from_code, to_code, 1.0, result)
        else:
            try:
                await query.edit_message_text(
                    f"❌ Ошибка: {rate}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except TelegramError as e:
                logger.error(f"Error sending error message to {user_id} in button: {e}")

if __name__ == "__main__":
    # Создание приложения с улучшенным логированием
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("currencies", currencies))
    application.add_handler(CommandHandler("alert", alert))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("referrals", referrals))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button))

    # Настройка задач с именами для отладки
    application.job_queue.run_repeating(check_payment_job, interval=60, name="check_payment")
    application.job_queue.run_repeating(check_alerts_job, interval=60, name="check_alerts")

    # Инициализация статистики с TTL
    if not redis_client.exists('stats'):
        redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps({"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}))
    logger.info("Bot starting...")

    # Улучшенный цикл polling с повторными попытками
    while True:
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except NetworkError as e:
            logger.error(f"Network error: {e}. Retrying in 5 seconds...")
            time.sleep(5)
        except TelegramError as e:
            logger.error(f"Telegram error: {e}. Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            logger.critical(f"Fatal error: {e}. Exiting...")
            exit(1)
