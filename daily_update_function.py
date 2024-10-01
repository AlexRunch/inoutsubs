import json
import boto3
import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import MemorySession
from datetime import datetime
import logging
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# Установите уровень логирования на DEBUG   (это мойкомментарий)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и Brevo
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers-new')
BREVO_API_KEY = os.getenv('BREVO_API_KEY')  # Получение API ключа из переменных окружения

if not BREVO_API_KEY:
    error_message = "BREVO_API_KEY не установлен. Проверьте переменные окружения."
    logger.error(error_message)
    raise ValueError(error_message.encode('utf-8'))

async def get_subscribers_list(client, channel):
    try:
        channel_entity = await client.get_entity(channel)
        participants = await client.get_participants(channel_entity)
        subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                       for participant in participants}
        return subscribers
    except Exception as e:
        logger.error(f"Ошибка при получении списка подписчиков для канала {channel}: {e}")
        raise

def send_email(channel_name, new_subscribers, unsubscribed, recipient_email):
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    subject = f"{channel_name} изменение в подписчиках"
    headline = "Привет 👋  Принес тебе инфу по подписчикам"
    
    text_content = ""
    if new_subscribers:
        text_content += "Подписались:\n\n"
        for user_id, user_info in new_subscribers.items():
            name, username = user_info.rsplit('@', 1)
            username = username.strip('()')
            text_content += f"🎉 {name.strip()} (@{username}) — https://t.me/{username}\n\n"
        text_content += "\n"
    
    if unsubscribed:
        text_content += "Отписались:\n\n"
        for user_id, user_info in unsubscribed.items():
            name, username = user_info.rsplit('@', 1)
            username = username.strip('()')
            text_content += f"😢 {name.strip()} (@{username}) — https://t.me/{username}\n\n"

    params = {
        "HEADLINE": headline,
        "TEXT": text_content
    }

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": recipient_email}],
        template_id=18,
        params=params,
        subject=subject
    )

    try:
        api_response = api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Email успешно отправлен на адрес {recipient_email}")
        logger.info(f"API Response: {api_response}")
    except ApiException as e:
        logger.error(f"Ошибка при отправке email на адрес {recipient_email}: {e}")
        raise

def mask_email(email):
    parts = email.split('@')
    username = parts[0]
    domain = parts[1]
    masked_username = username[:3] + '*' * (len(username) - 3)
    masked_domain = domain[0] + '*' * (len(domain.split('.')[0]) - 1) + '.' + domain.split('.')[-1]
    return f"{masked_username}@{masked_domain}"

async def process_channel(client, channel_data):
    try:
        channel_name = channel_data['channel_id']
        date = channel_data['date']
        admin_email = channel_data.get('email')
        
        # Логирование данных канала
        logger.info(f"Обработка канала: {channel_name}, дата: {date}, email: {mask_email(admin_email)}")
        
        if not admin_email or admin_email == 'no_email_provided@example.com':
            logger.error(f"Адрес электронной почты администратора не указан для канала {channel_name}")
            return ("no_email", channel_name)
        
        previous_subscribers = channel_data.get('subscribers', '{}')
        
        # Проверка типа данных и преобразование в строку, если необходимо
        if isinstance(previous_subscribers, dict):
            previous_subscribers = json.dumps(previous_subscribers)
        
        previous_subscribers = json.loads(previous_subscribers)
        
        # Получение текущих подписчиков канала
        current_subscribers = await get_subscribers_list(client, channel_name)
        
        # Определение новых подписчиков и отписавшихя
        new_subscribers = {key: value for key, value in current_subscribers.items() if key not in previous_subscribers}
        unsubscribed = {key: value for key, value in previous_subscribers.items() if key not in current_subscribers}
        
        # Проверка наличия изменений в подписчиках
        if new_subscribers or unsubscribed:
            # Отправка email с использованием шаблона
            send_email(channel_name, new_subscribers, unsubscribed, admin_email)
            
            # Логирование успешной отправки
            logger.info(f"Письмо успешно отправлено: канал {channel_name}, админ {mask_email(admin_email)}")
            
            # Обновление списка подписчиков в DynamoDB
            TABLE.update_item(
                Key={'channel_id': channel_name, 'date': date},
                UpdateExpression="set subscribers = :s, last_update = :u",
                ExpressionAttributeValues={
                    ':s': json.dumps(current_subscribers, ensure_ascii=False),
                    ':u': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            logger.info(f"Список подписчиков для канала {channel_name} успешно обновлен в DynamoDB")
            return ("updated", channel_name)
        else:
            logger.info(f"Нет изменений в подписчиках для канала {channel_name}, email не отправлен")
            return ("not_updated", channel_name)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке канала {channel_name}: {e}")
        return ("error", channel_name)

async def main():
    try:
        # Используем MemorySession вместо SQLite для работы в среде Lambda
        client = TelegramClient(MemorySession(), API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        
        # Получение всех каналов из DynamoDB
        response = TABLE.scan()
        channels = response['Items']
        logger.info(f"Найдено {len(channels)} каналов для обработки")
        
        # Логирование данных каналов
        for channel in channels:
            logger.info(f"Данные канала: {channel['channel_id']} - {mask_email(channel.get('email', 'no_email_provided@example.com'))}")
        
        channels_processed = 0
        channels_updated = 0
        
        # Создание задач для обработки каждого канала
        tasks = [process_channel(client, channel_data) for channel_data in channels if 'channel_id' in channel_data and 'date' in channel_data]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            channels_processed += 1
            if isinstance(result, tuple) and result[0] == "updated":
                channels_updated += 1
        
        logger.info(f"Обработано каналов: {channels_processed}")
        logger.info(f"Обновлено каналов (отправлены email): {channels_updated}")
        
        await client.disconnect()
    except Exception as e:
        logger.error(f"Ошибка в main: {e}")
        raise

def lambda_handler(event, context):
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
        return {'statusCode': 200, 'body': 'Ежедневная рассылка завершена.'}
    except Exception as e:
        logger.error(f"Ошибка в lambda_handler: {e}")
        return {'statusCode': 500, 'body': f'Ошибка: {e}'.encode('utf-8')}