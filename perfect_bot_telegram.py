import os
import json
import time
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import redis
from telegram.error import NetworkError, RetryAfter

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none")

if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN not set")
    exit(1)
if not CRYPTO_PAY_TOKEN:
    logger.error("CRYPTO_PAY_TOKEN not set")
    exit(1)

AD_MESSAGE = "\n\n📢 Реклама: Подпишись на @tpgbit для новостей о крипте!"
FREE_REQUEST_LIMIT = 5
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 120
ADMIN_ID = "1058875848"  # Твой ID

# Словари валют
CURRENCIES = {
    'доллар': {'id': 'usd', 'code': 'USD'}, 'доллары': {'id': 'usd', 'code': 'USD'}, 'доллара': {'id': 'usd', 'code': 'USD'}, 'usd': {'id': 'usd', 'code': 'USD'},
    'гривна': {'id': 'uah', 'code': 'UAH'}, 'гривны': {'id': 'uah', 'code': 'UAH'}, 'гривен': {'id': 'uah', 'code': 'UAH'}, 'uah': {'id': 'uah', 'code': 'UAH'},
    'евро': {'id': 'eur', 'code': 'EUR'}, 'eur': {'id': 'eur', 'code': 'EUR'},
    'рубль': {'id': 'rub', 'code': 'RUB'}, 'рубли': {'id': 'rub', 'code': 'RUB'}, 'рубля': {'id': 'rub', 'code': 'RUB'}, 'rub': {'id': 'rub', 'code': 'RUB'},
    'йена': {'id': 'jpy', 'code': 'JPY'}, 'йены': {'id': 'jpy', 'code': 'JPY'}, 'jpy': {'id': 'jpy', 'code': 'JPY'},
    'юань': {'id': 'cny', 'code': 'CNY'}, 'юани': {'id': 'cny', 'code': 'CNY'}, 'cny': {'id': 'cny', 'code': 'CNY'},
    'фунт': {'id': 'gbp', 'code': 'GBP'}, 'фунты': {'id': 'gbp', 'code': 'GBP'}, 'gbp': {'id': 'gbp', 'code': 'GBP'},
    'биткоин': {'id': 'bitcoin', 'code': 'BTC'}, 'биткоины': {'id': 'bitcoin', 'code': 'BTC'}, 'биткоина': {'id': 'bitcoin', 'code': 'BTC'}, 'btc': {'id': 'bitcoin', 'code': 'BTC'},
    'эфир': {'id': 'ethereum', 'code': 'ETH'}, 'эфириум': {'id': 'ethereum', 'code': 'ETH'}, 'эфира': {'id': 'ethereum', 'code': 'ETH'}, 'eth': {'id': 'ethereum', 'code': 'ETH'},
    'рипл': {'id': 'ripple', 'code': 'XRP'}, 'риплы': {'id': 'ripple', 'code': 'XRP'}, 'xrp': {'id': 'ripple', 'code': 'XRP'},
    'догекоин': {'id': 'dogecoin', 'code': 'DOGE'}, 'доге': {'id': 'dogecoin', 'code': 'DOGE'}, 'догекоина': {'id': 'dogecoin', 'code': 'DOGE'}, 'doge': {'id': 'dogecoin', 'code': 'DOGE'},
    'кардано': {'id': 'cardano', 'code': 'ADA'}, 'карданы': {'id': 'cardano', 'code': 'ADA'}, 'ada': {'id': 'cardano', 'code': 'ADA'},
    'солана': {'id': 'solana', 'code': 'SOL'}, 'соланы': {'id': 'solana', 'code': 'SOL'}, 'sol': {'id': 'solana', 'code': 'SOL'},
    'лайткоин': {'id': 'litecoin', 'code': 'LTC'}, 'лайткоины': {'id': 'litecoin', 'code': 'LTC'}, 'ltc': {'id': 'litecoin', 'code': 'LTC'}
}

