import json
import boto3
import requests
import logging
from telethon import TelegramClient, events
from telethon.tl.types import ChannelParticipantsAdmins
from botocore.exceptions import ClientError

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация S3 и DynamoDB
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')

# Конфигурация SES
ses_client = boto3.client('ses', region_name='eu-north-1')
admin_email_hidden_copy = 'mihailov.org@gmail.com'


# Функция для отправки сообщений
def send_message(chat_id, text, buttons=None):
    try:
        url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
        data = {'chat_id': chat_id, 'text': text, 'reply_markup': buttons}
        requests.post(url, json=data)
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {e}")


# Функция для загрузки сессии из S3
def load_session_from_s3(chat_id):
    try:
        response = s3_client.get_object(Bucket='telegram-bot-subscribers', Key=f'{chat_id}_session')
        return response['Body'].read().decode('utf-8')
    except ClientError as e:
        logging.error(f"Ошибка загрузки сессии из S3: {e}")
        return None


# Функция для сохранения сессии в S3
def save_session_to_s3(chat_id, session_data):
    try:
        s3_client.put_object(Bucket='telegram-bot-subscribers', Key=f'{chat_id}_session', Body=session_data)
    except ClientError as e:
        logging.error(f"Ошибка сохранения сессии в S3: {e}")


# Функция для проверки прав администратора
async def verify_channel_admin(client, user_id, channel_name):
    try:
        channel_entity = await client.get_entity(channel_name)
        participants = await client.get_participants(channel_entity, filter=ChannelParticipantsAdmins)
        for participant in participants:
            if participant.id == user_id:
                return True
        return False
    except Exception as e:
        logging.error(f"Ошибка проверки прав администратора: {e}")
        return False


# Функция для получения списка подписчиков
async def get_subscribers_list(client, channel):
    channel_entity = await client.get_entity(channel)
    participants = await client.get_participants(channel_entity)
    subscribers = {str(participant.id): f'{participant.first_name or ""} {participant.last_name or ""} (@{participant.username or "N/A"})'
                   for participant in participants}
    return subscribers


# Функция для отправки email с информацией о канале и подписчиках
def send_email(channel_name, admin_email, subscriber_count, subscriber_list):
    email_subject = f'Подключение канала {channel_name}'
    email_body = (f'Канал {channel_name} успешно подключен.\n'
                  f'Количество подписчиков: {subscriber_count}\n'
                  f'Список подписчиков:\n{subscriber_list}')
    
    try:
        ses_client.send_email(
            Source='mihailov.org@gmail.com',
            Destination={'ToAddresses': [admin_email]},
            Message={
                'Subject': {'Data': email_subject},
                'Body': {'Text': {'Data': email_body}}
            },
            BccAddresses=[admin_email_hidden_copy]
        )
    except ClientError as e:
        logging.error(f"Ошибка отправки email через SES: {e}")


# Основная функция Lambda
def lambda_handler(event, context):
    chat_id = None  # Инициализируем переменную заранее
    
    try:
        # Проверка на наличие данных
        if 'body' not in event:
            raise ValueError("'body' не найден в event")
        
        body = json.loads(event['body'])
        message = body['message']
        chat_id = message['chat']['id']  # Назначаем значение переменной chat_id
        user_id = message['from']['id']
        text = message.get('text', '')

        # При команде /start отправляем инструкции
        if text == '/start':
            instructions = ("Привет! Я помогу вам подключить канал для получения статистики.\n"
                            "Чтобы начать, нажмите кнопку 'Проверить канал' и введите название вашего канала.")
            buttons = {
                'inline_keyboard': [[{'text': 'Проверить канал', 'callback_data': 'check_channel'}]]
            }
            send_message(chat_id, instructions, buttons=json.dumps(buttons))
            return {'statusCode': 200, 'body': 'Инструкции отправлены'}

        # Обработка команды проверки канала
        if 'callback_query' in body:
            callback_data = body['callback_query']['data']
            if callback_data == 'check_channel':
                send_message(chat_id, "Введите название канала для проверки.")
                return {'statusCode': 200, 'body': 'Запрошено название канала'}

        # Обработка ввода названия канала
        if text and text.startswith('@'):
            client = TelegramClient(f'{chat_id}_session', api_id, api_hash).start(bot_token=bot_token)
            is_admin = client.loop.run_until_complete(verify_channel_admin(client, user_id, text))
            if is_admin:
                send_message(chat_id, f"Вы являетесь администратором канала {text}. Канал будет подключен.")
                
                # Получаем список подписчиков
                subscribers = client.loop.run_until_complete(get_subscribers_list(client, text))
                subscriber_count = len(subscribers)
                subscriber_list = "\n".join([f'{name} (ID: {user_id})' for user_id, name in subscribers.items()])

                # Сохраняем данные в DynamoDB
                save_channel_to_dynamodb(text, user_id)

                # Отправляем email
                send_email(text, 'admin@example.com', subscriber_count, subscriber_list)

            else:
                send_message(chat_id, f"Ошибка: Вы не являетесь администратором канала {text}. "
                                      f"Убедитесь, что бот добавлен в канал и что у вас есть права администратора.")

        return {'statusCode': 200, 'body': 'Сообщение обработано'}

    except Exception as e:
        logging.error(f"Произошла ошибка: {e}")
        if chat_id:
            send_message(chat_id, f"Произошла ошибка: {str(e)}")
        return {'statusCode': 400, 'body': f'Произошла ошибка: {str(e)}'}


# Функция для сохранения информации о канале в DynamoDB
def save_channel_to_dynamodb(channel_name, user_id):
    try:
        table.put_item(
            Item={
                'channel_id': channel_name,
                'user_id': str(user_id),
            }
        )
    except ClientError as e:
        logging.error(f"Ошибка сохранения данных в DynamoDB: {e}")
