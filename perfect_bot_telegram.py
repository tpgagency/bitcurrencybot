import os
import json
import time
import logging
import requests
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import redis
from telegram.error import NetworkError, RetryAfter, TelegramError

# Настройка логирования
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
CACHE_TIMEOUT = 120
ADMIN_IDS = ["1058875848", "6403305626"]

CURRENCIES = {
    'usd': {'id': 'usd', 'code': 'USD'},
    'uah': {'id': 'uah', 'code': 'UAH'},
    'eur': {'id': 'eur', 'code': 'EUR'},
    'rub': {'id': 'rub', 'code': 'RUB'},
    'jpy': {'id': 'jpy', 'code': 'JPY'},
    'cny': {'id': 'cny', 'code': 'CNY'},
    'gbp': {'id': 'gbp', 'code': 'GBP'},
    'kzt': {'id': 'kzt', 'code': 'KZT'},
    'try': {'id': 'try', 'code': 'TRY'},
    'btc': {'id': 'bitcoin', 'code': 'BTC'},
    'eth': {'id': 'ethereum', 'code': 'ETH'},
    'xrp': {'id': 'ripple', 'code': 'XRP'},
    'doge': {'id': 'dogecoin', 'code': 'DOGE'},
    'ada': {'id': 'cardano', 'code': 'ADA'},
    'sol': {'id': 'solana', 'code': 'SOL'},
    'ltc': {'id': 'litecoin', 'code': 'LTC'},
    'usdt': {'id': 'tether', 'code': 'USDT'},
    'bnb': {'id': 'binancecoin', 'code': 'BNB'},
    'trx': {'id': 'tron', 'code': 'TRX'},
    'dot': {'id': 'polkadot', 'code': 'DOT'},
    'matic': {'id': 'matic-network', 'code': 'MATIC'}
}

# Фиксированный курс UAH → USD (пример, обновляй регулярно)
UAH_TO_USD_FALLBACK = 0.025  # 1 UAH ≈ 0.025 USD (пример, проверь актуальный курс)

