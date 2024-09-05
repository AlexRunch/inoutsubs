import json
import boto3
from telethon import TelegramClient

# Конфигурация Telegram API
api_id = 24502638  # Ваш api_id
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'  # Ваш api_hash
phone = 'your_phone_number'
channel_name = 'your_channel_name'  # Имя вашего канала

# Конфигурация Amazon SES
sender_email = 'mihailov.org@gmail.com'
recipient_email = '4lokiam@gmail.com'
ses_client = boto3.client('ses', region_name='eu-north-1')

def send_email(subject, body):
    # Функция для отправки email через Amazon SES
    response = ses_client.send_email(
        Source=sender_email,
        Destination={'ToAddresses': [recipient_email]},
        Message={
            'Subject': {'Data': subject},
            'Body': {'Text': {'Data': body}}
        }
    )
    return response

async def get_subscriber_count(client, channel):
    # Получаем количество подписчиков канала
    channel_entity = await client.get_entity(channel)
    participants = await client.get_participants(channel_entity)
    return len(participants)

def lambda_handler(event, context):
    # Создаем экземпляр TelegramClient
    client = TelegramClient('session_name', api_id, api_hash)

    # Запускаем клиент и собираем данные о количестве подписчиков
    with client:
        client.loop.run_until_complete(client.start(phone=phone))
        subscriber_count = client.loop.run_until_complete(get_subscriber_count(client, channel_name))

    # Формируем текст для отправки по email
    email_subject = "Telegram Channel Report"
    email_body = f"Старт. Сегодня {subscriber_count} подписчиков"

    # Отправляем email через Amazon SES
    send_email(email_subject, email_body)

    return {
        'statusCode': 200,
        'body': json.dumps(f'Sent email with subscriber count: {subscriber_count}')
    }
