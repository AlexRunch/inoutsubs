import json
import boto3
import requests
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.types import ChannelParticipantsAdmins

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и SES
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')
ses_client = boto3.client('ses', region_name='eu-north-1')
admin_email_hidden_copy = 'mihailov.org@gmail.com'

def lambda_handler(event, context):
    try:
        # Логируем весь запрос от Webhook
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

        # Инициализация Telethon с MemorySession
        try:
            client = TelegramClient(MemorySession(), api_id, api_hash).start(bot_token=bot_token)
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

async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        participants = await client.get_participants(channel_entity, filter=ChannelParticipantsAdmins)

        for participant in participants:
            if participant.id == user_id:
                return True
        return False
    except Exception as e:
        print(f"Ошибка проверки прав администратора: {e}")
        raise e

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
    except Exception as e:
        raise Exception(f'Ошибка при записи данных в DynamoDB: {e}')

async def get_subscribers_list(client, channel):
    try:
        channel_entity = await client.get_entity(channel)
        participants = await client.get_participants(channel_entity)
        subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                    for participant in participants}
        return subscribers
    except Exception as e:
        raise Exception(f'Ошибка при получении списка подписчиков: {e}')

def send_message(chat_id, text):
    try:
        url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
        data = {
            'chat_id': chat_id,
            'text': text
        }
        print(f"Отправка сообщения: {data}")
        r = requests.post(url, json=data)
        print(f"Ответ Telegram API: {r.status_code}, {r.text}")
        return r.json()
    except Exception as e:
        print(f"Ошибка при отправке сообщения: {e}")
        raise e

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
        raise Exception(f'Ошибка при отправке email: {e}')
