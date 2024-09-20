import json
import logging
import boto3
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

# Конфигурация DynamoDB и SES
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers')
SES_CLIENT = boto3.client('ses', region_name='eu-north-1')

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

async def process_channel(client, channel_data):
    channel_name = channel_data['channel_id']
    admin_email = channel_data.get('email', 'no_email_provided@example.com')
    
    current_subscribers = await get_subscribers_list(client, channel_name)
    
    if current_subscribers:
        email_subject = f'Список подписчиков канала {channel_name}'
        email_body = "Список подписчиков:\n" + "\n".join([f"{name}" for name in current_subscribers.values()])
        
        send_email(email_subject, email_body, admin_email)
        
        # Обновление списка подписчиков в DynamoDB
        TABLE.update_item(
            Key={'channel_id': channel_name},
            UpdateExpression="set subscribers = :s",
            ExpressionAttributeValues={':s': current_subscribers}
        )
    else:
        logger.warning(f"Не удалось получить список подписчиков для канала {channel_name}")

async def main(event, context):
    client = TelegramClient(MemorySession(), API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    try:
        # Получение всех каналов из DynamoDB
        response = TABLE.scan()
        channels = response['Items']

        for channel_data in channels:
            if 'channel_id' not in channel_data:
                logger.warning(f"Запись без channel_id: {channel_data}")
                continue

            await process_channel(client, channel_data)
    
    finally:
        await client.disconnect()

def lambda_handler(event, context):
    try:
        asyncio.get_event_loop().run_until_complete(main(event, context))
        return {'statusCode': 200, 'body': json.dumps('Обработка завершена успешно')}
    except Exception as e:
        logger.error(f"Произошла ошибка: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps('Произошла внутренняя ошибка сервера')}
