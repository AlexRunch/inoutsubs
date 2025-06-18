import json
import boto3
import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import MemorySession
from datetime import datetime
import logging
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch

# Установите уровень логирования на DEBUG   (это мойкомментарий)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Конфигурация Telegram API
API_ID = 24502638
API_HASH = '751d5f310032a2f2b1ec888bd5fc7fcb'
BOT_TOKEN = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и Brevo
DYNAMODB = boto3.resource('dynamodb', region_name='eu-north-1')
TABLE = DYNAMODB.Table('telegram-subscribers-new')
BREVO_API_KEY = os.getenv('BREVO_API_KEY')  # Получение API ключа из переменных окружения

async def get_subscribers_list(client, channel):
    try:
        logger.info(f"Начинаю получение подписчиков для канала {channel}")
        channel_entity = await client.get_entity(channel)
        all_participants = []
        offset = 0
        limit = 100  # Уменьшаю лимит для стабильности
        max_iterations = 50  # Ограничиваю количество итераций
        iteration = 0
        
        while iteration < max_iterations:
            try:
                logger.info(f"Итерация {iteration + 1}, offset: {offset}, получено участников: {len(all_participants)}")
                
                participants = await asyncio.wait_for(
                    client(GetParticipantsRequest(
                        channel_entity, ChannelParticipantsSearch(''), offset, limit,
                        hash=0
                    )),
                    timeout=30  # Таймаут 30 секунд на запрос
                )
                
                if not participants.users:
                    logger.info(f"Больше нет участников, завершаю получение")
                    break
                    
                all_participants.extend(participants.users)
                offset += len(participants.users)
                iteration += 1
                
                logger.info(f"Получено {len(all_participants)} подписчиков для канала {channel}")
                
                # Добавляем задержку между запросами
                await asyncio.sleep(2)
                
            except asyncio.TimeoutError:
                logger.error(f"Таймаут при получении участников на итерации {iteration}")
                break
            except Exception as e:
                logger.error(f"Ошибка на итерации {iteration}: {e}")
                if iteration > 0:  # Если уже получили хотя бы что-то, продолжаем
                    break
                else:
                    raise
        
        subscribers = {}
        for p in all_participants:
            subscriber_info = f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})'
            subscribers[str(p.id)] = subscriber_info
        
        logger.info(f"Всего получено {len(subscribers)} подписчиков для канала {channel}")
        return subscribers
    except Exception as e:
        logger.error(f"Критическая ошибка получения списка подписчиков для канала {channel}: {e}")
        raise

def send_email(channel_name, new_subscribers, unsubscribed, recipient_email):
    # Проверяем BREVO_API_KEY только когда он действительно нужен
    if not BREVO_API_KEY:
        error_message = f"BREVO_API_KEY не установлен для отправки email канала {channel_name}"
        logger.error(error_message)
        raise ValueError(error_message)
        
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    subject = f"{channel_name} изменение в подписчиках"
    headline = "Привет 👋  Принес тебе инфу по подписчикам"
    
    text_content = ""
    if new_subscribers:
        text_content += "Подписались:\n\n"
        for user_id, user_info in new_subscribers.items():
            name, username = user_info.rsplit('@', 1)
            username = username.strip('()')
            text_content += f"🎉 {name.strip()} (@{username}) — https://t.me/{username}\n"
        text_content += "\n"
    
    if unsubscribed:
        text_content += "Отписались:\n\n"
        for user_id, user_info in unsubscribed.items():
            name, username = user_info.rsplit('@', 1)
            username = username.strip('()')
            text_content += f"😢 {name.strip()} (@{username}) — https://t.me/{username}\n"

    params = {
        "HEADLINE": headline,
        "TEXT": text_content
    }

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": recipient_email}],
        template_id=18,
        params=params,
        subject=subject
    )

    try:
        api_response = api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Email успешно отправлен на адрес {recipient_email}")
        logger.info(f"API Response: {api_response}")
    except ApiException as e:
        logger.error(f"Ошибка при отправке email на адрес {recipient_email}: {e}")
        raise

def mask_email(email):
    parts = email.split('@')
    username = parts[0]
    domain = parts[1]
    masked_username = username[:3] + '*' * (len(username) - 3)
    masked_domain = domain[0] + '*' * (len(domain.split('.')[0]) - 1) + '.' + domain.split('.')[-1]
    return f"{masked_username}@{masked_domain}"