async def set_bot_commands(application: Application):
    commands = [
        ("start", "Меню бота"),
        ("currencies", "Платежи"),
        ("stats", "Статистика"),
        ("subscribe", "Подписка"),
        ("alert", "Уведомления"),
        ("referrals", "Рефералы")
    ]
    bot = application.bot
    await bot.set_my_commands(commands)

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        status = chat_member.status
        if status in ['member', 'administrator', 'creator']:
            logger.debug(f"User {user_id} is subscribed to @tpgbit")
            return True
        logger.debug(f"User {user_id} is not subscribed to @tpgbit, status: {status}")
        return False
    except TelegramError as e:
        logger.error(f"Error checking subscription for {user_id}: {e}")
        if update.message:
            await update.message.reply_text(
                "Не могу проверить подписку. Убедись, что бот — админ в @tpgbit, и попробуй снова."
            )
        else:
            await update.callback_query.edit_message_text(
                "Не могу проверить подписку. Убедись, что бот — админ в @tpgbit, и попробуй снова."
            )
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_subscription(update, context):
        return True
    if update.message:
        await update.message.reply_text(
            "Чтобы пользоваться ботом, подпишись на @tpgbit!\n"
            "После подписки повтори запрос."
        )
    else:
        await update.callback_query.edit_message_text(
            "Чтобы пользоваться ботом, подпишись на @tpgbit!\n"
            "После подписки повтори запрос."
        )
    return False

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
        stats["request_types"] = stats.get("request_types", {})
        stats["request_types"][request_type] = stats["request_types"].get(request_type, 0) + 1
        redis_client.set('stats', json.dumps(stats))
        logger.debug(f"Stats updated: {user_id} - {request_type}")
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def check_limit(user_id):
    try:
        if user_id in ADMIN_IDS:
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
    to_code = to_data['code'].lower()
    
    try:
        # Прямой запрос
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={from_id}&vs_currencies={to_code}"
        logger.debug(f"Fetching direct: {url}")
        response = requests.get(url, timeout=15).json()
        logger.info(f"Direct response: {json.dumps(response)}")
        
        if from_id in response and to_code in response[from_id]:
            rate = response[from_id][to_code]
            if rate <= 0:
                logger.error(f"Invalid direct rate: {rate}")
                return None, "Курс недоступен (нулевое значение)"
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        
        # Обратный запрос
        url_reverse = f"https://api.coingecko.com/api/v3/simple/price?ids={to_id}&vs_currencies={from_key}"
        logger.debug(f"Fetching reverse: {url_reverse}")
        response_reverse = requests.get(url_reverse, timeout=15).json()
        logger.info(f"Reverse response: {json.dumps(response_reverse)}")
        
        if to_id in response_reverse and from_key in response_reverse[to_id]:
            rate = 1 / response_reverse[to_id][from_key]
            if rate <= 0:
                logger.error(f"Invalid reverse rate: {rate}")
                return None, "Курс недоступен (нулевое значение)"
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            return amount * rate, rate
        
        # Косвенный курс через USD для всех пар (фиат-фиат, фиат-крипто, крипто-фиат)
        if from_key in CURRENCIES and to_key in CURRENCIES:
            # Обработка USDT как стейблкоина (1 USDT = 1 USD)
            if from_key == 'usdt':
                rate_from_usd = 1.0  # Фиксированный курс USDT → USD
            else:
                # Получаем курс от исходной валюты к USD
                url_from_usd = f"https://api.coingecko.com/api/v3/simple/price?ids={from_id}&vs_currencies=usd"
                response_from_usd = requests.get(url_from_usd, timeout=15).json()
                logger.info(f"From USD response: {json.dumps(response_from_usd)}")
                
                if from_id in response_from_usd and 'usd' in response_from_usd[from_id]:
                    rate_from_usd = response_from_usd[from_id]['usd']
                    if rate_from_usd <= 0:
                        logger.error(f"Invalid rate from {from_key} to USD: {rate_from_usd}")
                        # Резервный фиксированный курс для UAH, если данные отсутствуют
                        if from_key == 'uah':
                            logger.warning(f"Using fallback rate for UAH to USD: {UAH_TO_USD_FALLBACK}")
                            rate_from_usd = UAH_TO_USD_FALLBACK
                        else:
                            return None, "Курс недоступен (нулевое значение)"
                else:
                    logger.error(f"No rate found for {from_id} to USD")
                    # Резервный фиксированный курс для UAH, если данные отсутствуют
                    if from_key == 'uah':
                        logger.warning(f"Using fallback rate for UAH to USD: {UAH_TO_USD_FALLBACK}")
                        rate_from_usd = UAH_TO_USD_FALLBACK
                    else:
                        return None, "Курс недоступен: данные отсутствуют"
            
            # Получаем курс от USD к целевой валюте
            if to_key == 'usdt':
                rate_to_target = 1.0  # Фиксированный курс USD → USDT
            else:
                url_to_usd = f"https://api.coingecko.com/api/v3/simple/price?ids=usd&vs_currencies={to_code}"
                response_to_usd = requests.get(url_to_usd, timeout=15).json()
                logger.info(f"To USD response: {json.dumps(response_to_usd)}")
                
                if 'usd' in response_to_usd and to_code in response_to_usd['usd']:
                    rate_to_target = response_to_usd['usd'][to_code]
                    if rate_to_target <= 0:
                        logger.error(f"Invalid rate from USD to {to_key}: {rate_to_target}")
                        return None, "Курс недоступен (нулевое значение)"
                else:
                    logger.error(f"No rate found for USD to {to_id}")
                    return None, "Курс недоступен: данные отсутствуют"
            
            # Вычисляем итоговый курс: (1 / rate_from_usd) * rate_to_target
            final_rate = (1 / rate_from_usd) * rate_to_target
            if final_rate <= 0:
                logger.error(f"Invalid final rate: {final_rate}")
                return None, "Курс недоступен (нулевое значение)"
            
            redis_client.setex(cache_key, CACHE_TIMEOUT, final_rate)
            return amount * final_rate, final_rate
        
        logger.error(f"No rate found for {from_id} to {to_id}")
        return None, "Курс недоступен: данные отсутствуют"
    except requests.RequestException as e:
        logger.error(f"API error: {e}")
        return None, f"Ошибка API: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.message.from_user.id)
    save_stats(user_id, "start")
    logger.info(f"User {user_id} started bot")
    await update.message.reply_text(
        'Привет! Я бот для конвертации валют.\n'
        'Просто напиши коды валют, например: "usd btc" или "100 uah usdt".\n'
        f'Бесплатно: {FREE_REQUEST_LIMIT} запросов в сутки.\n'
        f'Безлимит: /subscribe за {SUBSCRIPTION_PRICE} USDT.\n'
        'Для списка валют используй /currencies.\n'
        'Выбери команду в меню Telegram (внизу слева).'
    )

