import json
import logging
import boto3
import time
import os
import asyncio
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from botocore.exceptions import ClientError
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction

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

# Конфигурация SES
SES_CLIENT = boto3.client('ses', region_name='eu-north-1')
ADMIN_EMAIL_HIDDEN_COPY = 'mihailov.org@gmail.com'

# Путь к файлу сессии
SESSION_FILE = '/tmp/bot_session.session'

async def connect_with_retry(client, max_retries=5):
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

async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel = await client.get_entity(channel_name)
        admins = await client(GetParticipantsRequest(
            channel, filter=ChannelParticipantsAdmins(), offset=0, limit=100, hash=0))
        return any(admin.id == user_id for admin in admins.users)
    except Exception as e:
        logger.error(f"Ошибка проверки прав администратора: {e}")
        raise

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
        name, username = user_info.split(' (@')
        username = username.rstrip(')')
        message += f"🎉 {name} (@{username}) — https://t.me/{username}\n"
    
    try:
        await send_message(client, chat_id, message)
        logger.info(f"Сообщение о подключении канала успешно отправлено в чат {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения о подключении канала: {e}")
        raise

def send_email(channel_name, admin_email, subscriber_count, subscriber_list, username):
    # username здесь подразумевает имя пользователя (юзернейм) администратора канала.
    # Этот параметр используется для идентификации администратора в Telegram.
    # Он может быть использован в теле письма или для других целей,
    # связанных с идентификацией администратора канала.
    # Письмо для админа канала
    admin_email_subject = f'Подключение канала {channel_name}'
    admin_email_body = (f'Канал {channel_name} успешно подключен.\n'
                        f'Количество подписчиков: {subscriber_count}\n'
                        f'Список подписчиков:\n')
    
    for user_id, user_info in subscriber_list.items():
        name, username = user_info.split(' (@')
        username = username.rstrip(')')
        admin_email_body += f"🎉 {name} (@{username}) — https://t.me/{username}\n"
    
    # Письмо для 4mihailov@gmail.com
    owner_email_subject = f'Подключен новый канал {channel_name}'
    owner_email_body = (f'Название канала: {channel_name}\n'
                        f'Админ, который его подключил: @{username}\n'
                        f'Количество подписчиков канала: {subscriber_count}')
    
    try:
        # Отправка письма админу канала
        SES_CLIENT.send_email(
            Source='mihailov.org@gmail.com',
            Destination={
                'ToAddresses': [admin_email],
                'BccAddresses': [ADMIN_EMAIL_HIDDEN_COPY]
            },
            Message={
                'Subject': {'Data': admin_email_subject},
                'Body': {'Text': {'Data': admin_email_body}}
            }
        )
        
        # Отправка письма на 4mihailov@gmail.com
        SES_CLIENT.send_email(
            Source='mihailov.org@gmail.com',
            Destination={
                'ToAddresses': ['4mihailov@gmail.com']
            },
            Message={
                'Subject': {'Data': owner_email_subject},
                'Body': {'Text': {'Data': owner_email_body}}
            }
        )
        
        time.sleep(1)  # Добавляем задержку в 1 секунду после отправки email
        logger.info(f"Email успешно отправлен на адреса {admin_email} и 4mihailov@gmail.com")
    except ClientError as e:
        logger.error(f"Ошибка отправки email через SES: {e}")
        raise

def save_channel_to_dynamodb(channel_id, admin_user_id, subscribers):
    current_date = datetime.now().strftime("%Y-%m-%d")
    try:
        TABLE.put_item(
            Item={
                'channel_id': channel_id,
                'date': current_date,
                'admin_user_id': str(admin_user_id),
                'subscribers': subscribers,
                'new_subscribers': [],
                'unsubscribed': []
            }
        )
        logger.info(f"Канал {channel_id} успешно сохранен в DynamoDB")
        time.sleep(1)  # Добавляем задержку в 1 секунду после сохранения в DynamoDB
    except Exception as e:
        logger.error(f"Ошибка сохранения канала в DynamoDB: {e}")
        raise

async def process_message(client, chat_id, text, user_id):
    if text == '/start':
        welcome_message = ("Привет! Я бот для отслеживания изменений подписчиков вашего канала.\n\n"
                           "Чтобы подключить канал, выполните следующие шаги:\n"
                           "1. Добавьте меня в качестве администратора в ваш канал\n"
                           "2. Напишите мне @username вашего канала\n"
                           "3. После успешной проверки, напишите свою электронную почту\n\n"
                           "По всем вопросам обращайтесь к @alex_favin")
        await send_message(client, chat_id, welcome_message)
    elif text.startswith('@'):
        channel_name = text
        is_admin = await verify_channel_admin(client, user_id, channel_name)
        if is_admin:
            await send_message(client, chat_id, "Канал успешно проверен. Пожалуйста, напишите вашу электронную почту.")
            try:
                subscribers = await get_subscribers_list(client, channel_name)
                save_channel_to_dynamodb(channel_name, user_id, subscribers)
                logger.info(f"Канал {channel_name} успешно сохранен в DynamoDB для пользователя {user_id}")
            except Exception as e:
                logger.error(f"Ошибка при сохранении канала {channel_name} в DynamoDB для пользователя {user_id}: {str(e)}")
                await send_message(client, chat_id, "Произошла ошибка при сохранении данных канала. Пожалуйста, попробуйте еще раз.")
        else:
            await send_message(client, chat_id, "Вы не являетесь администратором этого канала или бот не добавлен в администраторы. Пожалуйста, проверьте и попробуйте снова.")
    elif '@' in text and '.' in text:  # Простая проверка на email
        email = text
        try:
            channel_name = get_channel_from_dynamodb(user_id)
            logger.info(f"Получено название канала из DynamoDB для пользователя {user_id}: {channel_name}")
        except Exception as e:
            logger.error(f"Ошибка при получении названия канала из DynamoDB для пользователя {user_id}: {str(e)}")
            channel_name = None
        
        if channel_name:
            try:
                subscribers = await get_subscribers_list(client, channel_name)
                logger.info(f"Получен список подписчиков для канала {channel_name}")
                send_email(channel_name, email, len(subscribers), json.dumps(subscribers, ensure_ascii=False, indent=2))
                logger.info(f"Отправлено email на адрес {email} с информацией о канале {channel_name}")
                await send_message(client, chat_id, f"Канал {channel_name} успешно подключен! Информация отправлена на {email}")
            except Exception as e:
                logger.error(f"Ошибка при обработке email {email} для канала {channel_name}: {str(e)}")
                await send_message(client, chat_id, "Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте еще раз.")
        else:
            logger.warning(f"Не удалось найти канал в DynamoDB для пользователя {user_id}")
            await send_message(client, chat_id, "Произошла ошибка. Пожалуйста, начните процесс подключения канала заново с команды /start")
    else:
        await send_message(client, chat_id, "Я не понимаю эту команду. Пожалуйста, следуйте инструкциям или используйте /start для начала.")

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

        logger.info(f"Обработка сообщения: chat_id={chat_id}, user_id={user_id}, text={text}")

        await process_message(client, chat_id, text, user_id)

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
