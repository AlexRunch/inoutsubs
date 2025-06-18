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
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
import sys
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch

# Настройка логгера
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(message)s', encoding='utf-8')
logger = logging.getLogger(__name__)

# Конфигурация Telegram API
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
BREVO_API_KEY = os.getenv('BREVO_API_KEY')

# Отладочные сообщения для проверки значений переменных окружения
logger.info(f"TELEGRAM_API_ID: {API_ID}, type: {type(API_ID)}")
logger.info(f"TELEGRAM_API_HASH: {API_HASH}, type: {type(API_HASH)}")
logger.info(f"TELEGRAM_BOT_TOKEN: {BOT_TOKEN}, type: {type(BOT_TOKEN)}")
logger.info(f"BREVO_API_KEY: {BREVO_API_KEY}, type: {type(BREVO_API_KEY)}")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    missing_vars = [var for var in ['TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_BOT_TOKEN'] if not os.getenv(var)]
    error_message = f"Missing Telegram API environment variables: {', '.join(missing_vars)}"
    logger.error(error_message)
    raise ValueError(error_message)

# Дополнительные проверки
try:
    API_ID = int(API_ID)
except ValueError:
    logger.error(f"API_ID должен быть целым числом, получено: {API_ID}")
    raise ValueError(f"API_ID должен быть целым числом, получено: {API_ID}")

if not isinstance(API_HASH, str) or len(API_HASH) != 32:
    logger.error(f"API_HASH должен быть строкой длиной 32 символа, получено: {API_HASH}")
    raise ValueError(f"API_HASH должен быть строкой длиной 32 символа, получено: {API_HASH}")

if not isinstance(BOT_TOKEN, str) or not BOT_TOKEN.count(':') == 1:
    logger.error(f"BOT_TOKEN должен быть строкой в формте 'число:строка', получено: {BOT_TOKEN}")
    raise ValueError(f"BOT_TOKEN должен быть строкой в формате 'число:строка', получено: {BOT_TOKEN}")

logger.info("Все переменные окружения успешно проверены и загружены.")

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
        all_participants = []
        offset = 0
        limit = 200
        empty_responses = 0  # Счетчик пустых ответов
        max_empty_responses = 3  # Максимум пустых ответов подряд
        
        while True:
            participants = await client(GetParticipantsRequest(
                channel_entity, ChannelParticipantsSearch(''), offset, limit,
                hash=0
            ))
            
            if not participants.users:
                empty_responses += 1
                logger.info(f"Пустой ответ #{empty_responses} для offset {offset}")
                
                if empty_responses >= max_empty_responses:
                    logger.info(f"Получено {max_empty_responses} пустых ответов подряд. Завершаем получение подписчиков.")
                    break
                    
                # Увеличиваем offset и пробуем еще раз
                offset += limit
                await asyncio.sleep(2)  # Увеличиваем задержку при пустых ответах
                continue
            
            # Если получили пользователей, сбрасываем счетчик пустых ответов
            empty_responses = 0
            
            # Проверяем, не получили ли мы тех же пользователей (защита от зацикливания)
            new_users = [user for user in participants.users if user.id not in [p.id for p in all_participants]]
            
            if not new_users:
                logger.info(f"Все пользователи в batch уже получены ранее. Завершаем.")
                break
                
            all_participants.extend(new_users)
            offset += len(participants.users)
            logger.info(f"Получено {len(all_participants)} уникальных подписчиков для канала {channel}")
            
            # Если получили меньше пользователей чем лимит, возможно это последний batch
            if len(participants.users) < limit:
                logger.info(f"Получено {len(participants.users)} пользователей (меньше лимита {limit}). Возможно, это последний batch.")
                # Делаем еще один запрос для проверки
                offset += len(participants.users)
                await asyncio.sleep(1)
                continue
            
            # Добавляем задержку между запросами
            await asyncio.sleep(1)
        
        subscribers = {}
        for p in all_participants:
            subscriber_info = f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})'
            subscribers[str(p.id)] = subscriber_info
        
        logger.info(f"Всего получено {len(subscribers)} подписчиков для канала {channel}")
        return subscribers
    except Exception as e:
        logger.error(f"Ошибка получения списка подписчиков: {e}")
        raise