async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    currency_list = ", ".join(sorted(CURRENCIES.keys()))
    await update.message.reply_text(f"Поддерживаемые валюты: {currency_list}")

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.message.from_user.id)
    args = context.args
    if len(args) != 3 or not args[2].replace('.', '', 1).isdigit():
        await update.message.reply_text('Пример: /alert usd btc 0.000015')
        return
    
    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        await update.message.reply_text("Ошибка: одна из валют не поддерживается")
        return
    
    alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
    alerts.append({"from": from_currency, "to": to_currency, "target": target_rate})
    redis_client.set(f"alerts:{user_id}", json.dumps(alerts))
    await update.message.reply_text(f"Уведомление установлено: {from_currency} → {to_currency} при курсе {target_rate}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    users = len(stats.get("users", {}))
    requests = stats.get("total_requests", 0)
    revenue = stats.get("revenue", 0.0)
    if user_id in ADMIN_IDS:
        await update.message.reply_text(f"Админ-статистика:\nПользователей: {users}\nЗапросов: {requests}\nДоход: {revenue} USDT")
    else:
        await update.message.reply_text(f"Твоя статистика:\nЗапросов сегодня: {stats.get('users', {}).get(user_id, {}).get('requests', 0)}")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.message.from_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    if stats.get("subscriptions", {}).get(user_id, False):
        logger.info(f"User {user_id} already subscribed")
        await update.message.reply_text("Ты уже подписан!")
        return
    
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(SUBSCRIPTION_PRICE),
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

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.message.from_user.id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
    await update.message.reply_text(
        f"Твоя реферальная ссылка: {ref_link}\n"
        f"Приглашено пользователей: {refs}\n"
        "Приглашай друзей и получай бонусы (скоро будет доступно)!"
    )

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    args = context.args
    if len(args) == 1 and args[0].startswith("ref_"):
        referrer_id = args[0].replace("ref_", "")
        if referrer_id.isdigit():
            referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
            if user_id not in referrals:
                referrals.append(user_id)
                redis_client.set(f"referrals:{referrer_id}", json.dumps(referrals))
                logger.info(f"New referral: {user_id} for {referrer_id}")
                await update.message.reply_text(
                    "Ты был приглашён через реферальную ссылку! Спасибо!"
                )

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    if not hasattr(context, 'user_data') or context.user_data is None:
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
    if not await enforce_subscription(update, context):
        return
    
    user_id = str(update.message.from_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id, False)
    delay = 1 if is_subscribed else 5
    
    if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
        await update.message.reply_text(f"Подожди {delay} секунд{'у' if delay == 1 else ''}!")
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
        if len(parts) < 2:
            raise ValueError("Недостаточно аргументов")
        if parts[0].replace('.', '', 1).isdigit():
            amount = float(parts[0])
            from_currency, to_currency = parts[1], parts[2]
            logger.debug(f"Parsed: amount={amount}, from={from_currency}, to={to_currency}")
        else:
            amount = 1
            from_currency, to_currency = parts[0], parts[1]
            logger.debug(f"Parsed: amount={amount}, from={from_currency}, to={to_currency}")
        
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate = get_exchange_rate(from_currency, to_currency, amount)
        if result:
            from_code = CURRENCIES[from_currency.lower()]['code']
            to_code = CURRENCIES[to_currency.lower()]['code']
            remaining_display = "∞" if user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id, False) else remaining
            await update.message.reply_text(
                f"{amount} {from_code} = {result:.6f} {to_code}\n"
                f"Курс: 1 {from_code} = {rate:.6f} {to_code}\n"
                f"Осталось запросов: {remaining_display}{AD_MESSAGE}"
            )
        else:
            await update.message.reply_text(f"Ошибка: {rate}")
    except Exception as e:
        logger.error(f"Message error for {user_id}: {e}")
        await update.message.reply_text('Примеры: "usd btc" или "100 uah usdt"\nИли используй меню через /start')

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.edit_message_text(
            "Введи сумму и валюты для конвертации, например: \"100 uah usdt\""
        )
    elif action == "price":
        await query.edit_message_text(
            "Введи валюту для проверки текущей цены, например: \"btc usd\""
        )
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

# Запуск
if __name__ == "__main__":
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Установить команды бота
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

    # Установить меню бота
    if not redis_client.exists('stats'):
        redis_client.set('stats', json.dumps({"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}))
    logger.info("Bot starting...")
    try:
        # Запустить приложение с бесконечным циклом
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except NetworkError as e:
        logger.error(f"Network error on start: {e}")
        time.sleep(5)
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
