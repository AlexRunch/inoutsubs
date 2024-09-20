import json
import boto3
import requests
import logging
from telethon import TelegramClient, events
from telethon.tl.types import ChannelParticipantsAdmins
from botocore.exceptions import ClientError
from telethon.sessions import StringSession
import asyncio

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация S3 и DynamoDB
S3_CLIENT = boto3.client('s3')
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers')

# Конфигурация SES
SES_CLIENT = boto3.client('ses', region_name='eu-north-1')
ADMIN_EMAIL_HIDDEN_COPY = 'mihailov.org@gmail.com'

# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def send_message(chat_id, text, buttons=None):
    try:
        url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
        data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
        if buttons:
            data['reply_markup'] = buttons
        response = requests.post(url, json=data)
        response.raise_for_status()
        logger.info(f"Сообщение отправлено успешно: {response.json()}")
    except requests.RequestException as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel = await client.get_entity(channel_name)
        admins = await client.get_participants(channel, filter=ChannelParticipantsAdmins)
        return any(admin.id == user_id for admin in admins)
    except Exception as e:
        logger.error(f"Ошибка проверки прав администратора: {e}")
        return False

async def get_subscribers_list(client, channel):
    try:
        channel_entity = await client.get_entity(channel)
        participants = await client.get_participants(channel_entity)
        return {str(p.id): f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})' for p in participants}
    except Exception as e:
        logger.error(f"Ошибка получения списка подписчиков: {e}")
        return {}

def send_email(channel_name, admin_email, subscriber_count, subscriber_list):
    email_subject = f'Подключение канала {channel_name}'
    email_body = (f'Канал {channel_name} успешно подключен.\n'
                  f'Количество подписчиков: {subscriber_count}\n'
                  f'Список подписчиков:\n{subscriber_list}')
    
    try:
        SES_CLIENT.send_email(
            Source='mihailov.org@gmail.com',
            Destination={'ToAddresses': [admin_email]},
            Message={
                'Subject': {'Data': email_subject},
                'Body': {'Text': {'Data': email_body}}
            },
            BccAddresses=[ADMIN_EMAIL_HIDDEN_COPY]
        )
    except ClientError as e:
        logger.error(f"Ошибка отправки email через SES: {e}")

def save_channel_to_dynamodb(channel_name, user_id):
    try:
        TABLE.put_item(Item={'channel_id': channel_name, 'user_id': str(user_id)})
    except ClientError as e:
        logger.error(f"Ошибка сохранения данных в DynamoDB: {e}")

def stop_updates(channel_name):
    try:
        TABLE.update_item(
            Key={'channel_id': channel_name},
            UpdateExpression="SET send_updates = :val",
            ExpressionAttributeValues={':val': False}
        )
        logger.info(f"Обновления для канала {channel_name} остановлены")
    except ClientError as e:
        logger.error(f"Ошибка остановки обновлений в DynamoDB: {e}")

async def process_channel_connection(client, chat_id, user_id, channel_name):
    is_admin = await verify_channel_admin(client, user_id, channel_name)
    if is_admin:
        send_message(chat_id, f"Вы являетесь администратором канала {channel_name}. Канал будет подключен.")
        
        subscribers = await get_subscribers_list(client, channel_name)
        subscriber_count = len(subscribers)
        subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])

        save_channel_to_dynamodb(channel_name, user_id)
        send_email(channel_name, 'admin@example.com', subscriber_count, subscriber_list)
    else:
        send_message(chat_id, f"Ошибка: Вы не являетесь администратором канала {channel_name}. "
                              f"Убедитесь, что бот добавлен в канал и что у вас есть права администратора.")

async def async_lambda_handler(event, context):
    chat_id = None
    
    try:
        logger.info(f"Получено событие: {event}")
        if 'body' not in event:
            raise ValueError("'body' не найден в event")
        
        body = json.loads(event['body'])
        logger.info(f"Тело запроса: {body}")
        
        if 'message' in body:
            message = body['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            text = message.get('text', '')

            if text == '/start':
                instructions = ("Привет! Я помогу вам подключить канал для получения статистики.\n"
                                "Чтобы начать, нажмите кнопку 'Проверить канал' и введите название вашего канала.")
                buttons = {
                    'inline_keyboard': [
                        [{'text': 'Проверить канал', 'callback_data': 'check_channel'}],
                        [{'text': 'Стоп', 'callback_data': 'stop_updates'}]
                    ]
                }
                send_message(chat_id, instructions, json.dumps(buttons))
                return {'statusCode': 200, 'body': json.dumps('Инструкции отправлены')}

            if text and text.startswith('@'):
                async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
                    await client.start(bot_token=BOT_TOKEN)
                    await process_channel_connection(client, chat_id, user_id, text)

        elif 'callback_query' in body:
            callback_query = body['callback_query']
            chat_id = callback_query['message']['chat']['id']
            callback_data = callback_query['data']
            if callback_data == 'check_channel':
                send_message(chat_id, "Введите название канала для проверки.")
                return {'statusCode': 200, 'body': json.dumps('Запрошено название канала')}
            elif callback_data == 'stop_updates':
                # Здесь нужно добавить логику для определения канала пользователя
                # Предположим, что у нас есть функция get_user_channel(user_id)
                channel_name = get_user_channel(callback_query['from']['id'])
                if channel_name:
                    stop_updates(channel_name)
                    send_message(chat_id, f"Обновления для канала {channel_name} остановлены.")
                else:
                    send_message(chat_id, "У вас нет подключенных каналов.")
                return {'statusCode': 200, 'body': json.dumps('Обработан запрос на остановку обновлений')}

        return {'statusCode': 200, 'body': json.dumps('Сообщение обработано')}

    except Exception as e:
        logger.error(f"Произошла ошибка: {e}")
        if chat_id:
            send_message(chat_id, f"Произошла ошибка: {str(e)}")
        return {'statusCode': 400, 'body': json.dumps(f'Произошла ошибка: {str(e)}')}

def lambda_handler(event, context):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(async_lambda_handler(event, context))

# Исправления:
# 1. Добавлена проверка наличия 'body' в event перед его использованием
# 2. Улучшена обработка ошибок в async_lambda_handler
# 3. Добавлено больше логирования для отслеживания процесса выполнения
# 4. Оптимизирована структура кода для более эффективной обработки различных типов событий
# 5. Добавлена кнопка "Стоп" и обработка команды остановки обновлений
