import os
import json
import time
import logging
import requests
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import redis
from telegram.error import NetworkError, TelegramError
from collections import deque
from telegram.constants import ParseMode
from typing import Optional, Tuple, Dict
from aiohttp import ClientSession

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
    logger.critical("TELEGRAM_TOKEN or CRYPTO_PAY_TOKEN not set")
    exit(1)

AD_MESSAGE = "\n\n📢 Подпишись на @tpgbit для новостей о крипте!"
FREE_REQUEST_LIMIT = 5
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 300  # 5 минут
ADMIN_IDS = ["1058875848", "6403305626"]
HISTORY_LIMIT = 20
MAX_RETRIES = 3
RATE_LIMIT_DELAY = 0.2

# API endpoints
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price"
WHITEBIT_API_URL = "https://whitebit.com/api/v1/public/ticker"

# Поддерживаемые валюты
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

# Fallback курсы
UAH_TO_USDT_FALLBACK = 0.0239
USDT_TO_UAH_FALLBACK = 41.84

# Инициализация Redis
def init_redis_connection():
    for attempt in range(MAX_RETRIES):
        try:
            redis_client.ping()
            logger.info("Connected to Redis successfully")
            return True
        except redis.ConnectionError as e:
            logger.warning(f"Redis connection attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
            time.sleep(2 ** attempt)
    logger.critical("Failed to connect to Redis after all retries")
    exit(1)

if not init_redis_connection():
    exit(1)

async def set_bot_commands(application):
    commands = [
        ("start", "Главное меню"),
        ("currencies", "Список валют"),
        ("stats", "Статистика"),
        ("subscribe", "Подписка"),
        ("alert", "Уведомления"),
        ("referrals", "Рефералы"),
        ("history", "История запросов")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully")

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = str(update.effective_user.id)
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except TelegramError as e:
        logger.error(f"Failed to check subscription for {user_id}: {e}")
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_subscription(update, context):
        return True
    await update.effective_message.reply_text(
        f"🚫 Чтобы пользоваться ботом, подпишись на {CHANNEL_USERNAME}!\nПосле подписки повтори запрос.",
        parse_mode=ParseMode.MARKDOWN
    )
    return False

def save_stats(user_id: str, request_type: str):
    stats = redis_client.hgetall('stats') or {'users': '{}', 'total_requests': '0', 'request_types': '{}'}
    users = json.loads(stats['users'])
    current_day = time.strftime("%Y-%m-%d")
    user_data = users.setdefault(user_id, {"requests": 0, "last_reset": current_day})
    
    if user_data["last_reset"] != current_day:
        user_data["requests"] = 0
        user_data["last_reset"] = current_day
    
    user_data["requests"] += 1
    stats['total_requests'] = str(int(stats['total_requests']) + 1)
    request_types = json.loads(stats['request_types'])
    request_types[request_type] = request_types.get(request_type, 0) + 1
    redis_client.hmset('stats', {
        'users': json.dumps(users),
        'total_requests': stats['total_requests'],
        'request_types': json.dumps(request_types)
    })
    redis_client.expire('stats', 24 * 60 * 60)

def save_history(user_id: str, from_currency: str, to_currency: str, amount: float, result: float):
    history = json.loads(redis_client.get(f"history:{user_id}") or '[]')
    history = deque(history, maxlen=HISTORY_LIMIT)
    history.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "from": from_currency,
        "to": to_currency,
        "amount": amount,
        "result": result
    })
    redis_client.setex(f"history:{user_id}", 30 * 24 * 60 * 60, json.dumps(list(history)))

def check_limit(user_id: str) -> Tuple[bool, str]:
    if user_id in ADMIN_IDS:
        return True, "∞"
    
    stats = redis_client.hgetall('stats') or {'users': '{}', 'subscriptions': '{}'}
    subscriptions = json.loads(stats.get('subscriptions', '{}'))
    if subscriptions.get(user_id, False):
        return True, "∞"
    
    users = json.loads(stats.get('users', '{}'))
    user_data = users.get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
    remaining = FREE_REQUEST_LIMIT - user_data["requests"]
    return remaining > 0, str(remaining)

