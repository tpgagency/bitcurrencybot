import requests
import json
import time
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_BOT_TOKEN_HERE')  # Токен от BotFather
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN', 'YOUR_CRYPTO_PAY_TOKEN')  # Токен от @Send
AD_MESSAGE = "\n\n📢 Реклама: Подпишись на мой канал @YourChannel для новостей о крипте и финансах!"
FREE_REQUEST_LIMIT = 10  # Лимит бесплатных запросов в сутки
SUBSCRIPTION_PRICE = 5  # Цена подписки в USDT

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

# Глобальные переменные
CACHE = {}  # Кэш курсов валют
CACHE_TIMEOUT = 300  # 5 минут

def save_stats(user_id, request_type):
    """Сохраняет статистику запросов"""
    global STATS
    current_day = time.strftime("%Y-%m-%d")
    if user_id not in STATS["users"]:
        STATS["users"][user_id] = {"requests": 0, "last_reset": current_day}
    
    user_data = STATS["users"][user_id]
    if user_data["last_reset"] != current_day:
        user_data["requests"] = 0
        user_data["last_reset"] = current_day
    
    user_data["requests"] += 1
    STATS["total_requests"] += 1
    STATS["request_types"][request_type] = STATS["request_types"].get(request_type, 0) + 1
    logger.info(f"Stats updated for user {user_id}: {request_type}")

def check_limit(user_id):
    """Проверяет лимит запросов пользователя"""
    global STATS
    subscribed = STATS["subscriptions"].get(user_id, False)
    if subscribed:
        return True
    user_data = STATS["users"].get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
    return user_data["requests"] < FREE_REQUEST_LIMIT

