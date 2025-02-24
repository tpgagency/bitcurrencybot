import requests
import json
import time
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from flask import Flask, render_template_string, request
from werkzeug.security import check_password_hash, generate_password_hash

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_BOT_TOKEN_HERE')  # Токен от BotFather
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN', 'YOUR_CRYPTO_PAY_TOKEN')  # Токен от @Send
ADMIN_PASSWORD_HASH = generate_password_hash('trust20242024')  # Пароль для дашборда (замени)
AD_MESSAGE = "\n\n📢 Реклама: Подпишись на мой канал @YourChannel для новостей о крипте и финансах!"
FREE_REQUEST_LIMIT = 10  # Лимит бесплатных запросов в сутки
SUBSCRIPTION_PRICE = 5  # Цена подписки в USDT (5 долларов в месяц)

# Словари валют
FIAT_CURRENCIES = {
    'доллар': 'USD', 'доллары': 'USD', 'доллара': 'USD', 'usd': 'USD',
    'гривна': 'UAH', 'гривны': 'UAH', 'гривен': 'UAH', 'uah': 'UAH',
    'евро': 'EUR', 'eur': 'EUR',
    'рубль': 'RUB', 'рубли': 'RUB', 'рубля': 'RUB', 'rub': 'RUB',
    'йена': 'JPY', 'йены': 'JPY', 'jpy': 'JPY',
    'юань': 'CNY', 'юани': 'CNY', 'cny': 'CNY',
    'фунт': 'GBP', 'фунты': 'GBP', 'gbp': 'GBP'
}

CRYPTO_CURRENCIES = {
    'биткоин': 'bitcoin', 'биткоины': 'bitcoin', 'биткоина': 'bitcoin', 'btc': 'bitcoin',
    'эфир': 'ethereum', 'эфириум': 'ethereum', 'эфира': 'ethereum', 'eth': 'ethereum',
    'рипл': 'ripple', 'риплы': 'ripple', 'xrp': 'ripple',
    'догекоин': 'dogecoin', 'доге': 'dogecoin', 'догекоина': 'dogecoin', 'doge': 'dogecoin',
    'кардано': 'cardano', 'карданы': 'cardano', 'ada': 'cardano',
    'солана': 'solana', 'соланы': 'solana', 'sol': 'solana',
    'лайткоин': 'litecoin', 'лайткоины': 'litecoin', 'ltc': 'litecoin'
}

# Глобальные переменные
CACHE = {}  # Кэш курсов валют
CACHE_TIMEOUT = 300  # 5 минут
STATS = {  # Статистика в памяти (сбрасывается при перезапуске Heroku)
    "users": {},  # {user_id: {"requests": int, "last_reset": str}}
    "total_requests": 0,
    "request_types": {}
}

def save_stats(user_id, request_type, context):
    """Сохраняет статистику запросов и подписок"""
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

    # Сохраняем подписки в bot_data (постоянно на Heroku)
    if "subscriptions" not in context.bot_data:
        context.bot_data["subscriptions"] = {}
    if "revenue" not in context.bot_data:
        context.bot_data["revenue"] = 0.0

def get_stats(context):
    """Возвращает статистику для дашборда"""
    total_users = len(STATS["users"])
    total_requests = STATS["total_requests"]
    popular_requests = sorted(STATS["request_types"].items(), key=lambda x: x[1], reverse=True)[:5]
    subscriptions = len(context.bot_data.get("subscriptions", {}))
    revenue = context.bot_data.get("revenue", 0.0)
    return total_users, total_requests, popular_requests, subscriptions, revenue

def check_limit(user_id, context):
    """Проверяет лимит запросов пользователя"""
    subscribed = context.bot_data.get("subscriptions", {}).get(user_id, False)
    if subscribed:
        return True
    user_data = STATS["users"].get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
    return user_data["requests"] < FREE_REQUEST_LIMIT

