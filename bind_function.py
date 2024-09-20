import json
import boto3
import logging
from telethon import TelegramClient, events, Button
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.tl.functions.channels import GetParticipantsRequest
from botocore.exceptions import ClientError
from telethon.sessions import StringSession
import asyncio
import traceback
from telethon.errors.rpcerrorlist import FloodWaitError, ChannelInvalidError
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers')

# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

async def send_message(client, chat_id, text, buttons=None):
    try:
        if buttons:
            buttons = [[Button.inline(btn['text'], btn['callback_data']) for btn in row] for row in buttons]
        await client.send_message(chat_id, text, buttons=buttons, parse_mode='html')
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

async def verify_bot_admin(client, channel_name):
    try:
        channel = await client.get_input_entity(channel_name)
        bot_user = await client.get_me()
        admins = await client(GetParticipantsRequest(
            channel, filter=ChannelParticipantsAdmins(), offset=0, limit=100, hash=0))
        return any(admin.id == bot_user.id for admin in admins.users)
    except ChannelInvalidError:
        logger.error(f"Неверный канал: {channel_name}")
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки прав администратора бота: {e}")
        return False

async def process_channel_connection(client, chat_id, channel_name):
    try:
        await show_typing_animation(client, chat_id)
        is_admin = await verify_bot_admin(client, channel_name)
        if is_admin:
            # Здесь можно добавить логику сохранения канала в базу данных
            await send_message(client, chat_id, f"Канал {channel_name} успешно проверен. Теперь, пожалуйста, напишите свою электронную почту.")
        else:
            await send_message(client, chat_id, f"Ошибка: Бот не является администратором канала {channel_name}. "
                                  f"Сперва добавьте бота в качестве администратора в ваш канал.")
    except Exception as e:
        error_message = f"Ошибка при подключении канала: {str(e)}"
        logger.error(error_message)
        await send_message(client, chat_id, f"Произошла ошибка при проверке канала: {str(e)}")

async def lambda_handler(event, context):
    chat_id = None
    client = None
    
    try:
        logger.info(f"Получено событие: {event}")
        
        # Проверяем, есть ли 'body' в event
        if 'body' in event:
            body = json.loads(event['body'])
        else:
            # Если 'body' отсутствует, используем само событие как body
            body = event
        
        logger.info(f"Тело запроса: {body}")
        
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.start(bot_token=BOT_TOKEN)
            await asyncio.sleep(1)  # Добавляем задержку в 1 секунду после запуска клиента
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"Получена ошибка FloodWaitError. Необходимо подождать {wait_time} секунд.")
            return {
                'statusCode': 429,
                'body': json.dumps({
                    'error': 'Слишком много запросов. Пожалуйста, попробуйте позже.',
                    'wait_time': wait_time
                })
            }
        
        if 'message' in body:
            message = body['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')

            if text == '/start':
                await show_typing_animation(client, chat_id)
                welcome_message = ("Привет! Я бот для отслеживания изменений по людям которые подписались или отписались от твоего канала.\n"
                                   "Каждый день тебе на почту будет приходить письмо, содержащее ники людей, которые подписались и отписались от канала за предыдущие сутки.\n\n"
                                   "Чтобы подключить канал, выполните следующие шаги:\n"
                                   "1. Добавьте меня в качестве администратора в ваш канал\n"
                                   "2. Напишите мне @username вашего канала. Я проверю, имеете ли вы доступ к подобным данным\n"
                                   "3. В случае успешной проверки, напишите свою электронную почту. На нее сразу после подключения прилетит весь список подписчиков, а потом будут приходить ежедневные обновления.\n\n"
                                   "Если вы не подключили бота к каналу, вы не сможете пройти проверку, поэтому я не смогу начать свою работу.\n"
                                   "По всем вопросам, пиши моему создателю @alex_favin")
                await send_message(client, chat_id, welcome_message)
                return {'statusCode': 200, 'body': json.dumps('Приветственное сообщение отправлено')}

            if text and text.startswith('@'):
                await process_channel_connection(client, chat_id, text)
                return {'statusCode': 200, 'body': json.dumps('Обработка подключения канала завершена')}

            # Здесь можно добавить обработку ввода email

        return {'statusCode': 200, 'body': json.dumps('Сообщение обработано')}

    except Exception as e:
        error_message = f"Произошла ошибка: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_message)
        if chat_id and client:
            await send_message(client, chat_id, f"Произошла ошибка при обработке запроса. Пожалуйста, обратитесь в поддержку.")
        return {'statusCode': 400, 'body': json.dumps(error_message)}
    finally:
        if client:
            await client.disconnect()

def lambda_handler_wrapper(event, context):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(lambda_handler(event, context))
