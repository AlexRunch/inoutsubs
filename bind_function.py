import json
import boto3
import requests
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.sessions import StringSession

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и SES
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')
ses_client = boto3.client('ses', region_name='eu-north-1')
admin_email_hidden_copy = 'mihailov.org@gmail.com'

# Конфигурация S3 для сессий
s3_client = boto3.client('s3')
bucket_name = 'telegram-bot-subscribers'

def lambda_handler(event, context):
    chat_id = None  # Инициализация переменной chat_id
    try:
        print(f"Received event: {json.dumps(event)}")
        
        # Получаем chat_id и user_id
        message = event.get('message', {})
        chat_id = message.get('chat', {}).get('id', None)
        user_id = message.get('from', {}).get('id', None)
        text = message.get('text', '')

        print(f"Chat ID: {chat_id}, User ID: {user_id}, Text: {text}")

        if chat_id is None or user_id is None:
            raise ValueError("Отсутствуют данные chat_id или user_id")

        # Загрузка сессии из S3
        session = load_session_from_s3(str(user_id))
        client = TelegramClient(StringSession(session), api_id, api_hash).start(bot_token=bot_token)

        if text == "/start":
            send_message(chat_id, 'Привет! Мы начинаем проверку. Пожалуйста, отправьте название вашего канала, который вы хотите подключить.')
            return

        # Проверяем является ли пользователь администратором канала
        channel_name = text.strip()
        is_admin, error_message = client.loop.run_until_complete(verify_channel_admin(client, user_id, channel_name))

        if is_admin:
            send_message(chat_id, f'Вы успешно подключены к каналу {channel_name}.')
            subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
            save_subscribers_to_db(channel_name, subscribers)
            send_email(admin_email_hidden_copy, channel_name, subscribers)
        else:
            send_message(chat_id, f'Ошибка: {error_message}')
    except Exception as e:
        print(f"Ошибка: {str(e)}")
        if chat_id is not None:
            send_message(chat_id, f'Произошла ошибка: {str(e)}')

async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        print(f"Проверяем канал: {channel_entity.title} (ID: {channel_entity.id})")

        participants = await client.get_participants(channel_entity, filter=ChannelParticipantsAdmins)
        print(f"Администраторы канала: {[p.id for p in participants]}")

        for participant in participants:
            if participant.id == user_id:
                return True, None
        return False, "Вы не являетесь администратором канала."
    except ChannelPrivateError:
        return False, "Канал является приватным."
    except ChatAdminRequiredError:
        return False, "Боту не хватает прав администратора."
    except Exception as e:
        print(f"Ошибка при проверке канала: {str(e)}")
        return False, f"Не удалось подключиться к Telegram API. Детали: {str(e)}"

async def get_subscribers_list(client, channel_name):
    channel_entity = await client.get_entity(channel_name)
    participants = await client.get_participants(channel_entity)
    subscribers = {p.id: f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})' for p in participants}
    return subscribers

def save_subscribers_to_db(channel_name, subscribers):
    try:
        table.put_item(
            Item={
                'channel_id': channel_name,
                'subscribers': subscribers
            }
        )
        print(f"Список подписчиков канала {channel_name} успешно сохранен.")
    except Exception as e:
        print(f"Ошибка при сохранении подписчиков в DynamoDB: {str(e)}")

def send_email(admin_email, channel_name, subscribers):
    subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])
    subject = f"Подключение канала {channel_name}"
    body = f"Канал {channel_name} подключен. Количество подписчиков: {len(subscribers)}.\n\nСписок подписчиков:\n{subscriber_list}"
    
    try:
        ses_client.send_email(
            Source=admin_email_hidden_copy,
            Destination={'ToAddresses': [admin_email]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Text': {'Data': body}}
            }
        )
        print(f"Email успешно отправлен на {admin_email}")
    except Exception as e:
        print(f"Ошибка при отправке email: {str(e)}")

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    try:
        response = requests.post(url, json=payload)
        print(f"Сообщение успешно отправлено: {response.json()}")
    except Exception as e:
        print(f"Ошибка при отправке сообщения: {str(e)}")

def load_session_from_s3(user_id):
    try:
        response = s3_client.get_object(Bucket=bucket_name, Key=f"sessions/{user_id}.session")
        session_data = response['Body'].read().decode('utf-8')
        print(f"Сессия загружена для пользователя {user_id}")
        return session_data
    except Exception as e:
        print(f"Ошибка загрузки сессии из S3: {str(e)}")
        return ""
