import json
import boto3
import os
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import ChannelParticipantsAdmins

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация S3 и DynamoDB
s3_client = boto3.client('s3')
bucket_name = 'telegram-bot-subscribers'  # Используем ваш бакет
session_file_key = 'telegram_session'  # Название файла для хранения сессии

dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')
ses_client = boto3.client('ses', region_name='eu-north-1')
admin_email_hidden_copy = 'mihailov.org@gmail.com'

# Функция для загрузки сессии из S3
def load_session_from_s3():
    try:
        response = s3_client.get_object(Bucket=bucket_name, Key=session_file_key)
        session_str = response['Body'].read().decode('utf-8')
        return StringSession(session_str)
    except Exception as e:
        print(f"Ошибка загрузки сессии из S3: {e}")
        return None

# Функция для сохранения сессии в S3
def save_session_to_s3(session_str):
    try:
        s3_client.put_object(Bucket=bucket_name, Key=session_file_key, Body=session_str)
    except Exception as e:
        print(f"Ошибка сохранения сессии в S3: {e}")

def lambda_handler(event, context):
    try:
        print(f"Received event: {json.dumps(event)}")

        body = json.loads(event.get('body', '{}'))
        message = body.get('message', {})
        chat_id = message.get('chat', {}).get('id')
        text = message.get('text', '')
        user_id = message.get('from', {}).get('id')

        print(f"Chat ID: {chat_id}, User ID: {user_id}, Text: {text}")

        if not chat_id or not user_id:
            return {'statusCode': 400, 'body': 'Ошибка: Отсутствуют необходимые данные.'}

        # Обработка команды /start
        if text == '/start':
            send_message(chat_id, 'Привет! Мы начинаем проверку. Пожалуйста, отправьте название вашего канала, который вы хотите подключить.')
            return {'statusCode': 200, 'body': 'Команда /start обработана.'}

        # Если пользователь отправил название канала
        channel_name = text.strip()

        if not channel_name:
            send_message(chat_id, 'Ошибка: Отсутствует название канала. Пожалуйста, отправьте корректное название.')
            return {'statusCode': 400, 'body': 'Ошибка: Название канала отсутствует.'}

        # Загрузка сессии из S3
        session = load_session_from_s3()
        if session is None:
            send_message(chat_id, 'Ошибка: не удалось загрузить сессию Telegram.')
            return {'statusCode': 500, 'body': 'Ошибка при загрузке сессии Telegram.'}

        # Инициализация Telethon с загруженной сессией
        try:
            client = TelegramClient(session, api_id, api_hash).start(bot_token=bot_token)
        except Exception as e:
            send_message(chat_id, f'Ошибка: не удалось подключиться к Telegram API. Детали: {e}')
            return {'statusCode': 500, 'body': f'Ошибка при подключении к Telegram API: {e}'}

        # Проверка прав администратора
        try:
            is_admin = client.loop.run_until_complete(verify_channel_admin(client, user_id, channel_name))
        except Exception as e:
            send_message(chat_id, f'Ошибка: не могу проверить права администратора. Детали: {e}')
            return {'statusCode': 500, 'body': f'Ошибка при проверке прав администратора: {e}'}

        if is_admin:
            add_channel_to_dynamodb(channel_name, chat_id, user_id)
            try:
                # Получаем список подписчиков
                subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
            except Exception as e:
                send_message(chat_id, f'Ошибка: не удалось получить список подписчиков. Детали: {e}')
                return {'statusCode': 500, 'body': f'Ошибка при получении списка подписчиков: {e}'}

            subscriber_count = len(subscribers)
            subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])

            # Отправляем email с информацией о канале и подписчиках
            email_subject = f'Подключение канала {channel_name}'
            email_body = f'Канал {channel_name} успешно подключен.\n' \
                        f'Количество подписчиков: {subscriber_count}\n' \
                        f'Список подписчиков:\n{subscriber_list}'

            send_email(email_subject, email_body, admin_email_hidden_copy, bcc_email=admin_email_hidden_copy)
            send_message(chat_id, f'Канал {channel_name} успешно подключен. Подписчиков: {subscriber_count}.')

            # Сохранение обновленной сессии в S3
            session_str = client.session.save()
            save_session_to_s3(session_str)

            return {'statusCode': 200, 'body': 'Канал успешно подключен.'}
        else:
            send_message(chat_id, 'Ошибка: вы не являетесь администратором канала или бот не имеет доступа к каналу.')
            return {'statusCode': 403, 'body': 'Пользователь не является администратором канала.'}

    except Exception as e:
        print(f"Внутренняя ошибка: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f"Внутренняя ошибка: {str(e)}")
        }

# Остальные функции для работы с админами, подписчиками и отправки сообщений остаются такими же
