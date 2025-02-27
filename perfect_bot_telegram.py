import os
import json
import time
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
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
from typing import Optional, Tuple, Dict, Any, List, Union
from functools import wraps

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_PAY_TOKEN = os.getenv('CRYPTO_PAY_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CHANNEL_USERNAME = "@tpgbit"
BOT_USERNAME = "BitCurrencyBot"

# Проверка обязательных переменных окружения
if not TELEGRAM_TOKEN or not CRYPTO_PAY_TOKEN:
    logger.critical("Missing TELEGRAM_TOKEN or CRYPTO_PAY_TOKEN")
    exit(1)

# Константы
AD_MESSAGE = "\n\n📢 Подпишись на @tpgbit для новостей о крипте\!"
FREE_REQUEST_LIMIT = 5
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 300
ADMIN_IDS = {"1058875848", "6403305626"}
HISTORY_LIMIT = 20
MAX_RETRIES = 3
HIGH_PRECISION_CURRENCIES = {'BTC', 'ETH', 'XRP', 'DOGE', 'ADA', 'SOL', 'LTC', 'BNB', 'TRX', 'DOT', 'MATIC'}

# API URLs
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

# Резервные курсы для UAH/USDT
UAH_TO_USDT_FALLBACK = 0.0239
USDT_TO_UAH_FALLBACK = 41.84

# Инициализация Redis
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none", socket_timeout=10)
except Exception as e:
    logger.critical(f"Failed to initialize Redis client: {e}")
    exit(1)

def init_redis_connection() -> bool:
    """Инициализация соединения с Redis с попытками повторного подключения"""
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

def require_subscription(func):
    """Декоратор для проверки подписки на канал"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await enforce_subscription(update, context):
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def rate_limit(func):
    """Декоратор для ограничения частоты запросов"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = str(update.effective_user.id)
        stats = json.loads(redis_client.get('stats') or '{}')
        is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id)
        delay = 1 if is_subscribed else 5

        # Проверка частоты запросов
        if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
            message = f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}\!"
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Проверка лимита бесплатных запросов
        can_proceed, remaining = check_limit(user_id)
        if not can_proceed:
            message = f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан\. /subscribe"
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Обновление времени последнего запроса
        context.user_data['last_request'] = time.time()
        return await func(update, context, *args, **kwargs)
    return wrapper

