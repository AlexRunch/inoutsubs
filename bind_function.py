import logging
import boto3
import time
import os
import asyncio
import json
from datetime import datetime
from telethon import TelegramClient, events, Button, types
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from botocore.exceptions import ClientError
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# Настройка логгера
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация S3 и DynamoDB
S3_CLIENT = boto3.client('s3')
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers-new')
USERS_TABLE = DYNAMODB.Table('my-telegram-users')  # Таблица для хранения пользователей

# Конфигурация Brevo
BREVO_API_KEY = os.getenv('BREVO_API_KEY')  # Получение API ключа из переменных окружения

if not BREVO_API_KEY:
    logger.error("BREVO_API_KEY не установлен. Проверьте переменные окружения.")
    raise ValueError("BREVO_API_KEY не установлен. Проверьте переменные окружения.")

# Путь к файлу сессии
SESSION_FILE = '/tmp/bot_session.session'

# ID пользователя, который может отправлять сообщения через broadcast
BROADCAST_USER_ID = 177520168

MAX_RETRIES = 3

async def connect_with_retry(client, max_retries=MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            await client.connect()
            return
        except FloodWaitError as e:
            if attempt == max_retries - 1:
                raise
            wait_time = e.seconds
            logger.info(f"Ожидание {wait_time} секунд перед повторной попыткой...")
            time.sleep(wait_time)

async def initialize_client():
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    if not os.path.exists(SESSION_FILE):
        await connect_with_retry(client)
        await client.sign_in(bot_token=BOT_TOKEN)
        await client.session.save()
    else:
        await client.start(bot_token=BOT_TOKEN)
    return client

async def send_message(client, chat_id, text, buttons=None):
    try:
        if buttons:
            await client.send_message(chat_id, text, buttons=buttons)
        else:
            await client.send_message(chat_id, text)
        logger.info(f"Сообщение отправлено успешно в чат {chat_id}")
        await asyncio.sleep(1)  # Добавляем задержку в 1 секунду после отправки сообщения
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
        raise

async def show_typing_animation(client, chat_id, duration=3):
    try:
        await client(SetTypingRequest(peer=chat_id, action=SendMessageTypingAction()))
        await asyncio.sleep(duration)
    except Exception as e:
        logger.error(f"Ошибка при отображении анимации набора текста: {e}")

async def is_user_admin(client, chat_id, user_id):
    try:
        logger.info(f"Проверка прав администратора для пользователя {user_id} в чате {chat_id}")
        chat = await client.get_entity(chat_id)
        if isinstance(chat, types.User):
            logger.warning(f"Чат {chat_id} является личной перепиской, а не каналом или группой")
            return False
        admins = await client(GetParticipantsRequest(
            chat, filter=ChannelParticipantsAdmins(), offset=0, limit=100, hash=0
        ))
        return any(admin.id == user_id for admin in admins.participants)
    except Exception as e:
        logger.error(f"Ошибка проверки прав администратора: {str(e)}")
        return False

async def get_subscribers_list(client, channel):
    try:
        channel_entity = await client.get_entity(channel)
        participants = await client.get_participants(channel_entity)
        return {str(p.id): f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})' for p in participants}
    except Exception as e:
        logger.error(f"Ошибка получения списка подписчиков: {e}")
        raise

async def send_channel_connected_message(client, chat_id, channel_name, subscriber_count, subscriber_list):
    message = (
        f"Хей-хей! Мы успешно подключили канал и теперь каждый день будем присылать информацию о том, "
        f"кто подписался, а кто отписался от канала.\n\n"
        f"На сегодняшний день у тебя: {subscriber_count}\n\n"
        f"Вот их список:\n"
    )
    
    for user_id, user_info in subscriber_list.items():
        name, subscriber_username = user_info.split(' (@')
        subscriber_username = subscriber_username.rstrip(')')
        message += f"🎉 {name} (@{subscriber_username}) — https://t.me/{subscriber_username}\n"
    
    try:
        await send_message(client, chat_id, message)
        logger.info(f"Сообщение о подключении канала успешно отправлено в чат {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения о подключении канала: {e}")
        raise

def send_email(channel_name, admin_email, subscriber_count, subscriber_list):
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))
    subject = f"Информация о канале {channel_name}"
    html_content = f"<html><body><h1>Информация о канале {channel_name}</h1><p>Количество подписчиков: {subscriber_count}</p><pre>{subscriber_list}</pre></body></html>"
    sender = {"name": "Your Bot", "email": "your-email@example.com"}
    to = [{"email": admin_email}]
    reply_to = {"email": "your-email@example.com"}
    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(to=to, sender=sender, subject=subject, html_content=html_content, reply_to=reply_to)

    try:
        api_response = api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Email успешно отправлен на адрес {admin_email}")
        logger.info(f"API Response: {api_response}")
    except ApiException as e:
        logger.error(f"Ошибка при отправке email на адрес {admin_email}: {e}")

def save_channel_to_dynamodb(channel_name, admin_user_id, subscribers, email, admin_name):
    try:
        date = datetime.now().strftime("%Y-%m-%d")
        item = {
            'channel_id': channel_name,
            'date': date,
            'admin_user_id': str(admin_user_id),
            'admin_name': admin_name,
            'email': email,
            'subscribers': json.dumps(subscribers, ensure_ascii=False),
            'new_subscribers': json.dumps([]),
            'unsubscribed': json.dumps([]),
            'total_subs': len(subscribers),
            'last_update': date
        }
        TABLE.put_item(Item=item)
        logger.info(f"Канал {channel_name} успешно сохранен в DynamoDB")
    except Exception as e:
        logger.error(f"Ошибка при сохранении канала {channel_name} в DynamoDB: {e}")

