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
    # Получаем данные из event
    try:
        body = json.loads(event['body'])
    except KeyError:
        return {
            'statusCode': 400,
            'body': json.dumps('Invalid request: Missing event body')
        }

    message = body.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    text = message.get('text', '')
    user_id = message.get('from', {}).get('id')
    
    if not chat_id or not user_id:
        return {'statusCode': 400, 'body': 'Ошибка: Отсутствуют необходимые данные.'}
    
    # Если пользователь отправил /start
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
        return {'statusCode': 500, 'body': 'Ошибка при подключении к Telegram API.'}
    
    # Проверка прав администратора
    try:
        is_admin = client.loop.run_until_complete(verify_channel_admin(client, user_id, channel_name))
    except Exception as e:
        send_message(chat_id, f'Ошибка: не могу проверить права администратора. Детали: {e}')
        return {'statusCode': 500, 'body': 'Ошибка при проверке прав администратора.'}
    
    if is_admin:
        add_channel_to_dynamodb(channel_name, chat_id, user_id)
        
        try:
            # Получаем список подписчиков
            subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
        except Exception as e:
            send_message(chat_id, f'Ошибка: не удалось получить список подписчиков. Детали: {e}')
            return {'statusCode': 500, 'body': 'Ошибка при получении списка подписчиков.'}
        
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

async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        
        # Получаем список администраторов канала
        participants = await client.get_participants(channel_entity, filter=ChannelParticipantsAdmins)
        
        # Проверяем, является ли пользователь администратором
        for participant in participants:
            if participant.id == user_id:
                return True
        
        return False
    except Exception as e:
        print(f"Ошибка проверки прав администратора: {e}")
        raise e

def add_channel_to_dynamodb(channel_name, admin_email, user_id):
    # Добавление данных канала в DynamoDB
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
    # Отправка сообщения пользователю через Telegram Bot API
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    data = {
        'chat_id': chat_id,
        'text': text
    }
response = r.json()  # Или просто возвращайте r, если не нужен JSON