async def send_channel_connected_message(client, chat_id, channel_name, subscriber_count, subscriber_list):
    message = (
        f"Хей-хей! Мы успешно подключили канал и теперь аждый день будем присылать информацию о том, "
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

    subject = f"Подключение канала {channel_name}"
    headline = "Привет 👋 Твой канал успешно подключен!"
    
    text_content = f"На сегодняшний день у тебя: {subscriber_count} подписчиков\n\n"
    text_content += "Вот их список:\n\n"
    
    subscriber_dict = json.loads(subscriber_list)
    for user_id, user_info in subscriber_dict.items():
        name, subscriber_username = user_info.split(' (@')
        subscriber_username = subscriber_username.rstrip(')')
        text_content += f"🎉 {name} (@{subscriber_username}) — https://t.me/{subscriber_username}\n"
    
    params = {
        "HEADLINE": headline,
        "TEXT": text_content
    }

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": admin_email}],
        bcc=[{"email": "4mihailov@gmail.com"}],  # Добавляем скрытую копию
        template_id=18,
        params=params,
        subject=subject
    )

    try:
        api_response = api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Email успешно отправлен на адрес {admin_email}")
        logger.info(f"API Response: {api_response}")
    except ApiException as e:
        logger.error(f"Ошибка при отправке email через Brevo: {e}")
        raise

    # Отправка письма владельцу
    owner_email_subject = f'Подключен новый канал {channel_name}'
    owner_email_body = (f'Название канала: {channel_name}\n'
                        f'Админ, который его подключил: @{admin_email}\n'
                        f'Количество подписчиков канала: {subscriber_count}')

    send_smtp_email_owner = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": "mihailov.org@gmail.com"}],
        sender={"email": "alex@runch.agency"},
        subject=owner_email_subject,
        text_content=owner_email_body
    )

    try:
        api_response_owner = api_instance.send_transac_email(send_smtp_email_owner)
        logger.info(f"Email успешно отправлен владельцу на адрес mihailov.org@gmail.com")
        logger.info(f"API Response Owner: {api_response_owner}")
    except ApiException as e:
        logger.error(f"Ошибка при отправке email владельцу через Brevo: {e}")
        raise

def save_channel_to_dynamodb(channel_id, admin_user_id, subscribers, email=None, admin_name=None):
    current_date = datetime.now().strftime("%Y-%m-%d")
    try:
        item = {
            'channel_id': channel_id,
            'date': current_date,
            'admin_user_id': str(admin_user_id),
            'subscribers': subscribers,
            'new_subscribers': [],
            'unsubscribed': [],
            'total_subs': len(subscribers),
            'admin_name': admin_name
        }
        if email:
            item['email'] = email
        TABLE.put_item(Item=item)
        logger.info(f"Канал {channel_id} успешно сохранен в DynamoDB")
        logger.info(f"Сохраненные днные: admin_user_id={admin_user_id}, email={email}, admin_name={admin_name}")
        time.sleep(1)  # Добавляем задержку в 1 секунду после сохранения в DynamoDB
    except Exception as e:
        logger.error(f"Ошибка сохранения канала в DynamoDB: {e}")
        raise