def get_exchange_rate(from_currency, to_currency, amount=1):
    """Получает курс валют с кэшированием"""
    from_key = from_currency.lower()
    to_key = to_currency.lower()
    cache_key = f"{from_key}_{to_key}"
    
    if cache_key in CACHE and time.time() - CACHE[cache_key]['timestamp'] < CACHE_TIMEOUT:
        rate = CACHE[cache_key]['rate']
        return amount * rate, rate
    
    from_is_crypto = from_key in CRYPTO_CURRENCIES
    to_is_crypto = to_key in CRYPTO_CURRENCIES
    
    from_code = CRYPTO_CURRENCIES.get(from_key) if from_is_crypto else FIAT_CURRENCIES.get(from_key, from_key.upper())
    to_code = CRYPTO_CURRENCIES.get(to_key) if to_is_crypto else FIAT_CURRENCIES.get(to_key, to_key.upper())
    
    try:
        if from_is_crypto or to_is_crypto:
            if from_is_crypto:
                url = f'https://api.coingecko.com/api/v3/simple/price?ids={from_code}&vs_currencies={to_code if not to_is_crypto else "usd"}'
                response = requests.get(url, timeout=5).json()
                rate = response[from_code]['usd' if to_is_crypto else to_code.lower()]
                if to_is_crypto:
                    url2 = f'https://api.coingecko.com/api/v3/simple/price?ids={to_code}&vs_currencies=usd'
                    response2 = requests.get(url2, timeout=5).json()
                    rate = rate / response2[to_code]['usd']
            else:
                url = f'https://api.coingecko.com/api/v3/simple/price?ids={to_code}&vs_currencies={from_code.lower()}'
                response = requests.get(url, timeout=5).json()
                rate = response[to_code][from_code.lower()]
        else:
            url = f'https://api.exchangerate-api.com/v4/latest/{from_code}'
            response = requests.get(url, timeout=5).json()
            rate = response['rates'][to_code]
        
        result = amount * rate
        CACHE[cache_key] = {'rate': rate, 'timestamp': time.time()}
        return result, rate
    except Exception:
        try:
            if from_key not in FIAT_CURRENCIES and from_key not in CRYPTO_CURRENCIES:
                from_code = from_key.upper() if not from_is_crypto else from_key
            if to_key not in FIAT_CURRENCIES and to_key not in CRYPTO_CURRENCIES:
                to_code = to_key.upper() if not to_is_crypto else to_key
            
            if from_code in CRYPTO_CURRENCIES.values() or to_code in CRYPTO_CURRENCIES.values():
                if from_code in CRYPTO_CURRENCIES.values():
                    url = f'https://api.coingecko.com/api/v3/simple/price?ids={from_code}&vs_currencies={to_code if to_code not in CRYPTO_CURRENCIES.values() else "usd"}'
                    response = requests.get(url, timeout=5).json()
                    rate = response[from_code]['usd' if to_code in CRYPTO_CURRENCIES.values() else to_code.lower()]
                    if to_code in CRYPTO_CURRENCIES.values():
                        url2 = f'https://api.coingecko.com/api/v3/simple/price?ids={to_code}&vs_currencies=usd'
                        response2 = requests.get(url2, timeout=5).json()
                        rate = rate / response2[to_code]['usd']
                else:
                    url = f'https://api.coingecko.com/api/v3/simple/price?ids={to_code}&vs_currencies={from_code.lower()}'
                    response = requests.get(url, timeout=5).json()
                    rate = response[to_code][from_code.lower()]
            else:
                url = f'https://api.exchangerate-api.com/v4/latest/{from_code}'
                response = requests.get(url, timeout=5).json()
                rate = response['rates'][to_code]
            
            result = amount * rate
            CACHE[cache_key] = {'rate': rate, 'timestamp': time.time()}
            return result, rate
        except Exception:
            return None, "Не удалось получить курс."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = str(update.message.from_user.id)
    save_stats(user_id, "start", context)
    await update.message.reply_text('Привет! Я бот для конвертации валют. Используй /kurs, например: "/kurs usd btc" или "/kurs 44 доллара к эфиру".\nБесплатно — 10 запросов в сутки, для безлимита — /subscribe за 5 USDT.')

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /subscribe для подписки"""
    user_id = str(update.message.from_user.id)
    if context.bot_data.get("subscriptions", {}).get(user_id, False):
        await update.message.reply_text("Ты уже подписан на безлимит!")
        return
    
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = {
        "amount": str(SUBSCRIPTION_PRICE),
        "currency": "USDT",
        "description": f"Безлимитная подписка для {user_id}"
    }
    response = requests.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload).json()
    
    if response.get("ok"):
        invoice_id = response["result"]["invoice_id"]
        pay_url = response["result"]["pay_url"]
        keyboard = [[InlineKeyboardButton(f"Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.user_data[user_id] = {"invoice_id": invoice_id}
        await update.message.reply_text(f"Оплати {SUBSCRIPTION_PRICE} USDT для безлимитного доступа:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Ошибка при создании платежа. Попробуй позже.")

async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /check для проверки оплаты"""
    user_id = str(update.message.from_user.id)
    if user_id not in context.user_data or "invoice_id" not in context.user_data[user_id]:
        await update.message.reply_text("Сначала запроси подписку через /subscribe!")
        return
    
    invoice_id = context.user_data[user_id]["invoice_id"]
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    response = requests.get(f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}", headers=headers).json()
    
    if response.get("ok") and response["result"]["items"]:
        status = response["result"]["items"][0]["status"]
        if status == "paid":
            context.bot_data.setdefault("subscriptions", {})[user_id] = True
            context.bot_data["revenue"] = context.bot_data.get("revenue", 0.0) + SUBSCRIPTION_PRICE
            del context.user_data[user_id]
            await update.message.reply_text("Оплата подтверждена! Теперь у тебя безлимит.")
        else:
            await update.message.reply_text("Оплата ещё не подтверждена. Проверь статус в @Send и попробуй снова через /check.")
    else:
        await update.message.reply_text("Ошибка проверки оплаты. Попробуй позже.")

