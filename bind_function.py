import json
import boto3
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация S3 и DynamoDB
s3_client = boto3.client('s3')
bucket_name = 'telegram-bot-subscribers'  # Имя вашего бакета
session_file_key = 'telegram_session'  # Имя файла для хранения сессии

dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')

# Функция для отправки сообщений через бот
def send_message(chat_id, text):
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': text
    }
    headers = {'Content-Type': 'application/json'}
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        print(f"Ошибка при отправке сообщения: {response.text}")
    return response.json()

# Функция для сохранения сессии Telegram в S3
def save_session_to_s3(session_string):
    try:
        s3_client.put_object(Body=session_string, Bucket=bucket_name, Key=session_file_key)
        print("Сессия успешно сохранена в S3.")
    except Exception as e:
        print(f"Ошибка сохранения сессии в S3: {str(e)}")

# Функция для загрузки сессии из S3
def load_session_from_s3():
    try:
        response = s3_client.get_object(Bucket=bucket_name, Key=session_file_key)
        session_data = response['Body'].read().decode('utf-8')
        print("Сессия успешно загружена из S3.")
        return session_data
    except Exception as e:
        print(f"Ошибка загрузки сессии из S3: {str(e)}")
        return None

# Функция для проверки прав администратора в канале
async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        participants = await client.get_participants(channel_entity, filter=ChannelParticipantsAdmins)
        for participant in participants:
            if participant.id == user_id:
                return True
        return False
    except Exception as e:
        print(f"Ошибка проверки прав администратора: {str(e)}")
        return False

# Функция для добавления информации о канале в DynamoDB
def add_channel_to_dynamodb(channel_name, admin_email, user_id):
    try:
        table.put_item(
            Item={
                'channel_id': channel_name,
                'email': admin_email,
                'user_id': str(user_id),
                'subscribers': {}
            }
        )
        print(f"Канал {channel_name} успешно добавлен в DynamoDB.")
    except Exception as e:
        print(f"Ошибка добавления канала в DynamoDB: {str(e)}")

def lambda_handler(event, context):
    try:
        # Логируем все событие для проверки структуры
        print(f"Received event: {json.dumps(event)}")

        body = json.loads(event.get('body', '{}'))
        message = body.get('message', {})
        chat_id = message.get('chat', {}).get('id')
        text = message.get('text', '')
        user_id = message.get('from', {}).get('id')

        print(f"Chat ID: {chat_id}, User ID: {user_id}, Text: {text}")

        if not chat_id or not user_id:
            send_message(chat_id, 'Ошибка: Отсутствуют необходимые данные.')
            return {'statusCode': 400, 'body': 'Ошибка: Отсутствуют необходимые данные.'}

        # Обработка команды /start
        if text == '/start':
            send_message(chat_id, 'Привет! Мы начинаем проверку. Пожалуйста, отправьте название вашего канала, который вы хотите подключить.')
            return {'statusCode': 200, 'body': 'Команда /start обработана.'}

        # Проверка данных о канале
        channel_name = text.strip()
        if not channel_name:
            send_message(chat_id, 'Ошибка: Пожалуйста, отправьте корректное название канала.')
            return {'statusCode': 400, 'body': 'Ошибка: Некорректное название канала.'}

        # Инициализация клиента Telegram
        session_string = load_session_from_s3()
        if session_string:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
        else:
            client = TelegramClient(StringSession(), api_id, api_hash)
            session_string = client.session.save()
            save_session_to_s3(session_string)

        client.start(bot_token=bot_token)

        # Проверка, является ли пользователь администратором канала
        is_admin = client.loop.run_until_complete(verify_channel_admin(client, user_id, channel_name))
        if is_admin:
            send_message(chat_id, f'Вы являетесь администратором канала {channel_name}. Канал успешно подключен.')
            # Сохраняем канал в DynamoDB
            add_channel_to_dynamodb(channel_name, 'admin_email@example.com', user_id)  # Адрес почты можно получить позже
        else:
            send_message(chat_id, 'Ошибка: Вы не являетесь администратором канала.')

        return {'statusCode': 200, 'body': 'Запрос обработан успешно.'}

    except Exception as e:
        print(f"Внутренняя ошибка: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f"Внутренняя ошибка: {str(e)}")
        }