async def process_channel(client, channel_data):
    try:
        channel_name = channel_data['channel_id']
        date = channel_data['date']
        admin_email = channel_data.get('email')
        
        # Логирование данных канала
        logger.info(f"Обработка канала: {channel_name}, дата: {date}, email: {mask_email(admin_email)}")
        
        if not admin_email or admin_email == 'no_email_provided@example.com':
            logger.error(f"Адрес электронной почты администратора не указан для канала {channel_name}")
            return ("no_email", channel_name)
        
        previous_subscribers = channel_data.get('subscribers', '{}')
        
        # Проверка типа данных и преобразование в строку, если необходимо
        if isinstance(previous_subscribers, dict):
            previous_subscribers = json.dumps(previous_subscribers)
        
        previous_subscribers = json.loads(previous_subscribers)
        
        # Получение текущих подписчиков канала
        current_subscribers = await get_subscribers_list(client, channel_name)
        
        # Определение новых подписчиков и отписавшихя
        new_subscribers = {key: value for key, value in current_subscribers.items() if key not in previous_subscribers}
        unsubscribed = {key: value for key, value in previous_subscribers.items() if key not in current_subscribers}
        
        # Проверка наличия изменений в подписчиках
        if new_subscribers or unsubscribed:
            # Отправка email с использованием шаблона
            send_email(channel_name, new_subscribers, unsubscribed, admin_email)
            
            # Логирование успешной отправки
            logger.info(f"Письмо успешно отправлено: канал {channel_name}, админ {mask_email(admin_email)}")
            
            # Обновление списка подписчиков в DynamoDB
            TABLE.update_item(
                Key={'channel_id': channel_name, 'date': date},
                UpdateExpression="set subscribers = :s, last_update = :u",
                ExpressionAttributeValues={
                    ':s': json.dumps(current_subscribers, ensure_ascii=False),
                    ':u': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            logger.info(f"Список подписчиков для канала {channel_name} успешно обновлен в DynamoDB")
            return ("updated", channel_name)
        else:
            logger.info(f"Нет изменений в подписчиках для канала {channel_name}, email не отправлен")
            return ("not_updated", channel_name)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке канала {channel_name}: {e}")
        return ("error", channel_name)

async def main():
    logger.info("=== ЗАПУСК DAILY UPDATE ФУНКЦИИ ===")
    try:
        logger.info("Инициализация Telegram клиента...")
        # Используем MemorySession вместо SQLite для работы в среде Lambda
        client = TelegramClient(MemorySession(), API_ID, API_HASH)
        
        # Добавляем таймаут на подключение
        await asyncio.wait_for(client.start(bot_token=BOT_TOKEN), timeout=30)
        logger.info("Telegram клиент успешно подключен")
        
        logger.info("Получение каналов из DynamoDB...")
        # Получение всех каналов из DynamoDB
        response = TABLE.scan()
        channels = response['Items']
        logger.info(f"Найдено {len(channels)} каналов для обработки")
        
        if not channels:
            logger.warning("Нет каналов для обработки")
            await client.disconnect()
            return
        
        # Логирование данных каналов
        for channel in channels:
            logger.info(f"Данные канала: {channel['channel_id']} - {mask_email(channel.get('email', 'no_email_provided@example.com'))}")
        
        channels_processed = 0
        channels_updated = 0
        
        logger.info("Начинаю обработку каналов...")
        # Создание задач для обработки каждого канала
        valid_channels = [channel_data for channel_data in channels if 'channel_id' in channel_data and 'date' in channel_data]
        logger.info(f"Валидных каналов для обработки: {len(valid_channels)}")
        
        if not valid_channels:
            logger.warning("Нет валидных каналов для обработки")
            await client.disconnect()
            return
            
        tasks = [process_channel(client, channel_data) for channel_data in valid_channels]
        
        # Добавляем общий таймаут на обработку всех каналов
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=300  # 5 минут на все каналы
        )
        
        for result in results:
            channels_processed += 1
            if isinstance(result, tuple) and result[0] == "updated":
                channels_updated += 1
            elif isinstance(result, Exception):
                logger.error(f"Ошибка при обработке канала: {result}")
        
        logger.info(f"=== ЗАВЕРШЕНИЕ ОБРАБОТКИ ===")
        logger.info(f"Обработано каналов: {channels_processed}")
        logger.info(f"Обновлено каналов (отправлены email): {channels_updated}")
        
        await client.disconnect()
        logger.info("Telegram клиент отключен")
        
    except asyncio.TimeoutError:
        logger.error("Общий таймаут выполнения функции")
        raise
    except Exception as e:
        logger.error(f"Критическая ошибка в main: {e}")
        raise

def lambda_handler(event, context):
    logger.info(f"=== LAMBDA HANDLER ЗАПУЩЕН ===")
    logger.info(f"Event: {json.dumps(event, default=str)}")
    logger.info(f"Context: timeout={context.get_remaining_time_in_millis()}ms")
    
    try:
        logger.info("Запуск основной функции...")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
        
        response = {
            'statusCode': 200, 
            'body': json.dumps({
                'message': 'Ежедневная рассылка завершена успешно',
                'timestamp': datetime.now().isoformat()
            })
        }
        logger.info(f"Успешное завершение: {response}")
        return response
        
    except Exception as e:
        error_message = f"Критическая ошибка в lambda_handler: {str(e)}"
        logger.error(error_message)
        
        response = {
            'statusCode': 500,
            'body': json.dumps({
                'error': error_message,
                'timestamp': datetime.now().isoformat()
            })
        }
        logger.error(f"Возвращаю ошибку: {response}")
        return response