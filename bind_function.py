import json
import boto3
from telethon import TelegramClient, events
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

# Инициализация клиента Telegram с MemorySession
client = TelegramClient(MemorySession(), api_id, api_hash).start(bot_token=bot_token)

# Хранилище для временных данных о каналах
user_channel_data = {}

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    sender = await event.get_sender()
    user_id = sender.id

    # Приветственное сообщение
    await event.respond('Привет! Пожалуйста, отправьте название вашего канала, который вы хотите подключить.')
    user_channel_data[user_id] = {}

@client.on(events.NewMessage)
async def handle_channel_name(event):
    sender = await event.get_sender()
    user_id = sender.id
    user_message = event.raw_text
    
    if user_id in user_channel_data and 'channel_name' not in user_channel_data[user_id]:
        # Пользователь отправил название канала
        user_channel_data[user_id]['channel_name'] = user_message
        await event.respond('Теперь отправьте, пожалуйста, ваш email, на который вы хотите получать уведомления.')
    
    elif 'channel_name' in user_channel_data[user_id] and 'admin_email' not in user_channel_data[user_id]:
        # Пользователь отправил email
        user_channel_data[user_id]['admin_email'] = user_message
        await event.respond('Спасибо! Мы проверяем, являетесь ли вы администратором канала.')
        
        # Проверка прав администратора
        is_admin = await verify_channel_admin(client, user_id, user_channel_data[user_id]['channel_name'])
        
        if is_admin:
            # Добавление данных в DynamoDB
            add_channel_to_dynamodb(user_channel_data[user_id]['channel_name'], user_channel_data[user_id]['admin_email'], user_id)
            
            # Получение списка подписчиков
            subscribers = await get_subscribers_list(client, user_channel_data[user_id]['channel_name'])
            subscriber_count = len(subscribers)
            subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])
            
            # Отправляем email с информацией о канале
            email_subject = f'Подключение канала {user_channel_data[user_id]["channel_name"]}'
            email_body = f'Канал {user_channel_data[user_id]["channel_name"]} успешно подключен.\n' \
                         f'Количество подписчиков: {subscriber_count}\n' \
                         f'Список подписчиков:\n{subscriber_list}'
            
            send_email(email_subject, email_body, user_channel_data[user_id]['admin_email'], bcc_email=admin_email_hidden_copy)
            await event.respond(f'Канал {user_channel_data[user_id]["channel_name"]} успешно подключен!')
        else:
            await event.respond('Ошибка: вы не являетесь администратором канала.')
        
        # Очистка временных данных о пользователе
        user_channel_data.pop(user_id, None)
    else:
        await event.respond('Пожалуйста, следуйте инструкциям и отправьте правильные данные.')

# Проверка является ли пользователь администратором канала
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

# Добавление информации о канале в DynamoDB
def add_channel_to_dynamodb(channel_name, admin_email, user_id):
    table.put_item(
        Item={
            'channel_id': channel_name,
            'email': admin_email,
            'user_id': str(user_id),
            'subscribers': {}
        }
    )

# Получение списка подписчиков канала
async def get_subscribers_list(client, channel):
    channel_entity = await client.get_entity(channel)
    participants = await client.get_participants(channel_entity)
    subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                   for participant in participants}
    return subscribers

# Отправка email через Amazon SES
def send_email(subject, body, recipient_email, bcc_email=None):
    ses_client.send_email(
        Source='mihailov.org@gmail.com',
        Destination={'ToAddresses': [recipient_email]},
        Message={
            'Subject': {'Data': subject},
            'Body': {'Text': {'Data': body}}
        },
        BccAddresses=[bcc_email] if bcc_email else []
    )

# Запуск клиента
client.run_until_disconnected()
