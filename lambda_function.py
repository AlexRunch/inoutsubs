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

# Конфигурация DynamoDB
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')  # Имя вашей таблицы в DynamoDB
partition_key = '@alex_runch'  # Указан ваш ключ

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
    # Преобразуем ID в строку с помощью str(participant.id)
    subscribers = {str(participant.username or participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                   for participant in participants}
    
    return subscribers

def get_previous_subscribers():
    # Получаем предыдущий список подписчиков из DynamoDB
    response = table.get_item(Key={'@alex_runch': partition_key})  # Используем ваш ключ @alex_runch
    return response.get('Item', {}).get('subscribers', {})

def update_subscribers_in_db(subscribers):
    # Обновляем список подписчиков в базе данных DynamoDB
    table.put_item(
        Item={
            '@alex_runch': partition_key,
            'subscribers': subscribers  # Ключи и значения в словаре теперь строки
        }
    )

def lambda_handler(event, context):
    # Указываем путь для хранения сессии Telethon в /tmp/ (это временная директория AWS Lambda)
    session_file_path = '/tmp/bot_session'

    # Создаем экземпляр TelegramClient с использованием bot_token
    client = TelegramClient(session_file_path, api_id, api_hash).start(bot_token=bot_token)

    # Собираем текущий список подписчиков
    with client:
        current_subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))

    # Получаем предыдущий список подписчиков из DynamoDB
    previous_subscribers = get_previous_subscribers()

    # Вычисляем подписавшихся и отписавшихся
    new_subscribers = {key: value for key, value in current_subscribers.items() if key not in previous_subscribers}
    unsubscribed = {key: value for key, value in previous_subscribers.items() if key not in current_subscribers}

    # Формируем текст для отправки по email с использованием username вместо ID, если он доступен
    if new_subscribers or unsubscribed:
        email_subject = "Ежедневная сводка изменений подписчиков Telegram"
        email_body = "✅ *Подписались:*\n" + \
                     "\n".join([f"🎉 {value} — [Открыть профиль](https://t.me/{key})" for key, value in new_subscribers.items() if isinstance(key, str)]) + \
                     "\n\n💔 *Отписались:*\n" + \
                     "\n".join([f"😢 {value} — [Посмотреть профиль](https://t.me/{key})" for key, value in unsubscribed.items() if isinstance(key, str)])
    else:
        email_subject = "Ежедневная сводка: без изменений"
        email_body = "Новых подписчиков нет. Никто не отписался."

    # Отправляем email через Amazon SES
    send_email(email_subject, email_body)

    # Обновляем список подписчиков в базе данных
    update_subscribers_in_db(current_subscribers)

    return {
        'statusCode': 200,
        'body': json.dumps(f'Sent daily report with {len(new_subscribers)} new and {len(unsubscribed)} unsubscribed users')
    }
