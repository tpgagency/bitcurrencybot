import logging
import time
import requests
import json
import redis
import telebot
from telebot import types

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol="
CURRENCY_PAIRS = {"BTCUSDT": "Bitcoin", "ETHUSDT": "Ethereum", "USDTUSD": "Tether"}
CACHE_TIME = 60  # Кэширование курсов в секундах

bot = telebot.TeleBot(TOKEN)
cache = redis.Redis(host='localhost', port=6379, db=0)

def get_price(symbol):
    try:
        cached_price = cache.get(symbol)
        if cached_price:
            return float(cached_price)
        
        response = requests.get(BINANCE_API_URL + symbol)
        if response.status_code == 200:
            price = float(response.json()["price"])
            cache.set(symbol, price, ex=CACHE_TIME)
            return price
        else:
            logging.error(f"Ошибка запроса к Binance: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Ошибка при получении цены: {e}")
        return None

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for pair, name in CURRENCY_PAIRS.items():
        markup.add(types.KeyboardButton(name))
    bot.send_message(message.chat.id, "Выберите криптовалюту для просмотра курса:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text in CURRENCY_PAIRS.values())
def send_price(message):
    symbol = [key for key, value in CURRENCY_PAIRS.items() if value == message.text][0]
    price = get_price(symbol)
    if price:
        bot.send_message(message.chat.id, f"Курс {message.text}: {price} USD")
    else:
        bot.send_message(message.chat.id, "Ошибка получения курса. Попробуйте позже.")

if __name__ == "__main__":
    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            logging.error(f"Ошибка в работе бота: {e}")
            time.sleep(5)
