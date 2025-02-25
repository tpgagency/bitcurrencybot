import os
import json
import time
import logging
import requests
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import redis
from telegram.error import NetworkError, RetryAfter, TelegramError

# Настройка логирования (максимальная детализация для отладки)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CHANNEL_USERNAME = "@tpgbit"
BOT_USERNAME = "BitCurrencyBot"  # Замени на имя твоего бота
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none")

if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN not set")
    exit(1)
if not CRYPTO_PAY_TOKEN:
    logger.error("CRYPTO_PAY_TOKEN not set")
    exit(1)

AD_MESSAGE = "\n\n📢 Подпишись на @tpgbit для новостей о крипте!"
FREE_REQUEST_LIMIT = 5
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 5  # Кэш на 5 секунд для реального времени
ADMIN_IDS = ["1058875848", "6403305626"]

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

# Fallback курсы (только при сбоях API)
UAH_TO_USDT_FALLBACK = 0.0239  # 1 UAH ≈ 0.0239 USDT
USDT_TO_UAH_FALLBACK = 41.84   # 1 USDT ≈ 41.84 UAH

async def set_bot_commands(application: Application):
    """Устанавливает команды бота в Telegram."""
    commands = [
        ("start", "Меню бота"),
        ("currencies", "Список валют"),
        ("stats", "Статистика"),
        ("subscribe", "Подписка"),
        ("alert", "Уведомления"),
        ("referrals", "Рефералы")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully")

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет подписку пользователя на канал."""
    user_id = update.effective_user.id
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        if chat_member.status in ['member', 'administrator', 'creator']:
            logger.debug(f"User {user_id} is subscribed to {CHANNEL_USERNAME}")
            return True
        logger.debug(f"User {user_id} is not subscribed to {CHANNEL_USERNAME}, status: {chat_member.status}")
        return False
    except TelegramError as e:
        logger.error(f"Error checking subscription for {user_id}: {e}")
        await update.effective_message.reply_text(
            "Не могу проверить подписку. Убедись, что бот — админ в @tpgbit, и попробуй снова."
        )
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Требует подписку на канал перед обработкой запроса."""
    if await check_subscription(update, context):
        return True
    await update.effective_message.reply_text(
        "Чтобы пользоваться ботом, подпишись на @tpgbit!\nПосле подписки повтори запрос."
    )
    return False

def save_stats(user_id: str, request_type: str):
    """Сохраняет статистику запросов в Redis."""
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
        redis_client.set('stats', json.dumps(stats))
        logger.debug(f"Stats updated: {user_id} - {request_type}")
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def check_limit(user_id: str) -> tuple[bool, str]:
    """Проверяет лимит запросов для пользователя."""
    try:
        if user_id in ADMIN_IDS:
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
    """Получает курс обмена в реальном времени с Binance или WhiteBIT."""
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"rate:{from_key}_{to_key}"
    
    # Проверка кэша
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

    # Binance API
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
        
        reverse_pair = f"{to_code}{from_code}"
        response = requests.get(f"{BINANCE_API_URL}?symbol={reverse_pair}", timeout=5).json()
        if 'price' in response:
            reverse_rate = float(response['price'])
            if reverse_rate <= 0:
                raise ValueError(f"Invalid Binance reverse rate for {reverse_pair}: {reverse_rate}")
            rate = 1 / reverse_rate
            logger.info(f"Binance reverse rate (real-time): {reverse_pair} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        
        # Расчёт через USDT
        rate_from_usdt = None
        rate_usdt_to = None
        
        # from -> USDT
        if from_key != 'usdt':
            from_usdt_pair = f"{from_code}USDT"
            response_from = requests.get(f"{BINANCE_API_URL}?symbol={from_usdt_pair}", timeout=5).json()
            if 'price' in response_from:
                rate_from_usdt = float(response_from['price'])  # USDT за 1 from_code
                logger.debug(f"Binance {from_usdt_pair} = {rate_from_usdt}")
            else:
                usdt_from_pair = f"USDT{from_code}"
                response_from_reverse = requests.get(f"{BINANCE_API_URL}?symbol={usdt_from_pair}", timeout=5).json()
                if 'price' in response_from_reverse:
                    rate_from_usdt = 1 / float(response_from_reverse['price'])  # Инверсия для USDT/from
                    logger.debug(f"Binance {usdt_from_pair} = {rate_from_usdt} (inverse)")
        else:
            rate_from_usdt = 1.0
        
        # USDT -> to
        if to_key != 'usdt':
            usdt_to_pair = f"USDT{to_code}"
            response_to = requests.get(f"{BINANCE_API_URL}?symbol={usdt_to_pair}", timeout=5).json()
            if 'price' in response_to:
                rate_usdt_to = float(response_to['price'])  # to_code за 1 USDT (прямой курс)
                logger.debug(f"Binance {usdt_to_pair} = {rate_usdt_to}")
            else:
                to_usdt_pair = f"{to_code}USDT"
                response_to_reverse = requests.get(f"{BINANCE_API_URL}?symbol={to_usdt_pair}", timeout=5).json()
                if 'price' in response_to_reverse:
                    rate_usdt_to = 1 / float(response_to_reverse['price'])  # Инверсия для to/USDT
                    logger.debug(f"Binance {to_usdt_pair} = {rate_usdt_to} (inverse)")
        else:
            rate_usdt_to = 1.0
        
        if rate_from_usdt and rate_usdt_to:
            rate = rate_from_usdt * rate_usdt_to  # Исправлено: умножение для from -> USDT -> to
            if rate <= 0:
                raise ValueError(f"Invalid Binance calculated rate: {rate}")
            logger.info(f"Binance rate via USDT (real-time): {from_key} to {to_key} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate

    except Exception as e:
        logger.warning(f"Binance API failed for {from_key} to {to_key}: {e}")

    # WhiteBIT API
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
        
        reverse_pair_key = f"{to_code}_{from_code}"
        if reverse_pair_key in response:
            reverse_rate = float(response[reverse_pair_key]['last_price'])
            if reverse_rate <= 0:
                raise ValueError(f"Invalid WhiteBIT reverse rate for {reverse_pair_key}: {reverse_rate}")
            rate = 1 / reverse_rate
            logger.info(f"WhiteBIT reverse rate (real-time): {reverse_pair_key} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        
        # Расчёт через USDT
        rate_from_usdt = None
        rate_usdt_to = None
        
        # from -> USDT
        if from_key != 'usdt':
            from_usdt_pair = f"{from_code}_USDT"
            if from_usdt_pair in response:
                rate_from_usdt = float(response[from_usdt_pair]['last_price'])  # USDT за 1 from_code
                logger.debug(f"WhiteBIT {from_usdt_pair} = {rate_from_usdt}")
            else:
                usdt_from_pair = f"USDT_{from_code}"
                if usdt_from_pair in response:
                    rate_from_usdt = 1 / float(response[usdt_from_pair]['last_price'])  # Инверсия для USDT/from
                    logger.debug(f"WhiteBIT {usdt_from_pair} = {rate_from_usdt} (inverse)")
        else:
            rate_from_usdt = 1.0
        
        # USDT -> to
        if to_key != 'usdt':
            usdt_to_pair = f"USDT_{to_code}"
            if usdt_to_pair in response:
                rate_usdt_to = float(response[usdt_to_pair]['last_price'])  # to_code за 1 USDT
                logger.debug(f"WhiteBIT {usdt_to_pair} = {rate_usdt_to}")
            else:
                to_usdt_pair = f"{to_code}_USDT"
                if to_usdt_pair in response:
                    rate_usdt_to = 1 / float(response[to_usdt_pair]['last_price'])  # Инверсия для to/USDT
                    logger.debug(f"WhiteBIT {to_usdt_pair} = {rate_usdt_to} (inverse)")
        else:
            rate_usdt_to = 1.0
        
        if rate_from_usdt and rate_usdt_to:
            rate = rate_from_usdt * rate_usdt_to  # Исправлено: умножение для from -> USDT -> to
            if rate <= 0:
                raise ValueError(f"Invalid WhiteBIT calculated rate: {rate}")
            logger.info(f"WhiteBIT rate via USDT (real-time): {from_key} to {to_key} = {rate}")
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate

    except Exception as e:
        logger.warning(f"WhiteBIT API failed for {from_key} to {to_key}: {e}")

    # Fallback
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
    """Обработчик команды /start."""
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    save_stats(user_id, "start")
    logger.info(f"User {user_id} started bot")
    await update.effective_message.reply_text(
        'Привет! Я бот для конвертации валют в реальном времени.\n'
        'Напиши коды валют, например: "usd btc" или "100 uah usdt".\n'
        f'Бесплатно: {FREE_REQUEST_LIMIT} запросов в сутки.\n'
        f'Безлимит: /subscribe за {SUBSCRIPTION_PRICE} USDT.\n'
        'Список валют: /currencies.\n'
        'Команды в меню Telegram (внизу слева).'
    )

async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /currencies."""
    if not await enforce_subscription(update, context):
        return
    currency_list = ", ".join(sorted(CURRENCIES.keys()))
    await update.effective_message.reply_text(f"Поддерживаемые валюты: {currency_list}")

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /alert."""
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.effective_user.id)
    args = context.args
    if len(args) != 3 or not args[2].replace('.', '', 1).isdigit():
        await update.effective_message.reply_text('Пример: /alert usd btc 0.000015')
        return
    
    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        await update.effective_message.reply_text("Ошибка: одна из валют не поддерживается")
        return
    
    alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
    alerts.append({"from": from_currency, "to": to_currency, "target": target_rate})
    redis_client.set(f"alerts:{user_id}", json.dumps(alerts))
    await update.effective_message.reply_text(f"Уведомление установлено: {from_currency} → {to_currency} при курсе {target_rate}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats."""
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    users = len(stats.get("users", {}))
    requests = stats.get("total_requests", 0)
    revenue = stats.get("revenue", 0.0)
    if user_id in ADMIN_IDS:
        await update.effective_message.reply_text(f"Админ-статистика:\nПользователей: {users}\nЗапросов: {requests}\nДоход: {revenue} USDT")
    else:
        await update.effective_message.reply_text(f"Твоя статистика:\nЗапросов сегодня: {stats.get('users', {}).get(user_id, {}).get('requests', 0)}")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /subscribe."""
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    if stats.get("subscriptions", {}).get(user_id, False):
        logger.info(f"User {user_id} already subscribed")
        await update.effective_message.reply_text("Ты уже подписан!")
        return
    
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(SUBSCRIPTION_PRICE),
        "description": f"Подписка для {user_id}"
    }
    try:
        response = requests.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload, timeout=15).json()
        logger.info(f"Invoice response: {json.dumps(response)}")
        if response.get("ok"):
            invoice_id = response["result"]["invoice_id"]
            pay_url = response["result"]["pay_url"]
            keyboard = [[InlineKeyboardButton(f"Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)]]
            context.user_data[user_id] = {"invoice_id": invoice_id}
            await update.effective_message.reply_text(f"Оплати {SUBSCRIPTION_PRICE} USDT:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            logger.error(f"Invoice failed: {response}")
            await update.effective_message.reply_text(f"Ошибка платежа: {response.get('error', 'Неизвестная ошибка')}")
    except requests.RequestException as e:
        logger.error(f"Subscribe error: {e}")
        await update.effective_message.reply_text("Ошибка связи с платежной системой")

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /referrals."""
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
    await update.effective_message.reply_text(
        f"Твоя реферальная ссылка: {ref_link}\n"
        f"Приглашено пользователей: {refs}\n"
        "Приглашай друзей и получай бонусы (скоро будет доступно)!"
    )

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает реферальные ссылки."""
    user_id = str(update.effective_user.id)
    args = context.args
    if len(args) == 1 and args[0].startswith("ref_"):
        referrer_id = args[0].replace("ref_", "")
        if referrer_id.isdigit():
            referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
            if user_id not in referrals:
                referrals.append(user_id)
                redis_client.set(f"referrals:{referrer_id}", json.dumps(referrals))
                logger.info(f"New referral: {user_id} for {referrer_id}")
                await update.effective_message.reply_text("Ты был приглашён через реферальную ссылку! Спасибо!")

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка платежей."""
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
                    redis_client.set('stats', json.dumps(stats))
                    del context.user_data[user_id]
                    logger.info(f"Payment confirmed for {user_id}")
                    await context.bot.send_message(user_id, "Оплата прошла! У тебя безлимит.")
        except requests.RequestException as e:
            logger.error(f"Payment check error for {user_id}: {e}")

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка уведомлений."""
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
                await context.bot.send_message(
                    user_id,
                    f"Уведомление! Курс {from_code} → {to_code} достиг {current_rate:.6f} (цель: {target_rate})"
                )
            else:
                updated_alerts.append(alert)
        redis_client.set(f"alerts:{user_id}", json.dumps(updated_alerts))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений."""
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await update.effective_message.reply_text(f"Подожди {delay} секунд{'у' if delay == 1 else ''}!")
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await update.effective_message.reply_text(f"Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe")
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
            await update.effective_message.reply_text(
                f"{amount:.1f} {from_code} = {result:.6f} {to_code}\n"
                f"Курс: 1 {from_code} = {rate:.6f} {to_code}\n"
                f"Осталось запросов: {remaining_display}{AD_MESSAGE}"
            )
        else:
            await update.effective_message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Message error for {user_id}: {e}")
        await update.effective_message.reply_text('Примеры: "usd btc" или "100 uah usdt"\nИли используй меню через /start')

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    
    if not await enforce_subscription(update, context):
        return
    
    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await query.edit_message_text(f"Подожди {delay} секунд{'у' if delay == 1 else ''}!")
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await query.edit_message_text(f"Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe")
        return
    
    context.user_data['last_request'] = time.time()
    action = query.data

    if action == "converter":
        await query.edit_message_text("Введи сумму и валюты для конвертации, например: \"100 uah usdt\"")
    elif action == "price":
        await query.edit_message_text("Введи валюту для проверки текущей цены, например: \"btc usd\"")
    elif action == "stats":
        users = len(stats.get("users", {}))
        requests = stats.get("total_requests", 0)
        revenue = stats.get("revenue", 0.0)
        if user_id in ADMIN_IDS:
            await query.edit_message_text(f"Админ-статистика:\nПользователей: {users}\nЗапросов: {requests}\nДоход: {revenue} USDT")
        else:
            await query.edit_message_text(f"Твоя статистика:\nЗапросов сегодня: {stats.get('users', {}).get(user_id, {}).get('requests', 0)}")
    elif action == "referrals":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
        await query.edit_message_text(
            f"Твоя реферальная ссылка: {ref_link}\n"
            f"Приглашено пользователей: {refs}\n"
            "Приглашай друзей и получай бонусы (скоро будет доступно)!"
        )

if __name__ == "__main__":
    """Запуск бота."""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("currencies", currencies))
    application.add_handler(CommandHandler("alert", alert))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("referrals", referrals))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button))
    application.job_queue.run_repeating(check_payment_job, interval=60)
    application.job_queue.run_repeating(check_alerts_job, interval=60)

    # Инициализация статистики
    if not redis_client.exists('stats'):
        redis_client.set('stats', json.dumps({"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}))
    logger.info("Bot starting...")

    # Запуск с обработкой ошибок сети
    while True:
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except NetworkError as e:
            logger.error(f"Network error on start: {e}")
            time.sleep(5)
