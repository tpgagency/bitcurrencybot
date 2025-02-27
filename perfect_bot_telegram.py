import os
import json
import time
import logging
import asyncio
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
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

if not TELEGRAM_TOKEN or not CRYPTO_PAY_TOKEN:
    logger.critical("Missing TELEGRAM_TOKEN or CRYPTO_PAY_TOKEN")
    exit(1)

AD_MESSAGE = "\n\n📢 Подпишись на @tpgbit для новостей о крипте\\!"
FREE_REQUEST_LIMIT = 5
SUBSCRIPTION_PRICE = 5
CACHE_TIMEOUT = 300  # 5 минут для кэша курсов
ADMIN_IDS = {"1058875848", "6403305626"}
HISTORY_LIMIT = 20
MAX_RETRIES = 3
HIGH_PRECISION_CURRENCIES = {'BTC', 'ETH', 'XRP', 'DOGE', 'ADA', 'SOL', 'LTC', 'BNB', 'TRX', 'DOT', 'MATIC'}

BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price"
WHITEBIT_API_URL = "https://whitebit.com/api/v1/public/ticker"
KUCOIN_API_URL = "https://api.kucoin.com/api/v1/market/allTickers"

CURRENCIES = {
    'usd': {'code': 'USDT'}, 'uah': {'code': 'UAH'}, 'eur': {'code': 'EUR'},
    'rub': {'code': 'RUB'}, 'jpy': {'code': 'JPY'}, 'cny': {'code': 'CNY'},
    'gbp': {'code': 'GBP'}, 'kzt': {'code': 'KZT'}, 'try': {'code': 'TRY'},
    'btc': {'code': 'BTC'}, 'eth': {'code': 'ETH'}, 'xrp': {'code': 'XRP'},
    'doge': {'code': 'DOGE'}, 'ada': {'code': 'ADA'}, 'sol': {'code': 'SOL'},
    'ltc': {'code': 'LTC'}, 'usdt': {'code': 'USDT'}, 'bnb': {'code': 'BNB'},
    'trx': {'code': 'TRX'}, 'dot': {'code': 'DOT'}, 'matic': {'code': 'MATIC'}
}

UAH_TO_USDT_FALLBACK = 0.0239  # 1 UAH = 0.0239 USDT
USDT_TO_UAH_FALLBACK = 41.84   # 1 USDT = 41.84 UAH
EUR_TO_USDT_FALLBACK = 1.08    # 1 EUR = 1.08 USDT

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none", socket_timeout=10)