async def set_bot_commands(application):
    """Установка команд бота"""
    await application.bot.set_my_commands([
        ("start", "Главное меню"), 
        ("currencies", "Список валют"), 
        ("stats", "Статистика"),
        ("subscribe", "Подписка"), 
        ("alert", "Уведомления"), 
        ("referrals", "Рефералы"),
        ("history", "История запросов")
    ])
    logger.info("Bot commands set")

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> bool:
    """Проверка подписки пользователя на канал"""
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except TelegramError as e:
        logger.error(f"Failed to check subscription for {user_id}: {e}")
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка и требование подписки на канал"""
    user_id = str(update.effective_user.id)
    if await check_subscription(context, user_id):
        return True
    
    # Отправка сообщения о необходимости подписки
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "🚫 Подпишись на @tpgbit, чтобы продолжить\!",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        await update.effective_message.reply_text(
            "🚫 Подпишись на @tpgbit, чтобы продолжить\!",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    return False

def save_stats(user_id: str, request_type: str):
    """Сохранение статистики запросов"""
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        current_day = time.strftime("%Y-%m-%d")
        
        # Инициализация данных пользователя
        users = stats.setdefault("users", {})
        user_data = users.setdefault(user_id, {"requests": 0, "last_reset": current_day})
        
        # Сброс запросов в новый день
        if user_data["last_reset"] != current_day:
            user_data.update(requests=0, last_reset=current_day)
        
        # Обновление счетчиков
        user_data["requests"] += 1
        stats["total_requests"] = stats.get("total_requests", 0) + 1
        stats.setdefault("request_types", {}).setdefault(request_type, 0)
        stats["request_types"][request_type] += 1
        
        # Сохранение в Redis
        redis_client.setex('stats', 24 * 60 * 60, json.dumps(stats))
    except Exception as e:
        logger.error(f"Error saving stats for {user_id}: {e}")

def save_history(user_id: str, from_currency: str, to_currency: str, amount: float, result: float):
    """Сохранение истории конвертаций пользователя"""
    try:
        history = deque(json.loads(redis_client.get(f"history:{user_id}") or '[]'), maxlen=HISTORY_LIMIT)
        history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from": from_currency, 
            "to": to_currency,
            "amount": amount, 
            "result": result
        })
        redis_client.setex(f"history:{user_id}", 30 * 24 * 60 * 60, json.dumps(list(history)))
    except Exception as e:
        logger.error(f"Error saving history for {user_id}: {e}")

def check_limit(user_id: str) -> Tuple[bool, str]:
    """Проверка лимита запросов"""
    try:
        # Администраторы и подписчики имеют безлимитный доступ
        if user_id in ADMIN_IDS:
            return True, "∞"
            
        stats = json.loads(redis_client.get('stats') or '{}')
        if stats.get("subscriptions", {}).get(user_id):
            return True, "∞"
            
        # Проверка оставшихся запросов для обычных пользователей
        users = stats.get("users", {})
        user_data = users.get(user_id, {"requests": 0, "last_reset": time.strftime("%Y-%m-%d")})
        remaining = FREE_REQUEST_LIMIT - user_data["requests"]
        
        return remaining > 0, str(remaining)
    except Exception as e:
        logger.error(f"Error checking limit for {user_id}: {e}")
        return False, "0"

def get_exchange_rate(from_currency: str, to_currency: str, amount: float = 1.0) -> Tuple[Optional[float], str]:
    """Получение курса обмена валют"""
    # Нормализация входных данных
    from_key, to_key = from_currency.lower(), to_currency.lower()
    
    # Проверка поддерживаемых валют
    if from_key not in CURRENCIES or to_key not in CURRENCIES:
        return None, "Неподдерживаемая валюта"
    
    # Кэширование запросов
    cache_key = f"rate:{from_key}_{to_key}"
    cached = redis_client.get(cache_key)
    if cached:
        rate = float(cached)
        return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (cached)"

    from_code, to_code = CURRENCIES.get(from_key)['code'], CURRENCIES.get(to_key)['code']
    
    # Обработка одинаковых валют
    if from_key == to_key:
        redis_client.setex(cache_key, CACHE_TIMEOUT, 1.0)
        return amount, f"1 {from_key.upper()} = 1 {to_key.upper()}"

    def fetch_rate(url: str, key: str, reverse: bool = False, api_name: str = "API") -> Optional[float]:
        """Получение курса из API"""
        try:
            response = requests.get(url, timeout=5).json()
            rate = float(response[key if not reverse else 'price'])
            return 1 / rate if reverse and rate > 0 else rate if rate > 0 else None
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            logger.warning(f"Error fetching rate from {api_name}: {e}")
            return None

    # Попытка получить курс напрямую из различных источников
    sources = [
        (f"{BINANCE_API_URL}?symbol={from_code}{to_code}", 'price', False, "Binance direct"),
        (f"{BINANCE_API_URL}?symbol={to_code}{from_code}", 'price', True, "Binance reverse"),
        (WHITEBIT_API_URL, f"{from_code}_{to_code}", False, "WhiteBIT direct"),
        (WHITEBIT_API_URL, f"{to_code}_{from_code}", True, "WhiteBIT reverse"),
    ]

    for url, pair, reverse, source in sources:
        rate = fetch_rate(url, 'price' if 'binance' in url else pair, reverse, source.split()[0])
        if rate:
            redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
            formatted_rate = rate if not reverse else 1/rate
            return amount * formatted_rate, f"1 {from_code} = {formatted_rate} {to_code} ({source})"

    # Попытка конвертации через промежуточную валюту
    for bridge in ('USDT', 'BTC'):
        if from_key != bridge.lower() and to_key != bridge.lower():
            rate_from = fetch_rate(f"{BINANCE_API_URL}?symbol={from_code}{bridge}", 'price')
            if not rate_from:
                rate_from = fetch_rate(f"{BINANCE_API_URL}?symbol={bridge}{from_code}", 'price', True)
                
            rate_to = fetch_rate(f"{BINANCE_API_URL}?symbol={bridge}{to_code}", 'price')
            if not rate_to:
                rate_to = fetch_rate(f"{BINANCE_API_URL}?symbol={to_code}{bridge}", 'price', True)
                
            if rate_from and rate_to:
                bridge_rate = rate_from * (1 / rate_to)
                if bridge_rate > 0:
                    redis_client.setex(cache_key, CACHE_TIMEOUT, bridge_rate)
                    return amount * bridge_rate, f"1 {from_code} = {bridge_rate} {to_code} (Binance via {bridge})"

    # Резервные курсы для UAH/USDT
    if from_key == 'uah' and to_key == 'usdt':
        rate = UAH_TO_USDT_FALLBACK
    elif from_key == 'usdt' and to_key == 'uah':
        rate = USDT_TO_UAH_FALLBACK
    else:
        return None, "Курс недоступен"
        
    redis_client.setex(cache_key, CACHE_TIMEOUT, rate)
    return amount * rate, f"1 {from_key.upper()} = {rate} {to_key.upper()} (fallback)"

@require_subscription
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = str(update.effective_user.id)
    save_stats(user_id, "start")
    
    # Обработка реферальной ссылки
    if context.args and context.args[0].startswith("ref_"):
        await handle_referral(update, context)

    # Клавиатура главного меню
    keyboard = [
        [
            InlineKeyboardButton("💱 Конвертер", callback_data="converter"), 
            InlineKeyboardButton("📈 Курсы", callback_data="price")
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"), 
            InlineKeyboardButton("💎 Подписка", callback_data="subscribe")
        ],
        [
            InlineKeyboardButton("🔔 Уведомления", callback_data="alert"), 
            InlineKeyboardButton("👥 Рефералы", callback_data="referrals")
        ],
        [InlineKeyboardButton("📜 История", callback_data="history")]
    ]
    
    await update.effective_message.reply_text(
        f"👋 *Привет*\! Я {BOT_USERNAME} — твой помощник для конвертации валют\!\n"
        f"🔑 *Бесплатно*: {FREE_REQUEST_LIMIT} запросов в сутки\n"
        f"🌟 *Безлимит*: /subscribe за {SUBSCRIPTION_PRICE} USDT{AD_MESSAGE}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

@require_subscription
async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /currencies"""
    await update.effective_message.reply_text(
        f"💱 *Поддерживаемые валюты*:\n{', '.join(sorted(CURRENCIES.keys()))}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")]]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