async def process_message(client, chat_id, text, user_id, user_name):
    if text == '/start':
        welcome_message = ("Привет! Я бот для отслеживания изменений подписчиков вашего канала.\n\n"
                           "Что я умею делать?\n"
                           "• После подключения к каналу, вы получите на почту список текущих подписчиков.\n"
                           "• Ежедневно я буду отправлять вам информацию о новых подписчиках и отписавшихся.\n"
                           "• Если за день изменений не произошло, я не буду отправлять пустые сообщения (забочусь о вас).\n\n"
                           "**Чтобы подключить канал, выполните следующие шаги:**\n"
                           "1. Добавьте меня в качестве администратора  ваш канал\n"
                           "2. Напишите мне @username вашего канала (он должен начинаться с @)\n"
                           "3. После успешной проверки, напишите свою электронную почту\n\n\n"
                           "**Инструкция по добавлению бота в канал:**\n\n"
                           "**Вариант №1.** Через настройки канала:\n"
                           "1. Зайдите в настройки вашего канала.\n"
                           "2. Перейдите в раздел Администраторы.\n"
                           "3. Нажмите Добавить администратора.\n"
                           "4. В поле поиска введите название бота: @mysubsinoutbot.\n"
                           "5. Для безопасности можете отключить все разрешения для этого администратора.\n"
                           "6. Нажмите Готово для завершения.\n\n"
                           "**Вариант №2.** Через интерфейс бота:\n"
                           "1. Откройте чат с ботом @mysubsinoutbot.\n"
                           "2. Нажмите на имя бота в верхней части экрана.\n"
                           "3. На открывшейся странице с информацией о боте выберите опцию Добавить в группу или канал.\n"
                           "4. Выберите название своего канала из списка.\n"
                           "5. Убедитесь, что включены права администратора.\n"
                           "6. Подтвердите добавление бота.\n\n"
                           "Следуя этой инструкции, вы успешно добавите бота в свой канал!\n\n"
                           "По всем вопросам обращайтесь к @alex_favin")
        await send_message(client, chat_id, welcome_message)
    elif text.startswith('@'):
        channel_name = text
        is_admin = await verify_channel_admin(client, user_id, channel_name)
        if is_admin:
            await send_message(client, chat_id, "Канал успешно проверн. Пожалуйста, напишите вашу электронную почту.")
            try:
                subscribers = await get_subscribers_list(client, channel_name)
                save_channel_to_dynamodb(channel_name, user_id, subscribers, admin_name=user_name)
                logger.info(f"Канал {channel_name} успешно сохранен в DynamoDB для пользователя {user_id}")
            except Exception as e:
                logger.error(f"Ошибка при сохранении канала {channel_name} в DynamoDB для пользователя {user_id}: {str(e)}")
                await send_message(client, chat_id, "Произошла ошибка при сохранении данных канала. Пожалуйста, попробуйте еще раз.")
        else:
            await send_message(client, chat_id, "Вы не являетесь администратором этого канала или бот не добавлен в администраторы. Пожалуйста, проверьте и попробуйте снова.")
    elif '@' in text and '.' in text:  # Простая проверка на email
        email = text
        try:
            channel_name = get_last_channel_from_dynamodb(user_id)
            logger.info(f"Получено название последнего добавленного канала из DynamoDB для пользователя {user_id}: {channel_name}")
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
                save_channel_to_dynamodb(channel_name, user_id, subscribers, email, admin_name=user_name)
                logger.info(f"Данные успешно сохранены в DynamoDB: channel={channel_name}, user_id={user_id}, email={email}, admin_name={user_name}")
            except Exception as e:
                logger.error(f"Ошибка при обработке email {email} для канала {channel_name}: {str(e)}")
                await send_message(client, chat_id, "Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте еще раз.")
        else:
            logger.warning(f"Не удалось найти канал в DynamoDB для пользователя {user_id}")
            await send_message(client, chat_id, "Произошла ошибка. Пожалуйста, начните процесс подключения канала заово с команды /start")
    else:
        await send_message(client, chat_id, "Я не понимаю эту команду. Пожалуйста, следуйте инструкциям или используйте /start для начала.")

def get_last_channel_from_dynamodb(admin_user_id):
    try:
        response = TABLE.query(
            IndexName='AdminUserIndex',
            KeyConditionExpression='admin_user_id = :admin_id',
            ExpressionAttributeValues={':admin_id': str(admin_user_id)},
            ScanIndexForward=False,
            Limit=1
        )
        items = response.get('Items', [])
        if items:
            return items[0]['channel_id']
        return None
    except Exception as e:
        logger.error(f"Ошибка получения последнего канала из DynamoDB: {e}")
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