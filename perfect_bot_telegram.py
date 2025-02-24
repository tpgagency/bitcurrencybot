import os
import json
import time
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import redis

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none")

if not TELEGRAM_TOKEN or not CRYPTO_PAY_TOKEN:
    logger.error("TELEGRAM_TOKEN or CRYPTO_PAY_TOKEN not set")
    exit(1)

AD_MESSAGE = "\n\n📢 Реклама: Подпишись на @YourChannel для новостей о крипте!"
FREE_REQUEST_LIMIT = 10
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 120  # Уменьшил до 2 минут для актуальности

# Словари валют (без изменений)
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
    """Сохраняет статистику в Redis"""
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
        request_types = stats.get("request_types", {})
        request_types[request_type] = request_types.get(request_type, 0) + 1
        stats["request_types"] = request_types
        redis_client.set('stats', json.dumps(stats))
        logger.info(f"Stats updated for user {user_id}: {request_type}")
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def check_limit(user_id):
    """Проверяет лимит запросов"""
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        subscribed = stats.get("subscriptions", {}).get(user_id, False)
        if subscribed:
            return True
        users = stats.get("users", {})
        user_data = users.get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
        remaining = FREE_REQUEST_LIMIT - user_data["requests"]
        return remaining > 0, remaining
    except Exception as e:
        logger.error(f"Error checking limit: {e}")
        return False, 0

def get_exchange_rate(from_currency, to_currency, amount=1):
    """Получает курс валют через CoinGecko"""
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"rate:{from_key}_{to_key}"
    
    cached = redis_client.get(cache_key)
    if cached:
        rate = float(cached)
        logger.info(f"Using cached rate for {from_key} to {to_key}: {rate}")
        return amount * rate, rate
    
    from_data = CURRENCIES.get(from_key)
    to_data = CURRENCIES.get(to_key)
    if not from_data or not to_data:
        return None, "Неподдерживаемая валюта."
    
    from_id = from_data['id']
    to_code = to_data['id']
    
    try:
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={from_id}&vs_currencies={to_code}"
    response = requests.get(url, timeout=10).json()
    logger.info(f"API response for {from_id} to {to_code}: {json.dumps(response)}")
    
    if not response or from_id not in response:
        return None, f"Курс недоступен: API вернул пустой ответ или нет {from_id}"
    if to_code not in response[from_id]:
        return None, f"Курс недоступен: нет данных для {to_code}"
        
    rate = response[from_id][to_code]
    if rate == 0 or rate is None:
        return None, "Курс недоступен (нулевое значение)."
    result = amount * rate
    redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
    return result, rate