@require_subscription
async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /alert"""
    user_id = str(update.effective_user.id)
    args = context.args
    
    # Проверка правильности формата команды
    if len(args) != 3 or not args[2].replace('.', '', 1).isdigit():
        keyboard = [
            [InlineKeyboardButton("🔔 USD → BTC", callback_data="alert_example_usd_btc")],
            [InlineKeyboardButton("🔔 EUR → UAH", callback_data="alert_example_eur_uah")],
            [InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        await update.effective_message.reply_text(
            "🔔 *Настрой уведомления*\! Формат: `/alert <валюта1> <валюта2> <курс>`\nПримеры ниже:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    # Проверка валют и создание уведомления
    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        await update.effective_message.reply_text(
            "❌ Ошибка: валюта не поддерживается", 
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    # Сохранение уведомления
    alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
    alerts.append({"from": from_currency, "to": to_currency, "target": target_rate})
    redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(alerts))
    
    await update.effective_message.reply_text(
        f"🔔 *Уведомление*: {from_currency.upper()} → {to_currency.upper()} при курсе {target_rate}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔔 Добавить ещё", callback_data="alert"), 
                InlineKeyboardButton("🔙 Назад", callback_data="start")
            ]
        ]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

@require_subscription
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats"""
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
    
    # Разные сообщения для администраторов и обычных пользователей
    if user_id in ADMIN_IDS:
        text = (f"📊 *Админ-статистика*:\n"
                f"👥 Пользователей: {len(stats.get('users', {}))}\n"
                f"📈 Запросов: {stats.get('total_requests', 0)}\n"
                f"💰 Доход: {stats.get('revenue', 0.0)} USDT")
    else:
        text = f"📊 *Твоя статистика*:\n📈 Запросов сегодня: {stats.get('users', {}).get(user_id, {}).get('requests', 0)}"
    
    await update.effective_message.reply_text(
        text, 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.MARKDOWN_V2
    )

