import requests
import json
import time
import os
import logging
from flask import Flask, render_template_string, request
from werkzeug.security import check_password_hash, generate_password_hash

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
ADMIN_PASSWORD_HASH = generate_password_hash('trust20242024')  # Пароль для дашборда (замени)

# Глобальные переменные (общие с Telegram-ботом)
STATS = {
    "users": {},
    "total_requests": 0,
    "request_types": {},
    "subscriptions": {},
    "revenue": 0.0
}

def save_stats(user_id, request_type):
    """Сохраняет статистику запросов"""
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

def get_stats():
    """Возвращает статистику для дашборда"""
    total_users = len(STATS["users"])
    total_requests = STATS["total_requests"]
    popular_requests = sorted(STATS["request_types"].items(), key=lambda x: x[1], reverse=True)[:5]
    subscriptions = len(STATS["subscriptions"])
    revenue = STATS["revenue"]
    return total_users, total_requests, popular_requests, subscriptions, revenue

# Flask приложение для дашборда
app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def dashboard():
    """Веб-дашборд с паролем"""
    if request.method == 'POST':
        password = request.form.get('password')
        if not check_password_hash(ADMIN_PASSWORD_HASH, password):
            return "Неверный пароль!", 403
    
    total_users, total_requests, popular_requests, subscriptions, revenue = get_stats()
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

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
