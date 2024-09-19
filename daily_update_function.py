import json
import boto3
from telethon import TelegramClient
from telethon.sessions import MemorySession

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и SES
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')
ses_client = boto3.client('ses', region_name='eu-north-1')

def lambda_handler(event, context):
    # Используем MemorySession вместо SQLite для работы в среде Lambda
    client = TelegramClient(MemorySession(), api_id, api_hash).start(bot_token=bot_token)
    
    # Получение всех каналов из DynamoDB
    response = table.scan()
    channels = response['Items']

    for channel_data in channels:
        # Проверка на наличие ключа 'channel_id' в записи
        if 'channel_id' not in channel_data:
            print(f"Запись без channel_id: {channel_data}")
            continue

        channel_name = channel_data['channel_id']
        admin_email = channel_data.get('email', 'no_email_provided@example.com')
        previous_subscribers = channel_data.get('subscribers', {})
        
        # Получение текущих подписчиков канала
        current_subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
        
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
        table.update_item(
            Key={'channel_id': channel_name},
            UpdateExpression="set subscribers = :s",
            ExpressionAttributeValues={':s': current_subscribers}
        )
    
    return {'statusCode': 200, 'body': 'Ежедневная рассылка завершена.'}

async def get_subscribers_list(client, channel):
    channel_entity = await client.get_entity(channel)
    participants = await client.get_participants(channel_entity)
    subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                   for participant in participants}
    return subscribers

def send_email(subject, body, recipient_email):
    ses_client.send_email(
        Source='mihailov.org@gmail.com',
        Destination={'ToAddresses': [recipient_email]},
        Message={
            'Subject': {'Data': subject},
            'Body': {'Text': {'Data': body}}
        }
    )