async def kurs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /kurs для конвертации валют"""
    user_id = str(update.message.from_user.id)
    args = context.args
    
    # Проверка оплаты
    if user_id in context.user_data and "invoice_id" in context.user_data[user_id]:
        invoice_id = context.user_data[user_id]["invoice_id"]
        headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
        response = requests.get(f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}", headers=headers).json()
        if response.get("ok") and response["result"]["items"] and response["result"]["items"][0]["status"] == "paid":
            context.bot_data.setdefault("subscriptions", {})[user_id] = True
            context.bot_data["revenue"] = context.bot_data.get("revenue", 0.0) + SUBSCRIPTION_PRICE
            del context.user_data[user_id]
            await update.message.reply_text("Оплата подтверждена! Теперь у тебя безлимит.")
    
    # Проверка лимита
    if not check_limit(user_id, context):
        await update.message.reply_text(f"Ты превысил лимит {FREE_REQUEST_LIMIT} бесплатных запросов в сутки. Подпишись на безлимит за {SUBSCRIPTION_PRICE} USDT: /subscribe")
        return
    
    if not args:
        await update.message.reply_text('Напиши, например: "/kurs usd btc", "/kurs 100 usd btc" или "/kurs 44 доллара к эфиру".')
        return
    
    text = " ".join(args).lower()
    parts = text.split()
    
    try:
        if 'к' in parts:
            k_index = parts.index('к')
            amount_part = parts[:k_index]
            from_part = parts[k_index-1]
            to_part = " ".join(parts[k_index+1:])
            
            if amount_part and amount_part[0].replace('.', '', 1).isdigit():
                amount = float(amount_part[0])
                from_currency = from_part
            else:
                amount = 1
                from_currency = " ".join(amount_part)
            to_currency = to_part
        else:
            if len(parts) >= 2 and parts[0].replace('.', '', 1).isdigit():
                amount = float(parts[0])
                from_currency, to_currency = parts[1], parts[2]
            else:
                amount = 1
                from_currency, to_currency = parts[0], parts[1]
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}", context)
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        
        if result:
            remaining = FREE_REQUEST_LIMIT - STATS["users"][user_id]["requests"] if not context.bot_data.get("subscriptions", {}).get(user_id, False) else "∞"
            response = f"{amount} {from_currency} = {result:.4f} {to_currency}\nКурс: 1 {from_currency} = {rate:.4f} {to_currency}\nОсталось запросов сегодня: {remaining}{AD_MESSAGE}"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception:
        await update.message.reply_text('Не понял запрос. Примеры: "/kurs usd btc", "/kurs 100 usd btc", "/kurs 44 доллара к эфиру".')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений без команд"""
    user_id = str(update.message.from_user.id)
    current_time = time.time()
    
    if 'last_request' in context.user_data and current_time - context.user_data['last_request'] < 1:
        await update.message.reply_text('Слишком много запросов! Подожди секунду.')
        return
    
    if not check_limit(user_id, context):
        await update.message.reply_text(f"Ты превысил лимит {FREE_REQUEST_LIMIT} бесплатных запросов в сутки. Подпишись на безлимит за {SUBSCRIPTION_PRICE} USDT: /subscribe")
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
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}", context)
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        if result:
            remaining = FREE_REQUEST_LIMIT - STATS["users"][user_id]["requests"] if not context.bot_data.get("subscriptions", {}).get(user_id, False) else "∞"
            response = f"{amount} {from_currency} = {result:.4f} {to_currency}\nКурс: 1 {from_currency} = {rate:.4f} {to_currency}\nОсталось запросов сегодня: {remaining}{AD_MESSAGE}"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception:
        pass

