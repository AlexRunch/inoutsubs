import json
import logging
import boto3
import time
import os
import asyncio
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
TABLE = DYNAMODB.Table('telegram-subscribers')

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
        time.sleep(1)  # Добавляем задержку в 1 секунду после отправки email
    except ClientError as e:
        logger.error(f"Ошибка отправки email через SES: {e}")
        raise

def save_channel_to_dynamodb(channel_name, user_id):
    try:
        TABLE.put_item(Item={'channel_id': channel_name, 'user_id': str(user_id)})
        logger.info(f"Канал {channel_name} успешно сохранен в DynamoDB")
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
        # Проверка канала
        channel_name = text
        is_admin = await verify_channel_admin(client, user_id, channel_name)
        if is_admin:
            try:
                last_reminder_time = get_last_reminder_time(user_id)
            except NameError:
                # Если функция не определена, создаем временное решение
                last_reminder_time = None
                logger.warning("Функция get_last_reminder_time не определена. Используется временное решение.")
            
            current_time = time.time()
            if last_reminder_time is None or (current_time - last_reminder_time) >= 180:  # 180 секунд = 3 минуты
                await send_message(client, chat_id, "Канал успешно проверен. Пожалуйста, напишите вашу электронную почту.")
                try:
                    save_last_reminder_time(user_id, current_time)
                except NameError:
                    logger.warning("Функция save_last_reminder_time не определена. Пропускаем сохранение времени напоминания.")
            try:
                save_channel_to_dynamodb(channel_name, user_id)
                logger.info(f"Канал {channel_name} успешно сохранен в DynamoDB для пользователя {user_id}")
            except Exception as e:
                logger.error(f"Ошибка при сохранении канала {channel_name} в DynamoDB для пользователя {user_id}: {str(e)}")
        else:
            await send_message(client, chat_id, "Вы не являетесь администратором этого канала или бот не добавлен в администраторы. Пожалуйста, проверьте и попробуйте снова.")
    elif '@' in text and '.' in text:  # Простая проверка на email
        # Обработка email
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

def get_channel_from_dynamodb(user_id):
    try:
        response = TABLE.get_item(Key={'user_id': str(user_id)})
        return response.get('Item', {}).get('channel_id')
    except Exception as e:
        logger.error(f"Ошибка получения канала из DynamoDB: {e}")
        return None