async def handle_start_command(client, chat_id, user_id, user_name):
    welcome_message = (
        "Привет! Я бот для отслеживания подписчиков в Telegram-каналах. "
        "Чтобы начать, используйте команду /bind для привязки канала."
    )
    await send_message(client, chat_id, welcome_message)
    logger.info(f"Отправлено приветственное сообщение пользователю {user_id}")

async def handle_bind_command(client, chat_id, user_id, text):
    # Извлекаем имя канала из команды
    channel_name = text.split(' ', 1)[1] if len(text.split(' ')) > 1 else None
    if not channel_name:
        await send_message(client, chat_id, "Пожалуйста, укажите имя канала после команды /bind")
        return

    try:
        # Проверяем, является ли пользователь администратором канала
        channel = await client.get_entity(channel_name)
        is_admin = await is_user_admin(client, channel, user_id)
        
        if not is_admin:
            await send_message(client, chat_id, "Вы должны быть администратором канала для его привязки.")
            return

        # Получаем список подписчиков
        subscribers = await get_subscribers_list(client, channel)
        
        # Сохраняем информацию о канале в DynamoDB
        admin_info = await client.get_entity(user_id)
        admin_name = f"{admin_info.first_name} {admin_info.last_name}" if admin_info.last_name else admin_info.first_name
        save_channel_to_dynamodb(channel_name, user_id, subscribers, "admin@example.com", admin_name)

        # Отправляем сообщение о успешной привязке
        await send_channel_connected_message(client, chat_id, channel_name, len(subscribers), subscribers)

    except Exception as e:
        logger.error(f"Ошибка при обработке команды /bind: {e}")
        await send_message(client, chat_id, f"Произошла ошибка при привязке канала: {str(e)}")

async def handle_broadcast_command(client, text):
    # Извлекаем сообщение для рассылки
    broadcast_message = text.split(' ', 1)[1] if len(text.split(' ')) > 1 else None
    if not broadcast_message:
        logger.error("Сообщение для рассылки не указано")
        return

    try:
        await broadcast_message_to_all_users(client, broadcast_message)
    except Exception as e:
        logger.error(f"Ошибка при выполнении рассылки: {e}")

async def process_message(client, chat_id, text, user_id, user_name):
    logger.info(f"Обработка сообщения: chat_id={chat_id}, user_id={user_id}, text={text}")
    
    if text.startswith('/start'):
        await handle_start_command(client, chat_id, user_id, user_name)
    elif text.startswith('/bind'):
        is_admin = await is_user_admin(client, chat_id, user_id)
        if is_admin:
            await handle_bind_command(client, chat_id, user_id, text)
        else:
            await client.send_message(chat_id, "У вас нет прав администратора для выполнения этой команды.")
    elif text.startswith('/broadcast') and user_id == BROADCAST_USER_ID:
        await handle_broadcast_command(client, text)
    else:
        await client.send_message(chat_id, "Неизвестная команда. Пожалуйста, используйте /start для начала работы.")

def get_channel_from_dynamodb(admin_user_id):
    try:
        response = TABLE.query(
            IndexName='AdminUserIndex',
            KeyConditionExpression='admin_user_id = :admin_id',
            ExpressionAttributeValues={':admin_id': str(admin_user_id)},
            Limit=1
        )
        items = response.get('Items', [])
        if items:
            return items[0]['channel_id']
        return None
    except Exception as e:
        logger.error(f"Ошибка получения канала из DynamoDB: {e}")
        return None

async def broadcast_message_to_all_users(client, message):
    try:
        response = USERS_TABLE.scan()
        users = response.get('Items', [])
        for user in users:
            if 'user_id' in user:
                chat_id = int(user['user_id'])
                try:
                    entity = await client.get_input_entity(chat_id)
                    await send_message(client, entity, message)
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения пользователю {chat_id}: {e}")
                await asyncio.sleep(1)  # Добавляем задержку в 1 секунду между отправками сообщений
        logger.info("Сообщение успешно отправлено всем пользователям.")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения всем пользователям: {e}")
        raise

async def main(event):
    logger.info("Начало обработки события")
    logger.info(f"Получено событие: {event}")
    try:
        client = await initialize_client()
        logger.info("Успешное подключение к Telegram API")
        
        # Извлечение данных из события
        if isinstance(event, dict):
            if 'body' in event:
                try:
                    body = json.loads(event['body'])
                except json.JSONDecodeError:
                    logger.error(f"Невозможно декодировать JSON из body: {event['body']}")
                    return
            elif 'message' in event:
                body = event
            else:
                logger.warning(f"Неожиданный формат события. Ключи: {event.keys()}")
                logger.info(f"Содержимое события: {event}")
                return
        else:
            logger.error(f"Событие не является словарем. Тип: {type(event)}")
            return

        if 'message' not in body:
            logger.warning(f"В теле события отсутствует ключ 'message'. Ключи body: {body.keys()}")
            logger.info(f"Содержимое body: {body}")
            return

        message = body['message']
        chat_id = message['chat']['id']
        user_id = message['from']['id']
        text = message.get('text', '')
        user_name = message['from'].get('username', '')

        logger.info(f"Обработка сообщения: chat_id={chat_id}, user_id={user_id}, user_name={user_name}, text={text}")

        await process_message(client, chat_id, text, user_id, user_name)

    except Exception as e:
        logger.error(f"Ошибка при обработке события: {str(e)}")
        raise
    finally:
        if 'client' in locals():
            await client.disconnect()

def lambda_handler(event, context):
    logger.info(f"Получено событие Lambda: {event}")
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(event))
    except Exception as e:
        logger.error(f"Ошибка в lambda_handler: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
    return {'statusCode': 200, 'body': json.dumps('OK')}
