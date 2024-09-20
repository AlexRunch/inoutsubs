import json
import logging
import boto3
import requests

# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Конфигурация Telegram API
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'
TELEGRAM_API_URL = f'https://api.telegram.org/bot{BOT_TOKEN}/'

# Конфигурация DynamoDB
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers')

def send_message(chat_id, text):
    url = f"{TELEGRAM_API_URL}sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    response = requests.post(url, json=data)
    if response.status_code != 200:
        logger.error(f"Ошибка отправки сообщения: {response.text}")
    return response.json()

def verify_bot_admin(channel_name):
    # Эту функцию нужно будет реализовать отдельно,
    # возможно, используя другой подход или сохраняя информацию в базе данных
    return True  # Заглушка

def process_message(event):
    chat_id = event['message']['chat']['id']
    text = event['message'].get('text', '')

    if text == '/start':
        welcome_message = ("Привет! Я бот для отслеживания изменений подписчиков вашего канала.\n\n"
                           "Чтобы подключить канал, выполните следующие шаги:\n"
                           "1. Добавьте меня в качестве администратора в ваш канал\n"
                           "2. Напишите мне @username вашего канала\n"
                           "3. После успешной проверки, напишите свою электронную почту\n\n"
                           "По всем вопросам обращайтесь к @alex_favin")
        send_message(chat_id, welcome_message)
    elif text.startswith('@'):
        is_admin = verify_bot_admin(text)
        if is_admin:
            send_message(chat_id, f"Канал {text} успешно проверен. Теперь напишите вашу электронную почту.")
        else:
            send_message(chat_id, f"Ошибка: Бот не является администратором канала {text}. "
                                  f"Сначала добавьте бота как администратора в ваш канал.")
    elif '@' in text and '.' in text:  # Простая проверка на email
        # Здесь добавьте логику сохранения email в базу данных
        send_message(chat_id, f"Email {text} сохранен. Вы будете получать ежедневные обновления на этот адрес.")
    else:
        send_message(chat_id, "Извините, я не понимаю эту команду.")

def lambda_handler(event, context):
    try:
        logger.info(f"Получено событие: {event}")
        
        if 'body' not in event:
            return {'statusCode': 400, 'body': json.dumps('Неверный формат запроса')}
        
        body = json.loads(event['body'])
        logger.info(f"Тело запроса: {body}")
        
        if 'message' in body:
            process_message(body)
        
        return {'statusCode': 200, 'body': json.dumps('OK')}
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps('Внутренняя ошибка сервера')}
