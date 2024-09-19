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
admin_email_hidden_copy = 'mihailov.org@gmail.com'

def lambda_handler(event, context):
    # Получаем данные из event
    channel_name = event.get('channel_name', None)
    admin_email = event.get('admin_email', None)
    user_id = event.get('user_id', None)
    
    if not channel_name or not admin_email or not user_id:
        return {'statusCode': 400, 'body': 'Ошибка: Отсутствуют необходимые данные.'}
    
    # Инициализация Telethon с MemorySession
    client = TelegramClient(MemorySession(), api_id, api_hash).start(bot_token=bot_token)
    
    # Проверка прав администратора
    is_admin = client.loop.run_until_complete(verify_channel_admin(client, user_id, channel_name))
    
    if is_admin:
        add_channel_to_dynamodb(channel_name, admin_email, user_id)
        
        # Получаем список подписчиков
        subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
        subscriber_count = len(subscribers)
        subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])

        # Отправляем email с информацией о канале и подписчиках
        email_subject = f'Подключение канала {channel_name}'
        email_body = f'Канал {channel_name} успешно подключен.\n' \
                     f'Количество подписчиков: {subscriber_count}\n' \
                     f'Список подписчиков:\n{subscriber_list}'
        
        send_email(email_subject, email_body, admin_email, bcc_email=admin_email_hidden_copy)
        return {'statusCode': 200, 'body': 'Канал успешно подключен.'}
    else:
        return {'statusCode': 403, 'body': 'Ошибка: Вы не являетесь администратором канала.'}

async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        participants = await client.get_participants(channel_entity, filter=ChannelParticipantsAdmins)
        for participant in participants:
            if participant.id == user_id:
                return True
        return False
    except Exception as e:
        print(f"Ошибка проверки канала: {e}")
        return False

def add_channel_to_dynamodb(channel_name, admin_email, user_id):
    # Добавление данных канала в DynamoDB
    table.put_item(
        Item={
            'channel_id': channel_name,
            'email': admin_email,
            'user_id': str(user_id),
            'subscribers': {}
        }
    )

async def get_subscribers_list(client, channel):
    channel_entity = await client.get_entity(channel)
    participants = await client.get_participants(channel_entity)
    subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                   for participant in participants}
    return subscribers

def send_email(subject, body, recipient_email, bcc_email=None):
    # Отправка email через Amazon SES
    ses_client.send_email(
        Source='mihailov.org@gmail.com',
        Destination={'ToAddresses': [recipient_email]},
        Message={
            'Subject': {'Data': subject},
            'Body': {'Text': {'Data': body}}
        },
        BccAddresses=[bcc_email] if bcc_email else []
    )