# Flask приложение для дашборда
app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def dashboard():
    """Веб-дашборд с паролем"""
    if request.method == 'POST':
        password = request.form.get('password')
        if not check_password_hash(ADMIN_PASSWORD_HASH, password):
            return "Неверный пароль!", 403
    
    # Доступ к bot_data невозможен напрямую в Flask, поэтому статистика ограничена памятью
    total_users, total_requests, popular_requests, subscriptions, revenue = get_stats(app.bot_context if hasattr(app, 'bot_context') else {})
    
    html = """
    <html>
        <head>
            <title>Дашборд бота</title>
            <meta http-equiv="refresh" content="30">
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; background: #f4f4f4; }
                h1 { color: #333; }
                .stat { background: white; padding: 15px; margin: 10px 0; border-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            </style>
        </head>
        <body>
            <h1>Статистика твоего бота</h1>
            <div class="stat">Уникальных пользователей: {{ total_users }}</div>
            <div class="stat">Всего запросов: {{ total_requests }}</div>
            <div class="stat">Подписок: {{ subscriptions }}</div>
            <div class="stat">Доход: {{ revenue }} USDT</div>
            <div class="stat">
                <h3>Популярные запросы:</h3>
                <ul>
                    {% for req, count in popular_requests %}
                        <li>{{ req }}: {{ count }} раз</li>
                    {% endfor %}
                </ul>
            </div>
            {% if not password_entered %}
                <form method="post">
                    <input type="password" name="password" placeholder="Введите пароль">
                    <input type="submit" value="Войти">
                </form>
            {% endif %}
        </body>
    </html>
    """
    return render_template_string(html, total_users=total_users, total_requests=total_requests, 
                                 popular_requests=popular_requests, subscriptions=subscriptions, 
                                 revenue=revenue, password_entered='password' in request.form)

# Запуск бота и Flask
port = int(os.getenv("PORT", 5000))
application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("kurs", kurs_command))
application.add_handler(CommandHandler("subscribe", subscribe))
application.add_handler(CommandHandler("check", check_payment))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

if __name__ == "__main__":
    from telegram.ext import ApplicationRunner
    runner = ApplicationRunner()
    runner.run(application, run_polling=True)
    app.bot_context = application.bot_data  # Передаём bot_data в Flask (грубый хак)
    app.run(host="0.0.0.0", port=port)