def save_stats(user_id, request_type):
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        current_day = time.strftime("%Y-%m-%d")
        users = stats.get("users", {})
        if user_id not in users:
            users[user_id] = {"requests": 0, "last_reset": current_day}
        
        user_data = users[user_id]
        if user_data["last_reset"] != current_day:
            user_data["requests"] = 0
            user_data["last_reset"] = current_day
        
        user_data["requests"] += 1
        stats["users"] = users
        stats["total_requests"] = stats.get("total_requests", 0) + 1
        stats["request_types"][request_type] = stats["request_types"].get(request_type, 0) + 1
        redis_client.set('stats', json.dumps(stats))
        logger.debug(f"Stats updated: {user_id} - {request_type}")
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def check_limit(user_id):
    try:
        if user_id == ADMIN_ID:
            logger.debug(f"Admin {user_id} - unlimited access")
            return True, "∞"
        
        stats = json.loads(redis_client.get('stats') or '{}')
        subscribed = stats.get("subscriptions", {}).get(user_id, False)
        if subscribed:
            logger.debug(f"Subscribed user {user_id} - unlimited access")
            return True, "∞"
        
        users = stats.get("users", {})
        user_data = users.get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
        remaining = FREE_REQUEST_LIMIT - user_data["requests"]
        logger.debug(f"User {user_id} has {remaining} requests left")
        return remaining > 0, remaining
    except Exception as e:
        logger.error(f"Error checking limit: {e}")
        return False, 0

