import json
import boto3
from telethon import TelegramClient
import os

# Конфигурация Telegram API
api_id = 24502638  # Ваш api_id
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'  # Ваш api_hash
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'  # Ваш bot token
channel_name = '@alex_runch'  # Имя вашего канала

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

async def get_subscribers_list(client, channel):
    # Получаем всех участников канала
    channel_entity = await client.get_entity(channel)
    participants = await client.get_participants(channel_entity)

    # Формируем список имен участников
    subscribers = [f'{participant.first_name} {participant.last_name or ""} (@{participant.username or "N/A"})'
                   for participant in participants]
    
    return subscribers

def lambda_handler(event, context):
    # Указываем путь для хранения сессии Telethon в /tmp/ (это временная директория AWS Lambda)
    session_file_path = '/tmp/bot_session'

    # Создаем экземпляр TelegramClient с использованием bot_token
    client = TelegramClient(session_file_path, api_id, api_hash).start(bot_token=bot_token)

    # Собираем данные о подписчиках
    with client:
        subscribers_list = client.loop.run_until_complete(get_subscribers_list(client, channel_name))

    # Формируем текст для отправки по email
    email_subject = "Список подписчиков канала Telegram"
    email_body = "Подписчики канала @alex_runch:\n\n" + "\n".join(subscribers_list)

    # Отправляем email через Amazon SES
    send_email(email_subject, email_body)

    return {
        'statusCode': 200,
        'body': json.dumps(f'Sent email with {len(subscribers_list)} subscribers')
    }
