import os
import json
import time
import logging
from flask import Flask, render_template_string, request, session, redirect, url_for
from werkzeug.security import check_password_hash, generate_password_hash
import redis

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'trust20242024')  # Пароль из переменной окружения
ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD) if os.getenv('ADMIN_PASSWORD') else generate_password_hash('trust20242024')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs="none")

# Инициализация STATS в Redis, если пусто
def init_stats():
    if not redis_client.exists('stats'):
        default_stats = {
            "users": {},
            "total_requests": 0,
            "request_types": {},
            "subscriptions": {},
            "revenue": 0.0
        }
        redis_client.set('stats', json.dumps(default_stats))

def get_stats():
    """Получает статистику из Redis"""
    try:
        stats = json.loads(redis_client.get('stats') or '{}')
        total_users = len(stats.get("users", {}))
        total_requests = stats.get("total_requests", 0)
        popular_requests = sorted(stats.get("request_types", {}).items(), key=lambda x: x[1], reverse=True)[:5]
        subscriptions = len(stats.get("subscriptions", {}))
        revenue = stats.get("revenue", 0.0)
        return total_users, total_requests, popular_requests, subscriptions, revenue
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return 0, 0, [], 0, 0.0

# Flask приложение
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'supersecretkey')  # Укажи в .env для безопасности

@app.route('/', methods=['GET', 'POST'])
def dashboard():
    """Веб-дашборд с сессиями"""
    if 'authenticated' not in session:
        if request.method == 'POST':
            password = request.form.get('password')
            if check_password_hash(ADMIN_PASSWORD_HASH, password):
                session['authenticated'] = True
                return redirect(url_for('dashboard'))
            return "Неверный пароль!", 403
        html = """
        <form method="post">
            <input type="password" name="password" placeholder="Введите пароль">
            <input type="submit" value="Войти">
        </form>
        """
        return render_template_string(html)

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
            <a href="/logout">Выйти</a>
        </body>
    </html>
    """
    return render_template_string(html, total_users=total_users, total_requests=total_requests, 
                                 popular_requests=popular_requests, subscriptions=subscriptions, 
                                 revenue=revenue)

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('dashboard'))

if __name__ == "__main__":
    init_stats()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