def get_exchange_rate(from_currency, to_currency, amount=1):
    """Получает курс валют через CoinGecko"""
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"{from_key}_{to_key}"
    
    if cache_key in CACHE and time.time() - CACHE[cache_key]['timestamp'] < CACHE_TIMEOUT:
        rate = CACHE[cache_key]['rate']
        logger.info(f"Using cached rate for {from_key} to {to_key}: {rate}")
        return amount * rate, rate
    
    from_data = CURRENCIES.get(from_key)
    to_data = CURRENCIES.get(to_key)
    
    if not from_data or not to_data:
        logger.error(f"Invalid currency codes: {from_key} -> {to_key}")
        return None, "Неподдерживаемая валюта."
    
    from_id = from_data['id']
    to_code = to_data['id']  # Используем ID для vs_currencies
    
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={from_id}&vs_currencies={to_code}"
        response = requests.get(url, timeout=5).json()
        logger.info(f"API response for {from_id} to {to_code}: {json.dumps(response)}")
        
        if from_id in response and to_code in response[from_id]:
            rate = response[from_id][to_code]
            if rate == 0 or rate is None:
                logger.error(f"Zero or null rate returned for {from_id} to {to_code}")
                return None, "Курс недоступен (нулевое значение)."
            result = amount * rate
            CACHE[cache_key] = {'rate': rate, 'timestamp': time.time()}
            return result, rate
        else:
            logger.error(f"No valid rate in response for {from_id} to {to_code}: {response}")
            return None, "Курс недоступен. Проверь валюты."
    except Exception as e:
        logger.error(f"Error fetching rate for {from_id} to {to_code}: {e}")
        return None, f"Ошибка получения курса: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = str(update.message.from_user.id)
    save_stats(user_id, "start")
    logger.info(f"User {user_id} started bot")
    await update.message.reply_text(
        'Привет! Я бот для конвертации валют.\n'
        'Используй /kurs, например: "/kurs usd btc" или "/kurs 44 доллара к эфиру".\n'
        f'Бесплатно — {FREE_REQUEST_LIMIT} запросов в сутки.\n'
        f'Для безлимита — /subscribe за {SUBSCRIPTION_PRICE} USDT.'
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /subscribe"""
    global STATS
    user_id = str(update.message.from_user.id)
    if STATS["subscriptions"].get(user_id, False):
        logger.info(f"User {user_id} already subscribed")
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
        logger.info(f"Invoice request for {user_id}: {response}")
        if response.get("ok"):
            invoice_id = response["result"]["invoice_id"]
            pay_url = response["result"]["pay_url"]
            keyboard = [[InlineKeyboardButton(f"Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data[user_id] = {"invoice_id": invoice_id}
            await update.message.reply_text(f"Оплати {SUBSCRIPTION_PRICE} USDT для безлимитного доступа:", reply_markup=reply_markup)
        else:
            logger.error(f"Invoice failed for {user_id}: {response.get('error', 'Unknown error')}")
            await update.message.reply_text(f"Ошибка создания платежа: {response.get('error', 'Попробуй позже.')}")
    except Exception as e:
        logger.error(f"Exception in subscribe for {user_id}: {e}")
        await update.message.reply_text("Ошибка подключения к платежной системе.")

async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /check"""
    global STATS
    user_id = str(update.message.from_user.id)
    if user_id not in context.user_data or "invoice_id" not in context.user_data[user_id]:
        await update.message.reply_text("Сначала запроси подписку через /subscribe!")
        return
    
    invoice_id = context.user_data[user_id]["invoice_id"]
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    try:
        response = requests.get(f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}", headers=headers).json()
        logger.info(f"Payment check for {user_id}: {response}")
        if response.get("ok") and response["result"]["items"]:
            status = response["result"]["items"][0]["status"]
            if status == "paid":
                STATS["subscriptions"][user_id] = True
                STATS["revenue"] += SUBSCRIPTION_PRICE
                del context.user_data[user_id]
                logger.info(f"Payment confirmed for {user_id}")
                await update.message.reply_text("Оплата подтверждена! Теперь у тебя безлимит.")
            else:
                await update.message.reply_text(f"Оплата ещё не подтверждена. Статус: {status}. Проверь в @Send.")
        else:
            logger.error(f"Payment check failed for {user_id}: {response.get('error', 'Unknown error')}")
            await update.message.reply_text("Ошибка проверки оплаты.")
    except Exception as e:
        logger.error(f"Exception in check_payment for {user_id}: {e}")
        await update.message.reply_text("Ошибка проверки. Попробуй позже.")

async def kurs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /kurs"""
    user_id = str(update.message.from_user.id)
    args = context.args
    
    if not check_limit(user_id):
        await update.message.reply_text(f"Ты превысил лимит {FREE_REQUEST_LIMIT} бесплатных запросов в сутки. Подпишись на безлимит за {SUBSCRIPTION_PRICE} USDT: /subscribe")
        return
    
    if not args:
        await update.message.reply_text('Напиши, например: "/kurs usd btc" или "/kurs 44 доллара к эфиру".')
        return
    
    text = " ".join(args).lower()
    parts = text.split()
    logger.info(f"User {user_id} requested kurs: {text}")
    
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
            remaining = FREE_REQUEST_LIMIT - STATS["users"][user_id]["requests"] if not STATS["subscriptions"].get(user_id, False) else "∞"
            response = f"{amount} {from_code} = {result:.6f} {to_code}\nКурс: 1 {from_code} = {rate:.6f} {to_code}\nОсталось запросов сегодня: {remaining}{AD_MESSAGE}"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Error in kurs for {user_id}: {e}")
        await update.message.reply_text('Не понял запрос. Примеры: "/kurs usd btc" или "/kurs 44 доллара к эфиру".')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = str(update.message.from_user.id)
    current_time = time.time()
    
    if 'last_request' in context.user_data and current_time - context.user_data['last_request'] < 1:
        await update.message.reply_text('Слишком много запросов! Подожди секунду.')
        return
    
    if not check_limit(user_id):
        await update.message.reply_text(f"Ты превысил лимит {FREE_REQUEST_LIMIT} бесплатных запросов в сутки. Подпишись на безлимит за {SUBSCRIPTION_PRICE} USDT: /subscribe")
        return
    
    context.user_data['last_request'] = current_time
    text = update.message.text.lower()
    logger.info(f"User {user_id} sent message: {text}")
    
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
            remaining = FREE_REQUEST_LIMIT - STATS["users"][user_id]["requests"] if not STATS["subscriptions"].get(user_id, False) else "∞"
            response = f"{amount} {from_code} = {result:.6f} {to_code}\nКурс: 1 {from_code} = {rate:.6f} {to_code}\nОсталось запросов сегодня: {remaining}{AD_MESSAGE}"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Error in handle_message for {user_id}: {e}")

# Запуск Telegram-бота
application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("kurs", kurs_command))
application.add_handler(CommandHandler("subscribe", subscribe))
application.add_handler(CommandHandler("check", check_payment))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

if __name__ == "__main__":
    application.run_polling()
