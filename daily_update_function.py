import json
import boto3
import asyncio
from telethon import TelegramClient
from telethon.sessions import MemorySession
from datetime import datetime
import logging

# Настройка логгера
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и SES
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers-new')
SES_CLIENT = boto3.client('ses', region_name='eu-north-1')

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
        logger.info(f"Email успешно отправлен на адрес {recipient_email}")
    except Exception as e:
        logger.error(f"Ошибка при отправке email на адрес {recipient_email}: {e}")
        raise

async def process_channel(client, channel_data):
    try:
        channel_name = channel_data['channel_id']
        date = channel_data['date']
        admin_email = channel_data.get('email', 'no_email_provided@example.com')
        previous_subscribers = channel_data.get('subscribers', '{}')
        
        # Проверка типа данных и преобразование в строку, если необходимо
        if isinstance(previous_subscribers, dict):
            previous_subscribers = json.dumps(previous_subscribers)
        
        previous_subscribers = json.loads(previous_subscribers)
        
        # Получение текущих подписчиков канала
        current_subscribers = await get_subscribers_list(client, channel_name)
        
        # Определение новых подписчиков и отписавшихся
        new_subscribers = {key: value for key, value in current_subscribers.items() if key not in previous_subscribers}
        unsubscribed = {key: value for key, value in previous_subscribers.items() if key not in current_subscribers}
        
        # Если есть изменения, отправляем email
        if new_subscribers or unsubscribed:
            email_subject = f'Обновления по подписчикам канала {channel_name}'
            email_body = "Новые подписчики:\n" + "\n".join([f"{name}" for name in new_subscribers.values()]) + \
                         "\nОтписались:\n" + "\n".join([f"{name}" for name in unsubscribed.values()])
            
            send_email(email_subject, email_body, admin_email)
        
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
    except Exception as e:
        logger.error(f"Ошибка при обработке канала {channel_name}: {e}")
        raise

async def main():
    try:
        # Используем MemorySession вместо SQLite для работы в среде Lambda
        client = TelegramClient(MemorySession(), API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        
        # Получение всех каналов из DynamoDB
        response = TABLE.scan()
        channels = response['Items']
        logger.info(f"Найдено {len(channels)} каналов для обработки")
        
        # Создание задач для обработки каждого канала
        tasks = [process_channel(client, channel_data) for channel_data in channels if 'channel_id' in channel_data and 'date' in channel_data]
        await asyncio.gather(*tasks)
        
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
        return {'statusCode': 500, 'body': f'Ошибка: {e}'}
