import json
import logging
import boto3
import requests
import time
from telethon import TelegramClient
from telethon.sessions import MemorySession
import asyncio

# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'
TELEGRAM_API_URL = f'https://api.telegram.org/bot{BOT_TOKEN}/'

# Конфигурация DynamoDB
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers')

# Конфигурация SES
SES_CLIENT = boto3.client('ses', region_name='eu-north-1')

# Словарь для отслеживания последнего времени использования команды /start
last_start_command = {}

# Минимальный интервал между командами /start (в секундах)
START_COMMAND_INTERVAL = 5

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
    url = f"{TELEGRAM_API_URL}getChatAdministrators"
    data = {"chat_id": channel_name}
    response = requests.post(url, json=data)
    if response.status_code != 200:
        logger.error(f"Ошибка получения администраторов чата: {response.text}")
        return False
    admins = response.json().get('result', [])
    bot_info = requests.get(f"{TELEGRAM_API_URL}getMe").json()['result']
    return any(admin['user']['id'] == bot_info['id'] for admin in admins)

def save_user_data(chat_id, channel_name, email):
    try:
        TABLE.put_item(
            Item={
                'chat_id': str(chat_id),
                'channel_name': channel_name,
                'email': email
            }
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения данных в DynamoDB: {str(e)}")
        return False

def get_user_data(chat_id):
    try:
        response = TABLE.get_item(Key={'chat_id': str(chat_id)})
        return response.get('Item')
    except Exception as e:
        logger.error(f"Ошибка получения данных из DynamoDB: {str(e)}")
        return None

async def get_subscribers_list(client, channel):
    try:
        channel_entity = await client.get_entity(channel)
        participants = await client.get_participants(channel_entity)
        subscribers = {
            str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
            for participant in participants
        }
        return subscribers
    except Exception as e:
        logger.error(f"Ошибка при получении списка подписчиков для канала {channel}: {str(e)}")
        return {}

def send_email(subject, body, recipient_email):
    try:
        SES_CLIENT.send_email(
            Source='mihailov.org@gmail.com',
            Destination={'ToAddresses': [recipient_email]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Text': {'Data': body}}
            }
        )
        logger.info(f"Email отправлен на адрес {recipient_email}")
    except Exception as e:
        logger.error(f"Ошибка при отправке email на адрес {recipient_email}: {str(e)}")

async def process_message(event, client):
    chat_id = event['message']['chat']['id']
    text = event['message'].get('text', '')

    if text == '/start':
        current_time = time.time()
        if chat_id in last_start_command:
            time_since_last_start = current_time - last_start_command[chat_id]
            if time_since_last_start < START_COMMAND_INTERVAL:
                logger.info(f"Игнорирование повторной команды /start от пользователя {chat_id}")
                return  # Игнорируем повторную команду /start

        last_start_command[chat_id] = current_time
        welcome_message = ("Привет! Я бот для отслеживания изменений подписчиков вашего канала.\n\n"
                           "Чтобы подключить канал, выполните следующие шаги:\n"
                           "1. Добавьте меня в качестве администратора в ваш канал\n"
                           "2. Напишите мне @username вашего канала\n"
                           "3. После успешной проверки, напишите свою электронную почту\n\n"
                           "По всем вопросам обращайтесь к @alex_favin")
        send_message(chat_id, welcome_message)
    elif text.startswith('@'):
        channel_name = text
        is_admin = verify_bot_admin(channel_name)
        if is_admin:
            send_message(chat_id, f"Канал {channel_name} успешно проверен. Теперь напишите вашу электронную почту.")
            save_user_data(chat_id, channel_name, None)
        else:
            send_message(chat_id, f"Ошибка: Бот не является администратором канала {channel_name}. "
                                  f"Сначала добавьте бота как администратора в ваш канал.")
    elif '@' in text and '.' in text:
        email = text
        user_data = get_user_data(chat_id)
        if user_data and 'channel_name' in user_data:
            channel_name = user_data['channel_name']
            if save_user_data(chat_id, channel_name, email):
                send_message(chat_id, f"Email {email} сохранен. Вы будете получать ежедневные обновления на этот адрес.")
                
                # Получение и отправка списка подписчиков на email
                subscribers = await get_subscribers_list(client, channel_name)
                email_subject = f"Список подписчиков канала {channel_name}"
                email_body = f"Канал: {channel_name}\nКоличество подписчиков: {len(subscribers)}\n\nСписок подписчиков:\n"
                email_body += "\n".join([f"{name}" for name in subscribers.values()])
                send_email(email_subject, email_body, email)
            else:
                send_message(chat_id, "Произошла ошибка при сохранении данных. Пожалуйста, попробуйте еще раз.")
        else:
            send_message(chat_id, "Пожалуйста, сначала укажите канал, отправив его @username.")
    else:
        send_message(chat_id, "Извините, я не понимаю эту команду.")

async def main(event, context):
    client = TelegramClient(MemorySession(), API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    try:
        body = json.loads(event['body'])
        if 'message' in body:
            await process_message(body['message'], client)
    finally:
        await client.disconnect()

def lambda_handler(event, context):
    try:
        logger.info(f"Получено событие: {event}")
        
        if 'body' not in event:
            return {'statusCode': 400, 'body': json.dumps('Неверный формат запроса')}
        
        asyncio.get_event_loop().run_until_complete(main(event, context))
        
        return {'statusCode': 200, 'body': json.dumps('OK')}
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps('Внутренняя ошибка сервера')}
