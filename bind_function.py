import json
import boto3
import os
import requests
from telethon import TelegramClient, errors
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.sessions import StringSession

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация S3 и DynamoDB
s3 = boto3.client('s3')
bucket_name = 'telegram-bot-subscribers'
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')
ses_client = boto3.client('ses', region_name='eu-north-1')

# Функция для отправки сообщений через Telegram API
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text
    }
    response = requests.post(url, json=payload)
    return response.json()

# Функция для загрузки сессии из S3
def load_session_from_s3(chat_id):
    try:
        response = s3.get_object(Bucket=bucket_name, Key=f'{chat_id}.session')
        session_data = response['Body'].read().decode('utf-8')
        return StringSession(session_data)
    except s3.exceptions.NoSuchKey:
        return StringSession()  # Если сессия отсутствует, создаем новую
    except Exception as e:
        print(f"Ошибка загрузки сессии из S3: {str(e)}")
        return None

# Функция для сохранения сессии в S3
def save_session_to_s3(chat_id, session):
    try:
        s3.put_object(Bucket=bucket_name, Key=f'{chat_id}.session', Body=session.save())
    except Exception as e:
        print(f"Ошибка сохранения сессии в S3: {str(e)}")

# Проверка прав администратора канала
async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel = await client.get_entity(channel_name)
        admins = await client.get_participants(channel, filter=ChannelParticipantsAdmins)
        for admin in admins:
            if admin.id == user_id:
                return True
        return False
    except errors.ChatAdminRequiredError:
        return False
    except Exception as e:
        print(f"Ошибка проверки прав администратора: {str(e)}")
        return False

# Получаем список подписчиков
async def get_subscribers_list(client, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        participants = await client.get_participants(channel_entity)
        subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                       for participant in participants}
        return subscribers
    except Exception as e:
        print(f"Ошибка получения списка подписчиков: {str(e)}")
        return {}

# Отправляем email с информацией о канале и подписчиках
def send_email(subject, body, recipient_email, bcc_email=None):
    try:
        ses_client.send_email(
            Source='mihailov.org@gmail.com',
            Destination={'ToAddresses': [recipient_email]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Text': {'Data': body}}
            },
            BccAddresses=[bcc_email] if bcc_email else []
        )
    except Exception as e:
        print(f"Ошибка отправки email: {str(e)}")

# Главная функция для Lambda
def lambda_handler(event, context):
    try:
        # Проверяем, есть ли ключ 'body' в событии
        if 'body' not in event:
            print(f"Содержимое event: {event}")
            raise KeyError("'body' не найден в event")
        
        # Обработка входящего сообщения из Webhook
        body = json.loads(event['body'])
        message = body.get('message', {})
        chat_id = message.get('chat', {}).get('id')
        user_id = message.get('from', {}).get('id')
        text = message.get('text')

        if not chat_id or not user_id:
            print("Ошибка: Отсутствуют данные chat_id или user_id")
            return {'statusCode': 400, 'body': 'Ошибка: Отсутствуют данные chat_id или user_id'}

        # Загружаем сессию для этого пользователя
        session = load_session_from_s3(chat_id)
        if not session:
            send_message(chat_id, 'Ошибка: не удалось загрузить сессию Telegram.')
            return {'statusCode': 500, 'body': 'Ошибка: не удалось загрузить сессию Telegram.'}

        client = TelegramClient(session, api_id, api_hash)
        client.start(bot_token=bot_token)

        if text == '/start':
            send_message(chat_id, 'Привет! Мы начинаем проверку. Пожалуйста, отправьте название вашего канала, который вы хотите подключить.')

        elif '@' in text:  # Если текст содержит имя канала
            channel_name = text
            is_admin = client.loop.run_until_complete(verify_channel_admin(client, user_id, channel_name))
            if is_admin:
                send_message(chat_id, f'Вы являетесь администратором канала {channel_name}. Канал будет подключен.')
                # Получаем список подписчиков
                subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
                subscriber_count = len(subscribers)
                subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])
                
                # Отправляем email
                email_subject = f'Подключение канала {channel_name}'
                email_body = f'Канал {channel_name} успешно подключен.\n' \
                             f'Количество подписчиков: {subscriber_count}\n' \
                             f'Список подписчиков:\n{subscriber_list}'
                send_email(email_subject, email_body, 'admin@example.com', bcc_email='mihailov.org@gmail.com')

                # Сохранение в DynamoDB
                table.put_item(Item={'channel_name': channel_name, 'user_id': str(user_id)})
            else:
                send_message(chat_id, 'Ошибка: Вы не являетесь администратором канала.')

        save_session_to_s3(chat_id, client.session)
        return {'statusCode': 200, 'body': 'OK'}
    except KeyError as e:
        print(f"Произошла ошибка: {str(e)}")
        return {'statusCode': 400, 'body': f"Произошла ошибка: {str(e)}"}
    except Exception as e:
        print(f"Произошла ошибка: {str(e)}")
        if 'chat_id' in locals():
            send_message(chat_id, f'Произошла ошибка: {str(e)}')
        return {'statusCode': 500, 'body': f'Произошла ошибка: {str(e)}'}