except Exception as e:
    logger.error(f"Error fetching rate: {e}, response: {response.text if 'response' in locals() else 'No response'}")
    return None, f"Ошибка получения курса: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    save_stats(user_id, "start")
    await update.message.reply_text(
        'Привет! Я бот для конвертации валют.\n'
        'Используй /kurs, например: "/kurs usd btc" или "/kurs 44 доллара к эфиру".\n'
        f'Бесплатно — {FREE_REQUEST_LIMIT} запросов в сутки.\n'
        f'Для безлимита — /subscribe за {SUBSCRIPTION_PRICE} USDT.'
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    if stats.get("subscriptions", {}).get(user_id, False):
        await update.message.reply_text("Ты уже подписан на безлимит!")
        return
    
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = {
        "amount": str(SUBSCRIPTION_PRICE),
        "currency": "USDT",
        "description": f"Безлимитная подписка для {user_id}"
    }
    try:
        response = requests.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload).json()
        if response.get("ok"):
            invoice_id = response["result"]["invoice_id"]
            pay_url = response["result"]["pay_url"]
            keyboard = [[InlineKeyboardButton(f"Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data[user_id] = {"invoice_id": invoice_id}
            await update.message.reply_text(f"Оплати {SUBSCRIPTION_PRICE} USDT для безлимита:", reply_markup=reply_markup)
        else:
            await update.message.reply_text(f"Ошибка создания платежа: {response.get('error', 'Попробуй позже.')}")
    except Exception as e:
        logger.error(f"Exception in subscribe: {e}")
        await update.message.reply_text("Ошибка подключения к платежной системе.")

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая проверка оплаты"""
    for user_id, data in list(context.user_data.items()):
        if "invoice_id" not in data:
            continue
        invoice_id = data["invoice_id"]
        headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
        try:
            response = requests.get(f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}", headers=headers).json()
            if response.get("ok") and response["result"]["items"]:
                status = response["result"]["items"][0]["status"]
                if status == "paid":
                    stats = json.loads(redis_client.get('stats') or '{}')
                    stats.setdefault("subscriptions", {})[user_id] = True
                    stats["revenue"] = stats.get("revenue", 0.0) + SUBSCRIPTION_PRICE
                    redis_client.set('stats', json.dumps(stats))
                    del context.user_data[user_id]
                    await context.bot.send_message(user_id, "Оплата подтверждена! Теперь у тебя безлимит.")
        except Exception as e:
            logger.error(f"Error in payment check for {user_id}: {e}")

async def kurs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    args = context.args
    can_proceed, remaining = check_limit(user_id)
    
    if not can_proceed:
        await update.message.reply_text(f"Ты превысил лимит {FREE_REQUEST_LIMIT} запросов в сутки. Подпишись: /subscribe")
        return
    if remaining <= 2:
        await update.message.reply_text(f"Осталось {remaining} запроса сегодня. Подпишись: /subscribe")

    if not args:
        await update.message.reply_text('Напиши, например: "/kurs usd btc" или "/kurs 44 доллара к эфиру".')
        return
    
    text = " ".join(args).lower()
    parts = text.split()
    
    try:
        if 'к' in parts:
            k_index = parts.index('к')
            amount_part = parts[:k_index]
            from_part = parts[k_index-1]
            to_part = " ".join(parts[k_index+1:])
            amount = float(amount_part[0]) if amount_part and amount_part[0].replace('.', '', 1).isdigit() else 1
            from_currency = from_part
            to_currency = to_part
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
            remaining_display = "∞" if json.loads(redis_client.get('stats') or '{}').get("subscriptions", {}).get(user_id, False) else remaining
            response = f"{amount} {from_code} = {result:.6f} {to_code}\nКурс: 1 {from_code} = {rate:.6f} {to_code}\nОсталось запросов сегодня: {remaining_display}{AD_MESSAGE}"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Error in kurs: {e}")
        await update.message.reply_text('Не понял запрос. Примеры: "/kurs usd btc" или "/kurs 44 доллара к эфиру".')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    current_time = time.time()
    
    if 'last_request' in context.user_data and current_time - context.user_data['last_request'] < 1:
        await update.message.reply_text('Слишком много запросов! Подожди секунду.')
        return
    
    can_proceed, remaining = check_limit(user_id)
    if not can_proceed:
        await update.message.reply_text(f"Ты превысил лимит {FREE_REQUEST_LIMIT} запросов в сутки. Подпишись: /subscribe")
        return
    
    context.user_data['last_request'] = current_time
    text = update.message.text.lower()
    
    try:
        parts = text.split()
        amount = float(parts[0])
        from_currency = parts[1]
        if 'в' in parts or 'to' in parts:
            to_currency = parts[-1]
        else:
            raise ValueError
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        if result:
            from_code = CURRENCIES[from_currency.lower()]['code']
            to_code = CURRENCIES[to_currency.lower()]['code']
            remaining_display = "∞" if json.loads(redis_client.get('stats') or '{}').get("subscriptions", {}).get(user_id, False) else remaining
            response = f"{amount} {from_code} = {result:.6f} {to_code}\nКурс: 1 {from_code} = {rate:.6f} {to_code}\nОсталось запросов сегодня: {remaining_display}{AD_MESSAGE}"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Error in handle_message: {e}")

# Запуск бота
application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("kurs", kurs_command))
application.add_handler(CommandHandler("subscribe", subscribe))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.job_queue.run_repeating(check_payment_job, interval=60)  # Проверка оплаты каждые 60 секунд

if __name__ == "__main__":
    if not redis_client.exists('stats'):
        default_stats = {"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}
        redis_client.set('stats', json.dumps(default_stats))
    application.run_polling()