def init_redis_connection() -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            redis_client.ping()
            logger.info("Successfully connected to Redis")
            return True
        except redis.ConnectionError as e:
            logger.warning(f"Redis connection attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
            time.sleep(2 ** attempt)
    logger.critical("Failed to connect to Redis after retries")
    return False

if not init_redis_connection():
    exit(1)

def escape_markdown_v2(text: str) -> str:
    reserved_chars = r'_*[]()~`>#+-=|{}!.'
    for char in reserved_chars:
        text = text.replace(char, f'\\{char}')
    return text

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except TelegramError as e:
        logger.error(f"Failed to check subscription for user {user_id}: {e}")
        return False

async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = str(update.effective_user.id)
    if await check_subscription(context, user_id):
        return True
    message = "🚫 Подпишись на @tpgbit, чтобы продолжить\\!"
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as e:
        logger.error(f"Failed to send subscription message to {user_id}: {e}")
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
        logger.error(f"Error saving stats for user {user_id}: {e}")

def save_history(user_id: str, from_currency: str, to_currency: str, amount: float, result: float):
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
        logger.error(f"Error saving history for user {user_id}: {e}")

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
        logger.error(f"Error checking limit for user {user_id}: {e}")
        return False, "0"

async def fetch_rate(session: aiohttp.ClientSession, url: str, key: str, reverse: bool = False, api_name: str = "API") -> Optional[float]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
            data = await response.json()
            logger.debug(f"API response from {api_name}: {data}")
            rate = float(data.get(key if not reverse else 'price', 0))
            if rate <= 0:
                logger.warning(f"{api_name} returned invalid rate: {rate}")
                return None
            return 1 / rate if reverse else rate
    except (aiohttp.ClientError, ValueError, KeyError, TypeError) as e:
        logger.warning(f"Error fetching rate from {api_name}: {str(e)}")
        return None

async def fetch_kucoin_rate(session: aiohttp.ClientSession, from_code: str, to_code: str) -> Optional[float]:
    try:
        async with session.get(KUCOIN_API_URL, timeout=aiohttp.ClientTimeout(total=5)) as response:
            data = await response.json()
            logger.debug(f"KuCoin API response: {data}")
            ticker = f"{from_code}-{to_code}"
            for item in data['data']['ticker']:
                if item['symbol'] == ticker:
                    rate = float(item['last'])
                    if rate > 0:
                        return rate
            return None
    except (aiohttp.ClientError, ValueError, KeyError, TypeError) as e:
        logger.warning(f"Error fetching rate from KuCoin: {e}")
        return None

async def get_exchange_rate(from_currency: str, to_currency: str, amount: float = 1.0) -> Tuple[Optional[float], str]:
    from_key, to_key = from_currency.lower(), to_currency.lower()
    if from_key not in CURRENCIES or to_key not in CURRENCIES:
        return None, "Неподдерживаемая валюта или неверный формат\\. Пример: `100\\.0 uah usdt`"

    cache_key = f"rate:{from_key}_{to_key}"
    cached = redis_client.get(cache_key)
    if cached:
        try:
            rate = float(cached)
            return amount * rate, f"1 {from_key.upper()} \\= {escape_markdown_v2(str(rate))} {to_key.upper()} \\(cached\\)"
        except ValueError as e:
            logger.warning(f"Invalid cached rate for {from_key}_{to_key}: {e}")

    from_code, to_code = CURRENCIES[from_key]['code'], CURRENCIES[to_key]['code']
    if from_key == to_key:
        redis_client.setex(cache_key, CACHE_TIMEOUT, 1.0)
        return amount, f"1 {from_key.upper()} \\= 1 {to_key.upper()}"

    async with aiohttp.ClientSession() as session:
        # Параллельные запросы к API
        tasks = [
            fetch_rate(session, f"{BINANCE_API_URL}?symbol={from_code}{to_code}", 'price', False, "Binance direct"),
            fetch_rate(session, f"{BINANCE_API_URL}?symbol={to_code}{from_code}", 'price', True, "Binance reverse"),
            fetch_rate(session, WHITEBIT_API_URL, f"{from_code}_{to_code}", False, "WhiteBIT direct"),
            fetch_rate(session, WHITEBIT_API_URL, f"{to_code}_{from_code}", True, "WhiteBIT reverse"),
            fetch_kucoin_rate(session, from_code, to_code)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_rates = []
        sources = ["Binance direct", "Binance reverse", "WhiteBIT direct", "WhiteBIT reverse", "KuCoin"]
        for rate, source in zip(results, sources):
            if isinstance(rate, float) and rate > 0:
                valid_rates.append((rate, source))

        if valid_rates:
            rate, source = valid_rates[0]  # Берем первый валидный курс
            redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
            return amount * rate, f"1 {from_code} \\= {escape_markdown_v2(str(rate))} {to_code} \\({escape_markdown_v2(source)}\\)"

        # Мост через USDT
        if from_key != 'usdt' and to_key != 'usdt':
            tasks_usdt = [
                fetch_rate(session, f"{BINANCE_API_URL}?symbol={from_code}USDT", 'price', False, f"Binance {from_code}USDT"),
                fetch_rate(session, f"{BINANCE_API_URL}?symbol=USDT{to_code}", 'price', True, f"Binance USDT{to_code}")
            ]
            usdt_results = await asyncio.gather(*tasks_usdt, return_exceptions=True)

            rate_from_usdt = usdt_results[0] if isinstance(usdt_results[0], float) and usdt_results[0] > 0 else None
            rate_to_usdt = usdt_results[1] if isinstance(usdt_results[1], float) and usdt_results[1] > 0 else None

            if rate_from_usdt and rate_to_usdt:
                rate = rate_from_usdt / rate_to_usdt
                redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
                return amount * rate, f"1 {from_code} \\= {escape_markdown_v2(str(rate))} {to_code} \\(Binance via USDT\\)"

        # Fallback для BTC, ETH и других валют
        if from_key == 'btc' and to_key in ['usdt', 'eur', 'uah']:
            rate_btc_usdt = await fetch_rate(session, f"{BINANCE_API_URL}?symbol=BTCUSDT", 'price', False, "Binance BTCUSDT")
            if rate_btc_usdt:
                if to_key == 'usdt':
                    rate = rate_btc_usdt
                elif to_key == 'eur':
                    rate_eur_usdt = await fetch_rate(session, f"{BINANCE_API_URL}?symbol=EURUSDT", 'price', False, "Binance EURUSDT") or EUR_TO_USDT_FALLBACK
                    rate = rate_btc_usdt / rate_eur_usdt
                elif to_key == 'uah':
                    rate = rate_btc_usdt * USDT_TO_UAH_FALLBACK
                redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
                return amount * rate, f"1 {from_code} \\= {escape_markdown_v2(str(rate))} {to_code} \\(Binance via USDT\\)"
        elif from_key == 'eth' and to_key in ['usdt', 'eur', 'uah']:
            rate_eth_usdt = await fetch_rate(session, f"{BINANCE_API_URL}?symbol=ETHUSDT", 'price', False, "Binance ETHUSDT")
            if rate_eth_usdt:
                if to_key == 'usdt':
                    rate = rate_eth_usdt
                elif to_key == 'eur':
                    rate_eur_usdt = await fetch_rate(session, f"{BINANCE_API_URL}?symbol=EURUSDT", 'price', False, "Binance EURUSDT") or EUR_TO_USDT_FALLBACK
                    rate = rate_eth_usdt / rate_eur_usdt
                elif to_key == 'uah':
                    rate = rate_eth_usdt * USDT_TO_UAH_FALLBACK
                redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
                return amount * rate, f"1 {from_code} \\= {escape_markdown_v2(str(rate))} {to_code} \\(Binance via USDT\\)"

        # Fallback для UAH
        if from_key == 'uah' and to_key == 'usdt':
            rate = UAH_TO_USDT_FALLBACK
            redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
            return amount * rate, f"1 {from_key.upper()} \\= {escape_markdown_v2(str(rate))} {to_key.upper()} \\(fallback\\)"
        elif from_key == 'usdt' and to_key == 'uah':
            rate = USDT_TO_UAH_FALLBACK
            redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
            return amount * rate, f"1 {from_key.upper()} \\= {escape_markdown_v2(str(rate))} {to_key.upper()} \\(fallback\\)"
        elif from_key == 'uah' and to_key == 'eur':
            rate_usdt = UAH_TO_USDT_FALLBACK
            rate_eur = await fetch_rate(session, f"{BINANCE_API_URL}?symbol=EURUSDT", 'price', True, "Binance EURUSDT") or EUR_TO_USDT_FALLBACK
            rate = rate_usdt / rate_eur
            redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
            return amount * rate, f"1 {from_key.upper()} \\= {escape_markdown_v2(str(rate))} {to_key.upper()} \\(Binance via USDT\\)"
        elif from_key == 'eur' and to_key == 'uah':
            rate_usdt = await fetch_rate(session, f"{BINANCE_API_URL}?symbol=EURUSDT", 'price', False, "Binance EURUSDT") or EUR_TO_USDT_FALLBACK
            rate = rate_usdt * USDT_TO_UAH_FALLBACK
            redis_client.setex(cache_key, CACHE_TIMEOUT, str(rate))
            return amount * rate, f"1 {from_key.upper()} \\= {escape_markdown_v2(str(rate))} {to_key.upper()} \\(Binance via USDT\\)"

    logger.warning(f"No rate found for {from_key} to {to_key}")
    return None, "Курс недоступен на данный момент"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    save_stats(user_id, "start")
    if context.args and context.args[0].startswith("ref_"):
        await handle_referral(update, context)

    keyboard = [
        [InlineKeyboardButton("💱 Конвертер", callback_data="converter"), InlineKeyboardButton("📈 Курсы", callback_data="price")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats"), InlineKeyboardButton("💎 Подписка", callback_data="subscribe")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="alert"), InlineKeyboardButton("👥 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton("📜 История", callback_data="history")]
    ]
    
    try:
        await update.effective_message.reply_text(
            f"👋 *Привет*\! Я {BOT_USERNAME} — твой помощник для конвертации валют\!\n"
            f"🔑 *Бесплатно*: {FREE_REQUEST_LIMIT} запросов в сутки\n"
            f"🌟 *Безлимит*: /subscribe за {SUBSCRIPTION_PRICE} USDT{AD_MESSAGE}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except TelegramError as e:
        logger.error(f"Failed to send start message to {user_id}: {e}")

async def currencies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    try:
        await update.effective_message.reply_text(
            f"💱 *Поддерживаемые валюты*:\n{', '.join(sorted(CURRENCIES.keys()))}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")]]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except TelegramError as e:
        logger.error(f"Failed to send currencies list: {e}")

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    args = context.args if update.message else None
    if not args or len(args) != 3 or not args[2].replace('.', '', 1).isdigit():
        keyboard = [
            [InlineKeyboardButton("🔔 USD → BTC", callback_data="alert_example_usd_btc")],
            [InlineKeyboardButton("🔔 EUR → UAH", callback_data="alert_example_eur_uah")],
            [InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ]
        text = "🔔 *Настрой уведомления*\! Формат: `/alert <валюта1> <валюта2> <курс>`\nПримеры ниже:"
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                await update.effective_message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        except TelegramError as e:
            logger.error(f"Failed to send alert instructions to {user_id}: {e}")
        return

    from_currency, to_currency, target_rate = args[0].lower(), args[1].lower(), float(args[2])
    if from_currency not in CURRENCIES or to_currency not in CURRENCIES:
        try:
            await update.effective_message.reply_text("❌ Ошибка: валюта не поддерживается", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as e:
            logger.error(f"Failed to send alert error to {user_id}: {e}")
        return

    try:
        alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
        alerts.append({"from": from_currency, "to": to_currency, "target": target_rate})
        redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(alerts))
        await update.effective_message.reply_text(
            f"🔔 *Уведомление*: {from_currency.upper()} → {to_currency.upper()} при курсе {escape_markdown_v2(str(target_rate))}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔔 Добавить ещё", callback_data="alert"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Failed to set alert for {user_id}: {e}")
        try:
            await update.effective_message.reply_text("❌ Ошибка при настройке уведомления", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send alert error to {user_id}: {te}")

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
        if user_id in ADMIN_IDS:
            text = (f"📊 *Админ\\-статистика*:\n"
                    f"👥 Пользователей: {len(stats.get('users', {}))}\n"
                    f"📈 Запросов: {stats.get('total_requests', 0)}\n"
                    f"💰 Доход: {escape_markdown_v2(str(stats.get('revenue', 0.0)))} USDT")
        else:
            text = f"📊 *Твоя статистика*:\n📈 Запросов сегодня: {stats.get('users', {}).get(user_id, {}).get('requests', 0)}"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed to send stats to {user_id}: {e}")
        try:
            await update.effective_message.reply_text("❌ Ошибка при получении статистики", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send stats error to {user_id}: {te}")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        if stats.get("subscriptions", {}).get(user_id):
            text = "💎 Ты уже подписан\\!"
            if update.callback_query:
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            return

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN},
                json={"asset": "USDT", "amount": str(SUBSCRIPTION_PRICE), "description": f"Подписка для {user_id}"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                result = await response.json()
                if result.get("ok"):
                    invoice_id = result["result"]["invoice_id"]
                    pay_url = result["result"]["pay_url"]
                    context.user_data[user_id] = {"invoice_id": invoice_id}
                    text = f"💎 Оплати *{SUBSCRIPTION_PRICE} USDT* для безлимита:"
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_PRICE} USDT", url=pay_url)],
                        [InlineKeyboardButton("🔙 Назад", callback_data="start")]
                    ])
                    if update.callback_query:
                        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
                    else:
                        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    error_msg = result.get('error', 'Неизвестно')
                    logger.error(f"Payment error for {user_id}: {error_msg}")
                    text = f"❌ Ошибка платежа: {escape_markdown_v2(error_msg)}"
                    if update.callback_query:
                        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
                    else:
                        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Subscribe error for {user_id}: {e}")
        text = "❌ Ошибка связи с платежной системой"
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send subscribe error to {user_id}: {te}")

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    try:
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
        text = f"👥 *Реф\\. ссылка*: `{ref_link}`\n👤 Приглашено: *{refs}*\n🌟 Бонусы скоро будут\\!"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Копировать", callback_data="copy_ref"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed to send referrals to {user_id}: {e}")
        try:
            await update.effective_message.reply_text("❌ Ошибка при получении рефералов", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send referrals error to {user_id}: {te}")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    try:
        history_data = json.loads(redis_client.get(f"history:{user_id}") or '[]')
        back_button = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
        if not history_data:
            text = "📜 *История пуста*\\."
        else:
            history_lines = []
            for entry in reversed(history_data):
                time_str = entry['time'].replace('-', '\\-')
                amount_str = escape_markdown_v2(str(entry['amount']))
                result_str = escape_markdown_v2(str(entry['result']))
                from_curr = escape_markdown_v2(entry['from'])
                to_curr = escape_markdown_v2(entry['to'])
                line = f"⏰ {time_str}: *{amount_str} {from_curr}* → *{result_str} {to_curr}*"
                history_lines.append(line)
            text = "📜 *История запросов*:\n" + "\n".join(history_lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back_button), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(back_button), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed to send history to {user_id}: {e}")
        try:
            await update.effective_message.reply_text("❌ Ошибка при получении истории", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send history error to {user_id}: {te}")

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if context.args and context.args[0].startswith("ref_"):
        referrer_id = context.args[0].replace("ref_", "")
        if referrer_id.isdigit() and referrer_id != user_id:
            try:
                referrals = json.loads(redis_client.get(f"referrals:{referrer_id}") or '[]')
                if user_id not in referrals:
                    referrals.append(user_id)
                    redis_client.setex(f"referrals:{referrer_id}", 30 * 24 * 60 * 60, json.dumps(referrals))
                    await update.effective_message.reply_text("👥 Спасибо за присоединение по реф\\. ссылке\\!", parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.error(f"Failed to handle referral for {user_id} from {referrer_id}: {e}")

async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is None:
        logger.debug("No user_data available in check_payment_job, skipping")
        return
    for user_id, data in list(context.user_data.items()):
        if "invoice_id" not in data:
            continue
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://pay.crypt.bot/api/getInvoices?invoice_ids={data['invoice_id']}",
                    headers={'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as response:
                    result = await response.json()
                    if result.get("ok") and result["result"]["items"] and result["result"]["items"][0]["status"] == "paid":
                        stats = json.loads(redis_client.get('stats') or '{}')
                        stats.setdefault("subscriptions", {})[user_id] = True
                        stats["revenue"] = stats.get("revenue", 0.0) + SUBSCRIPTION_PRICE
                        redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps(stats))
                        del context.user_data[user_id]
                        await context.bot.send_message(
                            user_id,
                            "💎 Оплата прошла\\! Безлимит активирован\\.",
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
        except Exception as e:
            logger.error(f"Payment check error for {user_id}: {e}")

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        if not stats.get("users"):
            return
        for user_id in stats.get("users", {}):
            alerts = json.loads(redis_client.get(f"alerts:{user_id}") or '[]')
            if not alerts:
                continue
            updated_alerts = []
            for alert in alerts:
                result, rate_info = await get_exchange_rate(alert["from"], alert["to"])
                if result and float(rate_info.split()[2]) <= alert["target"]:
                    from_code, to_code = CURRENCIES[alert["from"]]['code'], CURRENCIES[alert["to"]]['code']
                    await context.bot.send_message(
                        user_id,
                        f"🔔 *Уведомление*\! {from_code} → {to_code}: {escape_markdown_v2(str(float(rate_info.split()[2])))} \\(цель: {escape_markdown_v2(str(alert['target']))}\\)",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                else:
                    updated_alerts.append(alert)
            redis_client.setex(f"alerts:{user_id}", 30 * 24 * 60 * 60, json.dumps(updated_alerts))
    except Exception as e:
        logger.error(f"Error in check_alerts_job: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    user_id = str(update.effective_user.id)
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id)
        delay = 0 if is_subscribed else 5

        if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
            await update.effective_message.reply_text(f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}\!", parse_mode=ParseMode.MARKDOWN_V2)
            return

        can_proceed, remaining = check_limit(user_id)
        if not can_proceed:
            await update.effective_message.reply_text(f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан\\. /subscribe", parse_mode=ParseMode.MARKDOWN_V2)
            return

        context.user_data['last_request'] = time.time()
        text = update.effective_message.text.lower().split()
        amount = float(text[0]) if text[0].replace('.', '', 1).isdigit() else 1.0
        from_currency, to_currency = text[1 if amount != 1.0 else 0], text[2 if amount != 1.0 else 1]
        save_stats(user_id, f"{from_currency}_to_{to_currency}")
        result, rate_info = await get_exchange_rate(from_currency, to_currency, amount)
        if result is None:
            raise ValueError(rate_info)

        from_code, to_code = CURRENCIES[from_currency.lower()]['code'], CURRENCIES[to_currency.lower()]['code']
        precision = 8 if to_code in HIGH_PRECISION_CURRENCIES else 2
        await update.effective_message.reply_text(
            f"💰 *{escape_markdown_v2(str(amount))} {from_code}* \\= *{escape_markdown_v2(str(round(result, precision)))} {to_code}*\n"
            f"📈 {rate_info}\n🔄 Осталось: *{remaining}*{AD_MESSAGE}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                [InlineKeyboardButton("💱 Другая пара", callback_data="converter"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        save_history(user_id, from_code, to_code, amount, result)
    except (IndexError, ValueError) as e:
        try:
            await update.effective_message.reply_text(
                f"❌ Ошибка: {escape_markdown_v2(str(e) if isinstance(e, ValueError) else 'Неверный формат')}\nПример: `100\\.0 uah usdt`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💱 Попробовать снова", callback_data="converter")]]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except TelegramError as te:
            logger.error(f"Failed to send message error to {user_id}: {te}")
    except Exception as e:
        logger.error(f"Unexpected error in handle_message for {user_id}: {e}")
        try:
            await update.effective_message.reply_text("❌ Неизвестная ошибка, попробуй позже", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send unexpected error to {user_id}: {te}")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError as e:
        logger.error(f"Failed to answer callback query: {e}")
        return

    if not await enforce_subscription(update, context):
        return

    user_id = str(query.from_user.id)
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        is_subscribed = user_id in ADMIN_IDS or stats.get("subscriptions", {}).get(user_id)
        delay = 0 if is_subscribed else 5

        if 'last_request' in context.user_data and time.time() - context.user_data['last_request'] < delay:
            await query.edit_message_text(f"⏳ Подожди {delay} секунд{'у' if delay == 1 else ''}\!", parse_mode=ParseMode.MARKDOWN_V2)
            return

        can_proceed, remaining = check_limit(user_id)
        if not can_proceed:
            await query.edit_message_text(f"❌ Лимит {FREE_REQUEST_LIMIT} запросов исчерпан\\. /subscribe", parse_mode=ParseMode.MARKDOWN_V2)
            return

        context.user_data['last_request'] = time.time()
        action = query.data

        if action == "start":
            await start(update, context)
        elif action == "converter":
            await query.edit_message_text(
                "💱 *Выбери пару или введи вручную \\(например, '100\\.0 uah usdt'\\)*:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 USD → BTC", callback_data="convert:usd:btc"), InlineKeyboardButton("💶 EUR → UAH", callback_data="convert:eur:uah")],
                    [InlineKeyboardButton("₿ BTC → ETH", callback_data="convert:btc:eth"), InlineKeyboardButton("₴ UAH → USDT", callback_data="convert:uah:usdt")],
                    [InlineKeyboardButton("🔄 Ввести вручную", callback_data="manual_convert"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
                ]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        elif action.startswith("convert:"):
            _, from_currency, to_currency = action.split(":")
            result, rate_info = await get_exchange_rate(from_currency, to_currency)
            if result:
                from_code, to_code = CURRENCIES[from_currency.lower()]['code'], CURRENCIES[to_currency.lower()]['code']
                precision = 8 if to_code in HIGH_PRECISION_CURRENCIES else 2
                await query.edit_message_text(
                    f"💰 *1\\.0 {from_code}* \\= *{escape_markdown_v2(str(round(result, precision)))} {to_code}*\n"
                    f"📈 {rate_info}\n🔄 Осталось: *{remaining}*{AD_MESSAGE}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Ещё раз", callback_data=f"convert:{from_currency}:{to_currency}")],
                        [InlineKeyboardButton("💱 Другая пара", callback_data="converter"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
                    ]),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                save_history(user_id, from_code, to_code, 1.0, result)
            else:
                await query.edit_message_text(f"❌ Ошибка: {escape_markdown_v2(rate_info)}", parse_mode=ParseMode.MARKDOWN_V2)
        elif action == "manual_convert":
            await query.edit_message_text("💱 *Введи запрос вручную*: например, '100\\.0 uah usdt'", parse_mode=ParseMode.MARKDOWN_V2)
        elif action == "stats":
            await stats_handler(update, context)
        elif action == "subscribe":
            await subscribe(update, context)
        elif action == "alert":
            await alert(update, context)
        elif action == "referrals":
            await referrals(update, context)
        elif action == "history":
            await history(update, context)
        elif action == "copy_ref":
            ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
            refs = len(json.loads(redis_client.get(f"referrals:{user_id}") or '[]'))
            await query.edit_message_text(
                f"👥 *Реф\\. ссылка*: `{ref_link}`\n👤 Приглашено: *{refs}*\n🌟 Бонусы скоро будут\\!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Копировать", callback_data="copy_ref"), InlineKeyboardButton("🔙 Назад", callback_data="start")]
                ]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        elif action == "alert_example_usd_btc":
            await query.edit_message_text(
                "🔔 Пример: `/alert usd btc 0\\.000015` — уведомит, когда 1 USD \\= 0\\.000015 BTC",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        elif action == "alert_example_eur_uah":
            await query.edit_message_text(
                "🔔 Пример: `/alert eur uah 45\\.0` — уведомит, когда 1 EUR \\= 45\\.0 UAH",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        elif action == "price":
            await query.edit_message_text(
                "📈 *Выбери валюту для курса*:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("BTC", callback_data="convert:btc:usdt"), InlineKeyboardButton("ETH", callback_data="convert:eth:usdt")],
                    [InlineKeyboardButton("USD", callback_data="convert:usd:uah"), InlineKeyboardButton("EUR", callback_data="convert:eur:uah")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="start")]
                ]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        logger.error(f"Unexpected error in button handler for {user_id}: {e}")
        try:
            await query.edit_message_text("❌ Неизвестная ошибка, попробуй позже", parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as te:
            logger.error(f"Failed to send button error to {user_id}: {te}")

async def set_bot_commands(application: Application):
    try:
        await application.bot.set_my_commands([
            ("start", "Главное меню"),
            ("currencies", "Список валют"),
            ("alert", "Уведомления"),
            ("stats", "Статистика"),
            ("subscribe", "Подписка"),
            ("referrals", "Рефералы"),
            ("history", "История запросов")
        ])
        logger.info("Bot commands set successfully")
    except TelegramError as e:
        logger.error(f"Failed to set bot commands: {e}")

def main():
    try:
        logger.info("Initializing application...")
        app = Application.builder().token(TELEGRAM_TOKEN).build()

        logger.info("Adding handlers...")
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("currencies", currencies))
        app.add_handler(CommandHandler("alert", alert))
        app.add_handler(CommandHandler("stats", stats_handler))
        app.add_handler(CommandHandler("subscribe", subscribe))
        app.add_handler(CommandHandler("referrals", referrals))
        app.add_handler(CommandHandler("history", history))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(CallbackQueryHandler(button))

        if app.job_queue is None:
            logger.critical("JobQueue is not initialized! Install python-telegram-bot with [job-queue] support.")
            return

        logger.info("Scheduling jobs...")
        app.job_queue.run_repeating(check_payment_job, interval=60, name="check_payment")
        app.job_queue.run_repeating(check_alerts_job, interval=60, name="check_alerts")

        logger.info("Initializing bot...")
        asyncio.get_event_loop().run_until_complete(app.initialize())

        logger.info("Setting bot commands...")
        asyncio.get_event_loop().run_until_complete(set_bot_commands(app))

        if not redis_client.exists('stats'):
            logger.info("Initializing stats in Redis...")
            redis_client.setex('stats', 30 * 24 * 60 * 60, json.dumps({"users": {}, "total_requests": 0, "request_types": {}, "subscriptions": {}, "revenue": 0.0}))

        logger.info("Bot starting polling...")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, timeout=30)
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}")
        raise

if __name__ == "__main__":
    main()