@require_subscription
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /subscribe"""
    user_id = str(update.effective_user.id)
    stats = json.loads(redis_client.get('stats') or '{}')
    
    # Проверка существующей подписки
    if stats.get("subscriptions", {}).get(user_id):
        await update.effective_message.reply_text(
            "💎 Ты уже подписан\!", 
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    # Создание счета на оплату
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
            error_msg = response.get('error', 'Неизвестно')
            logger.error(f"Payment error for {user_id}: {error_msg}")
            await update.effective_message.reply_text(
                f"❌ Ошибка платежа: {error_msg}", 
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except requests.RequestException as e:
        logger.error(f"Subscribe error for {user_id}: {e}")
        await update.effective_message.reply_text(
            "❌ Ошибка связи с платежной системой", 
            parse_mode=ParseMode.MARKDOWN_V2
        )

@require_subscription
async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /referrals"""
    user_id = str(update.effective_user.id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
    
    await update.effective_message.reply_text(
        f"👥 *Реф. ссылка*: `{ref_link}`\n👤 Приглашено: *{refs}*\n🌟 Бонусы скоро будут\!",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔗 Копировать", callback_data="copy_ref"), 
                InlineKeyboardButton("🔙 Назад", callback_data="start")
            ]
        ]),
        parse_mode=ParseMode.MARKDOWN_V2
    )

@require_subscription
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /history"""
    user_id = str(update.effective_user.id)
    history = json.loads(redis_client.get(f"history:{user_id}") or '[]')
    back_button = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
    
    if not history:
        await update.effective_message.reply_text(
            "📜 *История пуста*\.",
            reply_markup=InlineKeyboardMarkup(back_button),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    
    response = "📜 *История запросов*:\n" + "\n".join(
        f"⏰ {entry['time']}: *{entry['amount']} {entry['from']}* → *{entry['result']} {entry['to']}*"
        for entry in reversed(history)
    )
    
    await update.effective_message.reply_text(
        response, 
        reply_markup=InlineKeyboardMarkup(back_button),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка реферальной ссылки"""
    user_id = str(update.effective_user.id)
    if context.args and context.args[0].startswith("ref_"):
        referrer_id = context.args[0].replace("ref_", "")
        if referrer_id.isdigit() and referrer_id != user_id:  # Защита от самореферала
            referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
            # Проверка, чтобы пользователь не был уже в списке рефералов
            if user_id not in referrals:
                referrals.append(user_id)
                redis_client.setex(f"referrals:{referrer_id}", 30 * 24 * 60 * 60, json.dumps(referrals))
                await update.effective_message.reply_text(
                    "👥 Спасибо за присоединение по реф. ссылке\!", 
                    parse_mode=ParseMode.MARKDOWN_V2
                )

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверка статуса платежей"""
    for user_id, data in list(context.user_data.items()):
        if "invoice_id" not in data:
            continue
        
        try:
            response = requests.get(
                f"https://pay.crypt.bot/api/getInvoices?invoice_ids={data['invoice_id']}",
                headers={'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN},
                timeout=15
            ).json()
            
            if response.get("ok") and response["result"]["items"] and response["result"]["items"][0]["status"] == "paid":
                stats = json.loads(redis_client.get('stats') or '{}')
                stats.setdefault("subscriptions", {})[user_id] = True
                stats["revenue"] = stats.get("revenue", 0.0) + SUBSCRIPTION_PRICE
                redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps(stats))
                
                # Удаление информации о счете после успешной оплаты
                del context.user_data[user_id]
                
                # Уведомление пользователя
                await context.bot.send_message(
                    user_id, 
                    "💎 Оплата прошла\! Безлимит активирован\.", 
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        except Exception as e:
            logger.error(f"Payment check error for {user_id}: {e}")

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверка условий для уведомлений"""
    stats = json.loads(redis_client.get('stats') or '{}')
    
    for user_id in stats.get("users", {}):
        alerts = json.loads(redis_client.get(f
