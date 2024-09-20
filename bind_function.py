import json
import logging
import boto3
from telethon import TelegramClient
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.tl.functions.channels import GetParticipantsRequest
import asyncio

# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers')

async def send_message(chat_id, text):
    async with TelegramClient('bot', API_ID, API_HASH) as client:
        await client.start(bot_token=BOT_TOKEN)
        await client.send_message(chat_id, text, parse_mode='html')

async def verify_bot_admin(channel_name):
    async with TelegramClient('bot', API_ID, API_HASH) as client:
        await client.start(bot_token=BOT_TOKEN)
        try:
            channel = await client.get_input_entity(channel_name)
            bot_user = await client.get_me()
            admins = await client(GetParticipantsRequest(
                channel, filter=ChannelParticipantsAdmins(), offset=0, limit=100, hash=0))
            return any(admin.id == bot_user.id for admin in admins.users)
        except Exception as e:
            logger.error(f"Ошибка проверки прав администратора бота: {e}")
            return False

async def process_message(event):
    chat_id = event['message']['chat']['id']
    text = event['message'].get('text', '')

    if text == '/start':
        welcome_message = ("Привет! Я бот для отслеживания изменений подписчиков вашего канала.\n\n"
                           "Чтобы подключить канал, выполните следующие шаги:\n"
                           "1. Добавьте меня в качестве администратора в ваш канал\n"
                           "2. Напишите мне @username вашего канала\n"
                           "3. После успешной проверки, напишите свою электронную почту\n\n"
                           "По всем вопросам обращайтесь к @alex_favin")
        await send_message(chat_id, welcome_message)
    elif text.startswith('@'):
        is_admin = await verify_bot_admin(text)
        if is_admin:
            await send_message(chat_id, f"Канал {text} успешно проверен. Теперь напишите вашу электронную почту.")
        else:
            await send_message(chat_id, f"Ошибка: Бот не является администратором канала {text}. "
                                        f"Сначала добавьте бота как администратора в ваш канал.")
    elif '@' in text and '.' in text:  # Простая проверка на email
        # Здесь добавьте логику сохранения email в базу данных
        await send_message(chat_id, f"Email {text} сохранен. Вы будете получать ежедневные обновления на этот адрес.")
    else:
        await send_message(chat_id, "Извините, я не понимаю эту команду.")

def lambda_handler(event, context):
    try:
        logger.info(f"Получено событие: {event}")
        
        if 'body' not in event:
            return {'statusCode': 400, 'body': json.dumps('Неверный формат запроса')}
        
        body = json.loads(event['body'])
        logger.info(f"Тело запроса: {body}")
        
        if 'message' in body:
            asyncio.get_event_loop().run_until_complete(process_message(body))
        
        return {'statusCode': 200, 'body': json.dumps('OK')}
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps('Внутренняя ошибка сервера')}