async def fetch_all_rates(session: ClientSession) -> Dict[str, float]:
    rates = {}
    try:
        async with session.get(BINANCE_API_URL) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data:
                    if 'symbol' in item and 'price' in item:
                        rates[item['symbol']] = float(item['price'])
    except Exception as e:
        logger.warning(f"Failed to fetch Binance rates: {e}")
    
    try:
        async with session.get(WHITEBIT_API_URL) as resp:
            if resp.status == 200:
                data = await resp.json()
                for pair, info in data.items():
                    rates[pair.replace('_', '')] = float(info['last_price'])
    except Exception as e:
        logger.warning(f"Failed to fetch WhiteBIT rates: {e}")
    
    return rates

async def get_exchange_rate(from_currency: str, to_currency: str, amount: float = 1.0) -> Tuple[Optional[float], str]:
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"rate:{from_key}_{to_key}"
    
    cached = redis_client.get(cache_key)
    if cached:
        rate = float(cached)
        return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (cached)"
    
    from_data = CURRENCIES.get(from_key)
    to_data = CURRENCIES.get(to_key)
    if not from_data or not to_data:
        return None, "Неподдерживаемая валюта"
    
    from_code = from_data['code']
    to_code = to_data['code']
    
    if from_key == to_key:
        rate = 1.0
        redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
        return amount * rate, f"1 {from_key.upper()} = 1 {to_key.upper()}"
    
    async with ClientSession() as session:
        rates = await fetch_all_rates(session)
        
        pair = f"{from_code}{to_code}"
        if pair in rates:
            rate = rates[pair]
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, f"1 {from_code} = {rate} {to_code} (direct)"
        
        reverse_pair = f"{to_code}{from_code}"
        if reverse_pair in rates:
            rate = 1 / rates[reverse_pair]
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, f"1 {from_code} = {rate} {to_code} (reverse)"
        
        if from_key != 'usdt' and to_key != 'usdt':
            from_usdt = rates.get(f"{from_code}USDT") or (1 / rates.get(f"USDT{from_code}", 0) if rates.get(f"USDT{from_code}") else 0)
            to_usdt = rates.get(f"{to_code}USDT") or (1 / rates.get(f"USDT{to_code}", 0) if rates.get(f"USDT{to_code}") else 0)
            if from_usdt and to_usdt:
                rate = from_usdt / to_usdt
                redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
                return amount * rate, f"1 {from_code} = {rate} {to_code} (via USDT)"
        
        if from_key != 'btc' and to_key != 'btc':
            from_btc = rates.get(f"{from_code}BTC") or (1 / rates.get(f"BTC{from_code}", 0) if rates.get(f"BTC{from_code}") else 0)
            to_btc = rates.get(f"{to_code}BTC") or (1 / rates.get(f"BTC{to_code}", 0) if rates.get(f"BTC{to_code}") else 0)
            if from_btc and to_btc:
                rate = from_btc / to_btc
                redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
                return amount * rate, f"1 {from_code} = {rate} {to_code} (via BTC)"
        
        if from_key == 'uah' and to_key == 'usdt':
            rate = UAH_TO_USDT_FALLBACK
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (fallback)"
        if from_key == 'usdt' and to_key == 'uah':
            rate = USDT_TO_UAH_FALLBACK
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (fallback)"
    
    return None, "Курс недоступен: данные отсутствуют"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    save_stats(user_id, "start")
    
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
    await update.effective_message.reply_text(
        "👋 *Привет!* Я BitCurrencyBot — твой помощник для конвертации валют!\n"
        "🌟 Выбери действие или напиши запрос (например, \"usd btc\" или \"100 uah usdt\").\n"
        f"🔑 *Бесплатно:* {FREE_REQUEST_LIMIT} запросов в сутки.\n"
        f"🌟 *Безлимит:* /subscribe за {SUBSCRIPTION_PRICE} USDT.{AD_MESSAGE}",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    currency_list = ", ".join(sorted(CURRENCIES.keys()))
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(
        f"💱 *Поддерживаемые валюты:*\n{currency_list}",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
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
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(
            "🔔 *Настрой уведомления!* Введи: `/alert <валюта1> <валюта2> <курс>`\nПримеры ниже:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        await update.effective_message.reply_text("❌ Ошибка: валюта не поддерживается", parse_mode=ParseMode.MARKDOWN)
        return
    
    alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
    alerts.append({"from": from_currency, "to": to_currency, "target": target_rate, "notified": False})
    redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(alerts))
    keyboard = [
        [InlineKeyboardButton("🔔 Добавить ещё", callback_data="alert"),
         InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(
        f"🔔 *Уведомление установлено:* {from_currency.upper()} → {to_currency.upper()} при курсе *{target_rate}*",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    stats = redis_client.hgetall('stats') or {'users': '{}', 'total_requests': '0', 'revenue': '0.0'}
    users = len(json.loads(stats['users']))
    requests = int(stats['total_requests'])
    revenue = float(stats.get('revenue', 0.0))
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if user_id in ADMIN_IDS:
        await update.effective_message.reply_text(
            f"📊 *Админ-статистика:*\n👥 Пользователей: *{users}*\n📈 Запросов: *{requests}*\n💰 Доход: *{revenue} USDT*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        user_requests = json.loads(stats['users']).get(user_id, {}).get('requests', 0)
        await update.effective_message.reply_text(
            f"📊 *Твоя статистика:*\n📈 Запросов сегодня: *{user_requests}*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    stats = redis_client.hgetall('stats') or {'subscriptions': '{}'}
    subscriptions = json.loads(stats.get('subscriptions', '{}'))
    if subscriptions.get(user_id, False):
        await update.effective_message.reply_text("💎 Ты уже подписан!", parse_mode=ParseMode.MARKDOWN)
        return
    
    async with ClientSession() as session:
        headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
        payload = {"asset": "USDT", "amount": str(SUBSCRIPTION_PRICE), "description": f"Подписка для {user_id}"}
        async with session.post("https://pay.crypt.bot/api/createInvoice", json=payload, headers=headers) as resp:
            response = await resp.json()
            if response.get("ok"):
                invoice_id = response["result"]["invoice_id"]
                pay_url = response["result"]["pay_url"]
                keyboard = [
                    [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)],
                    [InlineKeyboardButton("🔙 Назад", callback_data="start")]
                ]
                context.user_data[user_id] = {"invoice_id": invoice_id}
                await update.effective_message.reply_text(
                    f"💎 Оплати *{SUBSCRIPTION_PRICE} USDT* для безлимита:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.effective_message.reply_text(
                    f"❌ Ошибка платежа: {response.get('error', 'Неизвестная ошибка')}",
                    parse_mode=ParseMode.MARKDOWN
                )

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.effective_message.reply_text(
        f"👥 *Реферальная ссылка:* `{ref_link}`\n👤 Приглашено: *{refs}*\n🌟 Приглашай друзей!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    history = json.loads(redis_client.get(f"history:{user_id}") or '[]')
    if not history:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(
            "📜 *История запросов пуста.*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    response = "📜 *История твоих запросов:*\n"
    for entry in reversed(history):
        response += f"⏰ {entry['time']}: *{entry['amount']} {entry['from']}* → *{entry['result']} {entry['to']}*\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(response, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    args = context.args
    if len(args) == 1 and args[0].startswith("ref_"):
        referrer_id = args[0].replace("ref_", "")
        if referrer_id.isdigit() and user_id != referrer_id:
            referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
            if user_id not in referrals:
                referrals.append(user_id)
                redis_client.setex(f"referrals:{referrer_id}", 30 * 24 * 60 * 60, json.dumps(referrals))
                await update.effective_message.reply_text(
                    "👥 Приглашение через реферальную ссылку принято!",
                    parse_mode=ParseMode.MARKDOWN
                )

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    async with ClientSession() as session:
        headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
        for user_id, data in list(context.user_data.items()):
            if "invoice_id" not in data:
                continue
            invoice_id = data["invoice_id"]
            async with session.get(f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}", headers=headers) as resp:
                response = await resp.json()
                if response.get("ok") and response["result"]["items"]:
                    status = response["result"]["items"][0]["status"]
                    if status == "paid":
                        stats = redis_client.hgetall('stats') or {'subscriptions': '{}', 'revenue': '0.0'}
                        subscriptions = json.loads(stats.get('subscriptions', '{}'))
                        subscriptions[user_id] = True
                        stats['subscriptions'] = json.dumps(subscriptions)
                        stats['revenue'] = str(float(stats.get('revenue', '0.0')) + SUBSCRIPTION_PRICE)
                        redis_client.hmset('stats', stats)
                        del context.user_data[user_id]
                        await context.bot.send_message(
                            user_id,
                            "💎 Оплата прошла! У тебя безлимит.",
                            parse_mode=ParseMode.MARKDOWN
                        )

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    stats = redis_client.hgetall('stats') or {'users': '{}'}
    users = json.loads(stats.get('users', '{}'))
    async with ClientSession() as session:
        rates = await fetch_all_rates(session)
        for user_id in users:
            alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
            if not alerts:
                continue
            updated_alerts = []
            for alert in alerts:
                from_currency, to_currency, target_rate = alert["from"], alert["to"], float(alert["target"])
                notified = alert.get("notified", False)
                from_code = CURRENCIES[from_currency]['code']
                to_code = CURRENCIES[to_currency]['code']
                pair = f"{from_code}{to_code}"
                rate = rates.get(pair) or (1 / rates.get(f"{to_code}{from_code}", 0) if rates.get(f"{to_code}{from_code}") else None)
                if rate and rate <= target_rate and not notified:
                    await context.bot.send_message(
                        user_id,
                        f"🔔 *Уведомление!* Курс *{from_code} → {to_code}* достиг *{rate:.8f}* (цель: {target_rate})",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    alert["notified"] = True
                updated_alerts.append(alert)
            redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(updated_alerts))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.effective_user.id)
    stats = redis_client.hgetall('stats') or {'subscriptions': '{}'}
    is_subscribed = user_id in ADMIN_IDS or json.loads(stats.get('subscriptions', '{}')).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await update.effective_message.reply_text(
            f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await update.effective_message.reply_text(
            f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    context.user_data['last_request'] = time.time()
    text = update.effective_message.text.lower()
    
    parts = text.split()
    if len(parts) < 2:
        keyboard = [
            [InlineKeyboardButton("💱 Попробовать снова", callback_data="converter"),
             InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        await update.effective_message.reply_text(
            '📝 *Примеры:* `"usd btc"` или `"100 uah usdt"`\nИли используй меню через /start',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    amount = float(parts[0]) if parts[0].replace('.', '', 1).isdigit() else 1.0
    from_currency = parts[1] if amount != 1.0 else parts[0]
    to_currency = parts[2] if amount != 1.0 else parts[1]
    
    save_stats(user_id, f"{from_currency}_to_{to_currency}")
    result, rate_info = await get_exchange_rate(from_currency, to_currency, amount)
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
        await update.effective_message.reply_text(
            f"💰 *{amount:.1f} {from_code}* = *{result:.{precision}f} {to_code}*\n"
            f"📈 {rate_info}\n"
            f"🔄 Осталось запросов: *{remaining_display}*{AD_MESSAGE}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        save_history(user_id, from_code, to_code, amount, result)
    else:
        await update.effective_message.reply_text(f"❌ Ошибка: {rate_info}", parse_mode=ParseMode.MARKDOWN)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer(text="🌟 Обработка...", show_alert=False)
    user_id = str(query.from_user.id)
    
    if not await enforce_subscription(update, context):
        await query.edit_message_text(f"🚫 Подпишись на {CHANNEL_USERNAME}!", parse_mode=ParseMode.MARKDOWN)
        return
    
    stats = redis_client.hgetall('stats') or {'subscriptions': '{}'}
    is_subscribed = user_id in ADMIN_IDS or json.loads(stats.get('subscriptions', '{}')).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await query.edit_message_text(f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}!", parse_mode=ParseMode.MARKDOWN)
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await query.edit_message_text(f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. /subscribe", parse_mode=ParseMode.MARKDOWN)
        return
    
    context.user_data['last_request'] = time.time()
    action = query.data

    if action == "start":
        await start(update, context)
    elif action == "converter":
        keyboard = [
            [InlineKeyboardButton("💰 USD → BTC", callback_data="convert:usd:btc"),
             InlineKeyboardButton("💶 EUR → UAH", callback_data="convert:eur:uah")],
            [InlineKeyboardButton("₿ BTC → ETH", callback_data="convert:btc:eth"),
             InlineKeyboardButton("₴ UAH → USDT", callback_data="convert:uah:usdt")],
            [InlineKeyboardButton("🔄 Ввести вручную", callback_data="manual_convert"),
             InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        await query.edit_message_text(
            "💱 *Выбери пару или введи вручную (например, \"100 uah usdt\"):*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    elif action == "price":
        await query.edit_message_text("📈 *Введи валюту, например: \"btc usd\"*", parse_mode=ParseMode.MARKDOWN)
    elif action == "stats":
        await stats(update, context)
    elif action == "subscribe":
        await subscribe(update, context)
    elif action == "alert":
        await alert(update, context)
    elif action == "referrals":
        await referrals(update, context)
    elif action == "history":
        await history(update, context)
    elif action == "manual_convert":
        await query.edit_message_text("💱 *Введи запрос: \"100 uah usdt\"*", parse_mode=ParseMode.MARKDOWN)
    elif action == "copy_ref":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        await query.answer(text=f"🌟 Скопировано: {ref_link}", show_alert=False)
        await referrals(update, context)
    elif action.startswith("convert:"):
        _, from_currency, to_currency = action.split(":")
        result, rate_info = await get_exchange_rate(from_currency, to_currency)
        if result is not None:
            from_code = CURRENCIES[from_currency]['code']
            to_code = CURRENCIES[to_currency]['code']
            precision = 8 if to_code in ['BTC', 'ETH', 'XRP', 'DOGE', 'ADA', 'SOL', 'LTC', 'BNB', 'TRX', 'DOT', 'MATIC'] else 6
            keyboard = [
                [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                [InlineKeyboardButton("💱 Другая пара", callback_data="converter"),
                 InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]
            await query.edit_message_text(
                f"💰 *1.0 {from_code}* = *{result:.{precision}f} {to_code}*\n"
                f"📈 {rate_info}\n"
                f"🔄 Осталось запросов: *{remaining}*{AD_MESSAGE}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            save_history(user_id, from_code, to_code, 1.0, result)
        else:
            await query.edit_message_text(f"❌ Ошибка: {rate_info}", parse_mode=ParseMode.MARKDOWN)

async def main():
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
    
    application.job_queue.run_repeating(check_payment_job, interval=60, name="check_payment")
    application.job_queue.run_repeating(check_alerts_job, interval=60, name="check_alerts")
    
    if not redis_client.exists('stats'):
        redis_client.hmset('stats', {
            'users': json.dumps({}),
            'total_requests': '0',
            'request_types': json.dumps({}),
            'subscriptions': json.dumps({}),
            'revenue': '0.0'
        })
        redis_client.expire('stats', 30 * 24 * 60 * 60)
    
    await set_bot_commands(application)
    logger.info("Bot starting...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, timeout=30)

if __name__ == "__main__":
    asyncio.run(main())