def get_exchange_rate(from_currency, to_currency, amount=1):
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"rate:{from_key}_{to_key}"
    
    cached = redis_client.get(cache_key)
    if cached:
        rate = float(cached)
        logger.info(f"Cache hit: {from_key} to {to_key} = {rate}")
        return amount * rate, rate
    
    from_data = CURRENCIES.get(from_key)
    to_data = CURRENCIES.get(to_key)
    if not from_data or not to_data:
        logger.error(f"Invalid currency: {from_key} or {to_key}")
        return None, "Неподдерживаемая валюта"
    
    from_id = from_data['id']
    to_id = to_data['id']
    
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={from_id}&vs_currencies={to_id}"
        logger.debug(f"Fetching: {url}")
        response = requests.get(url, timeout=15).json()
        logger.info(f"Response: {json.dumps(response)}")
        
        if from_id in response and to_id in response[from_id]:
            rate = response[from_id][to_id]
            if rate <= 0:
                logger.error(f"Invalid rate: {rate}")
                return None, "Курс недоступен (нулевое значение)"
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        
        # Пробуем обратный курс
        url_reverse = f"https://api.coingecko.com/api/v3/simple/price?ids={to_id}&vs_currencies={from_id}"
        logger.debug(f"Fetching reverse: {url_reverse}")
        response_reverse = requests.get(url_reverse, timeout=15).json()
        logger.info(f"Reverse response: {json.dumps(response_reverse)}")
        
        if to_id in response_reverse and from_id in response_reverse[to_id]:
            rate = 1 / response_reverse[to_id][from_id]
            if rate <= 0:
                logger.error(f"Invalid reverse rate: {rate}")
                return None, "Курс недоступен (нулевое значение)"
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        
        logger.error(f"No rate found for {from_id} to {to_id}")
        return None, "Курс недоступен: данные отсутствуют"
    except requests.RequestException as e:
        logger.error(f"API error: {e}")
        return None, f"Ошибка API: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    save_stats(user_id, "start")
    logger.info(f"User {user_id} started bot")
    await update.message.reply_text(
        'Привет! Я бот для конвертации валют.\n'
        'Примеры: "/kurs usd btc" или "/kurs 44 доллара к эфиру".\n'
        f'Бесплатно: {FREE_REQUEST_LIMIT} запросов в сутки.\n'
        f'Безлимит: /subscribe за {SUBSCRIPTION_PRICE} USDT.'
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    if stats.get("subscriptions", {}).get(user_id, False):
        logger.info(f"User {user_id} already subscribed")
        await update.message.reply_text("Ты уже подписан!")
        return
    
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = {
        "amount": str(SUBSCRIPTION_PRICE),
        "currency": "USDT",
        "description": f"Подписка для {user_id}"
    }
    logger.debug(f"Creating invoice: {json.dumps(payload)}")
    try:
        response = requests.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload, timeout=15).json()
        logger.info(f"Invoice response: {json.dumps(response)}")
        if response.get("ok"):
            invoice_id = response["result"]["invoice_id"]
            pay_url = response["result"]["pay_url"]
            keyboard = [[InlineKeyboardButton(f"Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)]]
            context.user_data[user_id] = {"invoice_id": invoice_id}
            await update.message.reply_text(f"Оплати {SUBSCRIPTION_PRICE} USDT:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            logger.error(f"Invoice failed: {response}")
            await update.message.reply_text(f"Ошибка платежа: {response.get('error', 'Неизвестная ошибка')}")
    except requests.RequestException as e:
        logger.error(f"Subscribe error: {e}")
        await update.message.reply_text("Ошибка связи с платежной системой")

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
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

async def kurs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    args = context.args
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await update.message.reply_text(f"Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe")
        return
    
    if not args:
        await update.message.reply_text('Примеры: "/kurs usd btc" или "/kurs 44 доллара к эфиру"')
        return
    
    text = " ".join(args).lower()
    parts = text.split()
    logger.info(f"Kurs request from {user_id}: {text}")
    
    try:
        if 'к' in parts:
            k_index = parts.index('к')
            amount = float(parts[0]) if parts[0].replace('.', '', 1).isdigit() else 1
            from_currency = parts[k_index-1]
            to_currency = parts[-1]
        else:
            if len(parts) >= 2 and parts[0].replace('.', '', 1).isdigit():
                amount = float(parts[0])
                from_currency, to_currency = parts[1], parts[2]
            else:
                amount = 1
                from_currency, to_currency = parts[0], parts[1]
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        
        if result:
            from_code = CURRENCIES[from_currency.lower()]['code']
            to_code = CURRENCIES[to_currency.lower()]['code']
            remaining_display = "∞" if user_id == ADMIN_ID or json.loads(redis_client.get('stats') or '{}').get("subscriptions", {}).get(user_id, False) else remaining
            await update.message.reply_text(
                f"{amount} {from_code} = {result:.6f} {to_code}\n"
                f"Курс: 1 {from_code} = {rate:.6f} {to_code}\n"
                f"Осталось запросов: {remaining_display}{AD_MESSAGE}"
            )
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Kurs error for {user_id}: {e}")
        await update.message.reply_text("Ошибка ввода. Примеры: '/kurs usd btc'")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < 1:
        await update.message.reply_text("Подожди секунду!")
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await update.message.reply_text(f"Лимит {FREE_REQUEST_LIMIT} запросов исчерпан. Подпишись: /subscribe")
        return
    
    context.user_data['last_request'] = time.time()
    text = update.message.text.lower()
    logger.info(f"Message from {user_id}: {text}")
    
    try:
        parts = text.split()
        amount = float(parts[0])
        from_currency = parts[1]
        to_currency = parts[-1] if 'в' in parts or 'to' in parts else None
        if not to_currency:
            raise ValueError
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        if result:
            from_code = CURRENCIES[from_currency.lower()]['code']
            to_code = CURRENCIES[to_currency.lower()]['code']
            remaining_display = "∞" if user_id == ADMIN_ID or json.loads(redis_client.get('stats') or '{}').get("subscriptions", {}).get(user_id, False) else remaining
            await update.message.reply_text(
                f"{amount} {from_code} = {result:.6f} {to_code}\n"
                f"Курс: 1 {from_code} = {rate:.6f} {to_code}\n"
                f"Осталось запросов: {remaining_display}{AD_MESSAGE}"
            )
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Message error for {user_id}: {e}")
        await update.message.reply_text("Примеры: '10 usd to btc'")

# Запуск
application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("kurs", kurs_command))
application.add_handler(CommandHandler("subscribe", subscribe))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.job_queue.run_repeating(check_payment_job, interval=60)

if __name__ == "__main__":
    if not redis_client.exists('stats'):
        redis_client.set('stats', json.dumps({"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}))
    logger.info("Bot starting...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except NetworkError as e:
        logger.error(f"Network error on start: {e}")
        time.sleep(5)
        application.run_polling()
