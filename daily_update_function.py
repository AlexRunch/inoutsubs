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
from telethon.tl.types import ChannelParticipantsSearch, ChannelParticipantsRecent, ChannelParticipantsAdmins

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
        
        # Определяем тип канала для диагностики
        channel_type = "неизвестный"
        if hasattr(channel_entity, 'broadcast'):
            if channel_entity.broadcast:
                channel_type = "публичный канал"
            else:
                channel_type = "группа/супергруппа"
        
        if hasattr(channel_entity, 'megagroup') and channel_entity.megagroup:
            channel_type = "супергруппа"
            
        logger.info(f"📺 Тип канала {channel}: {channel_type}")
        logger.warning(f"⚠️ ВНИМАНИЕ: Боты имеют ограниченный доступ к участникам {channel_type}")
        
        all_participants = []
        offset = 0
        limit = 100  # Увеличиваю обратно для получения всех подписчиков
        max_iterations = 100  # Увеличиваю до 100 для больших каналов (до 10,000 подписчиков)
        iteration = 0
        
        logger.info(f"Параметры получения: батч={limit}, макс_итераций={max_iterations}, максимум_подписчиков={limit*max_iterations}")
        
        while iteration < max_iterations:
            try:
                logger.info(f"Итерация {iteration + 1}/{max_iterations}, offset: {offset}, получено участников: {len(all_participants)}")
                
                participants = await asyncio.wait_for(
                    client(GetParticipantsRequest(
                        channel_entity, ChannelParticipantsSearch(''), offset, limit,
                        hash=0
                    )),
                    timeout=30  # Увеличиваю таймаут обратно
                )
                
                if not participants.users:
                    logger.info(f"✅ Больше нет участников, завершаю получение на итерации {iteration + 1}")
                    
                    # ВАЖНО: Проверяем не слишком ли мало итераций для большого канала
                    if iteration < 5 and len(all_participants) < 500:
                        logger.warning(f"🤔 ПОДОЗРИТЕЛЬНО: Только {iteration + 1} итераций для канала с {len(all_participants)} участниками")
                        logger.warning(f"🤔 Возможно Telegram API ограничивает доступ бота к полному списку участников")
                    
                    break
                
                batch_size = len(participants.users)
                all_participants.extend(participants.users)
                offset += batch_size
                iteration += 1
                
                logger.info(f"📊 Итерация {iteration}: получено {batch_size} участников, всего: {len(all_participants)}")
                
                # Проверка на неполный батч - признак конца данных
                if batch_size < limit:
                    logger.info(f"✅ Получен неполный батч ({batch_size} < {limit}), это последние данные")
                    break
                
                # Принудительная очистка памяти
                participants = None
                
                # Добавляем задержку между запросами
                await asyncio.sleep(1.5)  # Немного увеличиваю для стабильности
                
            except asyncio.TimeoutError:
                logger.error(f"⏰ Таймаут при получении участников на итерации {iteration + 1}")
                if len(all_participants) > 0:
                    logger.warning(f"🔄 Продолжаю с {len(all_participants)} уже полученными участниками")
                    break
                else:
                    raise
            except Exception as e:
                logger.error(f"❌ Ошибка на итерации {iteration + 1}: {e}")
                if len(all_participants) > 0:
                    logger.warning(f"🔄 Продолжаю с {len(all_participants)} уже полученными участниками")
                    break
                else:
                    raise
        
        # ВАЖНО: Проверяем получили ли мы всех подписчиков
        if iteration >= max_iterations:
            logger.warning(f"⚠️ ВНИМАНИЕ: Достигнут лимит итераций ({max_iterations})! Возможно получены НЕ ВСЕ подписчики канала {channel}")
            logger.warning(f"⚠️ Получено: {len(all_participants)} подписчиков. В канале может быть больше!")
        
        subscribers = {}
        for p in all_participants:
            subscriber_info = f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})'
            subscribers[str(p.id)] = subscriber_info
        
        # Очищаем память
        all_participants = None
        
        logger.info(f"🎯 ИТОГО получено {len(subscribers)} подписчиков для канала {channel}")
        logger.info(f"📈 Статистика: {iteration} итераций, {len(subscribers)} уникальных подписчиков")
        
        return subscribers
    except Exception as e:
        logger.error(f"💥 Критическая ошибка получения списка подписчиков для канала {channel}: {e}")
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
        
        # Получение текущих подписчиков канала (АДМИНСКИЙ МЕТОД)
        logger.info(f"👑 Используем админский метод получения подписчиков для {channel_name}")
        current_subscribers = await get_subscribers_list_admin(client, channel_name)
        
        logger.info(f"📊 Сравнение подписчиков канала {channel_name}:")
        logger.info(f"📊 Предыдущих подписчиков: {len(previous_subscribers)}")
        logger.info(f"📊 Текущих подписчиков: {len(current_subscribers)}")
        
        # Определение новых подписчиков и отписавшихся
        new_subscribers = {key: value for key, value in current_subscribers.items() if key not in previous_subscribers}
        unsubscribed = {key: value for key, value in previous_subscribers.items() if key not in current_subscribers}
        
        logger.info(f"📊 Новых подписчиков: {len(new_subscribers)}")
        logger.info(f"📊 Отписавшихся: {len(unsubscribed)}")
        
        # ВАЖНАЯ ПРОВЕРКА: Подозрительная ситуация с ложными отписками
        if len(unsubscribed) > 0 and len(current_subscribers) < len(previous_subscribers):
            logger.warning(f"🚨 ПОДОЗРИТЕЛЬНАЯ СИТУАЦИЯ для канала {channel_name}:")
            logger.warning(f"🚨 Текущих подписчиков ({len(current_subscribers)}) МЕНЬШЕ чем было ({len(previous_subscribers)})")
            logger.warning(f"🚨 Возможно получены НЕ ВСЕ подписчики! Отписки могут быть ЛОЖНЫМИ!")
            
            # Если разница в подписчиках примерно равна количеству "отписавшихся", 
            # это скорее всего баг пагинации, а не реальные отписки
            subscriber_diff = len(previous_subscribers) - len(current_subscribers)
            if abs(subscriber_diff - len(unsubscribed)) <= 5:  # Допускаем погрешность в 5 человек
                logger.error(f"⛔ КРИТИЧЕСКАЯ ОШИБКА: Разница в подписчиках ({subscriber_diff}) ≈ количеству отписавшихся ({len(unsubscribed)})")
                logger.error(f"⛔ Это ЛОЖНЫЕ отписки из-за неполного получения данных!")
                logger.error(f"⛔ ПРОПУСКАЮ отправку уведомления для избежания ложной тревоги")
                
                # Обновляем только текущих подписчиков в БД, но НЕ отправляем уведомление
                TABLE.update_item(
                    Key={'channel_id': channel_name, 'date': date},
                    UpdateExpression="set subscribers = :s, last_update = :u",
                    ExpressionAttributeValues={
                        ':s': json.dumps(current_subscribers, ensure_ascii=False),
                        ':u': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                )
                logger.info(f"✅ Обновлены данные в БД для {channel_name} БЕЗ отправки уведомления")
                return ("updated_no_notification", channel_name)
        
        # ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: Telegram API ограничения для ботов
        # Если бот видит примерно такое же количество подписчиков как и раньше,
        # но не получает новых - это может быть ограничение API
        if (len(current_subscribers) <= len(previous_subscribers) + 10 and 
            len(current_subscribers) >= len(previous_subscribers) - 10 and
            len(new_subscribers) == 0 and len(unsubscribed) == 0):
            
            logger.info(f"📊 Стабильные данные для канала {channel_name}:")
            logger.info(f"📊 Изменений нет, возможно из-за ограничений Telegram API для ботов")
            logger.info(f"📊 Это нормально для больших публичных каналов")
            
            # Просто обновляем данные без уведомления
            TABLE.update_item(
                Key={'channel_id': channel_name, 'date': date},
                UpdateExpression="set subscribers = :s, last_update = :u",
                ExpressionAttributeValues={
                    ':s': json.dumps(current_subscribers, ensure_ascii=False),
                    ':u': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            logger.info(f"✅ Обновлены стабильные данные для {channel_name}")
            return ("stable_data", channel_name)
        
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
        
        # Добавляем общий таймаут на обработку всех каналов (увеличен для больших каналов)
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=600  # 10 минут на все каналы для обработки больших каналов
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

async def get_subscribers_list_as_user(client, channel, use_user_account=True):
    """
    Получение участников канала через пользовательский аккаунт
    Пользовательские аккаунты имеют полный доступ к участникам
    """
    try:
        logger.info(f"🔑 Получение подписчиков как ПОЛЬЗОВАТЕЛЬ для канала {channel}")
        
        # Создаем отдельный клиент для пользовательского аккаунта
        user_client = TelegramClient(MemorySession(), API_ID, API_HASH)
        
        # ВАЖНО: Здесь нужно авторизоваться как пользователь, а не как бот
        # Для этого нужен номер телефона и код подтверждения
        logger.info("📱 Требуется авторизация пользователя для полного доступа к участникам")
        
        # Пока используем бота, но с улучшенными методами
        return await get_subscribers_list_enhanced(client, channel)
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения участников как пользователь: {e}")
        # Откатываемся на стандартный метод
        return await get_subscribers_list(client, channel)

async def get_subscribers_list_enhanced(client, channel):
    """
    Улучшенный метод получения участников с множественными стратегиями
    """
    try:
        logger.info(f"🔍 Расширенное получение подписчиков для канала {channel}")
        channel_entity = await client.get_entity(channel)
        
        # Определяем тип канала
        channel_type = "неизвестный"
        if hasattr(channel_entity, 'broadcast'):
            if channel_entity.broadcast:
                channel_type = "публичный канал"
            else:
                channel_type = "группа/супергруппа"
        
        if hasattr(channel_entity, 'megagroup') and channel_entity.megagroup:
            channel_type = "супергруппа"
            
        logger.info(f"📺 Тип канала {channel}: {channel_type}")
        
        all_participants = []
        
        # СТРАТЕГИЯ 1: Обычный поиск участников
        logger.info("📋 Стратегия 1: Обычный поиск участников")
        participants_1 = await get_participants_with_filter(client, channel_entity, ChannelParticipantsSearch(''))
        all_participants.extend(participants_1)
        logger.info(f"✅ Стратегия 1: получено {len(participants_1)} участников")
        
        # СТРАТЕГИЯ 2: Последние активные участники
        logger.info("📋 Стратегия 2: Последние активные участники")
        try:
            from telethon.tl.types import ChannelParticipantsRecent
            participants_2 = await get_participants_with_filter(client, channel_entity, ChannelParticipantsRecent())
            # Добавляем только уникальных
            unique_participants_2 = [p for p in participants_2 if p.id not in [u.id for u in all_participants]]
            all_participants.extend(unique_participants_2)
            logger.info(f"✅ Стратегия 2: получено {len(unique_participants_2)} новых участников")
        except Exception as e:
            logger.warning(f"⚠️ Стратегия 2 недоступна: {e}")
        
        # СТРАТЕГИЯ 3: Поиск с популярными именами/символами
        logger.info("📋 Стратегия 3: Поиск с популярными символами")
        search_terms = ['a', 'e', 'i', 'o', 'u', 'м', 'а', 'е', 'и', 'о', 'у', 'н', 'т', 'с', 'р']
        for term in search_terms[:5]:  # Ограничиваем чтобы не превысить лимиты
            try:
                participants_3 = await get_participants_with_filter(client, channel_entity, ChannelParticipantsSearch(term))
                unique_participants_3 = [p for p in participants_3 if p.id not in [u.id for u in all_participants]]
                all_participants.extend(unique_participants_3)
                logger.info(f"✅ Стратегия 3 ('{term}'): получено {len(unique_participants_3)} новых участников")
                await asyncio.sleep(2)  # Пауза между запросами
            except Exception as e:
                logger.warning(f"⚠️ Стратегия 3 ('{term}') недоступна: {e}")
        
        # Удаляем дубликаты по ID
        unique_participants = {}
        for p in all_participants:
            unique_participants[p.id] = p
        
        logger.info(f"🎯 Всего уникальных участников: {len(unique_participants)}")
        
        # Преобразуем в нужный формат
        subscribers = {}
        for p in unique_participants.values():
            subscriber_info = f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})'
            subscribers[str(p.id)] = subscriber_info
        
        logger.info(f"🎯 ИТОГО получено {len(subscribers)} подписчиков для канала {channel} (РАСШИРЕННЫЙ МЕТОД)")
        return subscribers
        
    except Exception as e:
        logger.error(f"💥 Ошибка расширенного получения участников: {e}")
        # Откатываемся на стандартный метод
        return await get_subscribers_list(client, channel)

async def get_participants_with_filter(client, channel_entity, filter_type, max_participants=1000):
    """
    Получение участников с определенным фильтром
    """
    participants = []
    offset = 0
    limit = 100
    max_iterations = 10
    
    for iteration in range(max_iterations):
        try:
            result = await asyncio.wait_for(
                client(GetParticipantsRequest(
                    channel_entity, filter_type, offset, limit, hash=0
                )),
                timeout=30
            )
            
            if not result.users:
                break
                
            participants.extend(result.users)
            offset += len(result.users)
            
            if len(participants) >= max_participants:
                break
                
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.warning(f"⚠️ Ошибка на итерации {iteration}: {e}")
            break
    
    return participants

async def get_subscribers_list_admin(client, channel):
    """
    Получение участников канала с использованием админских прав бота
    Админ-боты имеют расширенный доступ к участникам
    """
    try:
        logger.info(f"👑 АДМИНСКИЙ ДОСТУП: Получение подписчиков для канала {channel}")
        channel_entity = await client.get_entity(channel)
        
        # Проверяем права бота
        try:
            # Пробуем получить права администратора
            admins = await client(GetParticipantsRequest(
                channel_entity, ChannelParticipantsAdmins(), 0, 10, hash=0
            ))
            bot_is_admin = False
            bot_me = await client.get_me()
            for admin in admins.users:
                if admin.id == bot_me.id:
                    bot_is_admin = True
                    break
            
            if bot_is_admin:
                logger.info(f"✅ БОТ ЯВЛЯЕТСЯ АДМИНОМ канала {channel}! Используем полный доступ")
            else:
                logger.warning(f"⚠️ Бот НЕ АДМИН канала {channel}, используем ограниченный доступ")
                
        except Exception as e:
            logger.warning(f"⚠️ Не удалось проверить админские права: {e}")
        
        # Определяем тип канала
        channel_type = "неизвестный"
        if hasattr(channel_entity, 'broadcast'):
            if channel_entity.broadcast:
                channel_type = "публичный канал"
            else:
                channel_type = "группа/супергруппа"
        
        if hasattr(channel_entity, 'megagroup') and channel_entity.megagroup:
            channel_type = "супергруппа"
            
        logger.info(f"📺 Тип канала {channel}: {channel_type}")
        
        all_participants = []
        
        # СТРАТЕГИЯ АДМИНИСТРАТОРА: Получаем ВСЕ типы участников
        
        # 1. ВСЕ УЧАСТНИКИ (главная стратегия для админов)
        logger.info("👑 АДМИНСКАЯ СТРАТЕГИЯ: Получение ВСЕХ участников")
        participants_all = await get_all_participants_admin(client, channel_entity)
        all_participants.extend(participants_all)
        logger.info(f"✅ Админская стратегия: получено {len(participants_all)} участников")
        
        # 2. ПОСЛЕДНИЕ АКТИВНЫЕ (дополнительно)
        logger.info("📋 Дополнительно: Последние активные участники")
        try:
            participants_recent = await get_participants_with_filter(client, channel_entity, ChannelParticipantsRecent(), 500)
            unique_recent = [p for p in participants_recent if p.id not in [u.id for u in all_participants]]
            all_participants.extend(unique_recent)
            logger.info(f"✅ Последние активные: получено {len(unique_recent)} новых участников")
        except Exception as e:
            logger.warning(f"⚠️ Последние активные недоступны: {e}")
        
        # 3. ПОИСК ПО СИМВОЛАМ (если нужно еще больше)
        if len(all_participants) < 1000:  # Если все еще мало участников
            logger.info("📋 Дополнительно: Поиск по популярным символам")
            search_terms = ['a', 'e', 'i', 'o', 'u', 'м', 'а', 'е', 'и', 'о']
            for term in search_terms[:3]:  # Ограничиваем чтобы не превысить лимиты
                try:
                    participants_search = await get_participants_with_filter(client, channel_entity, ChannelParticipantsSearch(term), 300)
                    unique_search = [p for p in participants_search if p.id not in [u.id for u in all_participants]]
                    all_participants.extend(unique_search)
                    logger.info(f"✅ Поиск '{term}': получено {len(unique_search)} новых участников")
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"⚠️ Поиск '{term}' недоступен: {e}")
        
        # Удаляем дубликаты по ID
        unique_participants = {}
        for p in all_participants:
            unique_participants[p.id] = p
        
        logger.info(f"🎯 Всего уникальных участников: {len(unique_participants)}")
        
        # Преобразуем в нужный формат
        subscribers = {}
        for p in unique_participants.values():
            subscriber_info = f'{p.first_name or ""} {p.last_name or ""} (@{p.username or "N/A"})'
            subscribers[str(p.id)] = subscriber_info
        
        logger.info(f"👑 ИТОГО получено {len(subscribers)} подписчиков для канала {channel} (АДМИНСКИЙ МЕТОД)")
        return subscribers
        
    except Exception as e:
        logger.error(f"💥 Ошибка админского получения участников: {e}")
        # Откатываемся на расширенный метод
        return await get_subscribers_list_enhanced(client, channel)

async def get_all_participants_admin(client, channel_entity, max_participants=10000):
    """
    Получение ВСЕХ участников с использованием админских прав
    """
    participants = []
    offset = 0
    limit = 200  # Админы могут запрашивать больше за раз
    max_iterations = 50  # Увеличиваем для больших каналов
    
    logger.info(f"👑 Запрос ВСЕХ участников: батч={limit}, макс_итераций={max_iterations}")
    
    for iteration in range(max_iterations):
        try:
            logger.info(f"👑 Админская итерация {iteration + 1}/{max_iterations}, offset: {offset}, получено: {len(participants)}")
            
            # Используем пустой поиск для получения всех участников
            result = await asyncio.wait_for(
                client(GetParticipantsRequest(
                    channel_entity, 
                    ChannelParticipantsSearch(''),  # Пустой поиск = все участники
                    offset, 
                    limit, 
                    hash=0
                )),
                timeout=45  # Увеличенный таймаут для админов
            )
            
            if not result.users:
                logger.info(f"✅ Получены ВСЕ участники канала на итерации {iteration + 1}")
                break
            
            batch_size = len(result.users)
            participants.extend(result.users)
            offset += batch_size
            
            logger.info(f"📊 Админская итерация {iteration + 1}: получено {batch_size} участников, всего: {len(participants)}")
            
            # Проверка на неполный батч
            if batch_size < limit:
                logger.info(f"✅ Получен последний неполный батч ({batch_size} < {limit})")
                break
            
            # Проверка лимита
            if len(participants) >= max_participants:
                logger.warning(f"⚠️ Достигнут лимит участников ({max_participants})")
                break
                
            await asyncio.sleep(1.5)  # Пауза между запросами
            
        except asyncio.TimeoutError:
            logger.error(f"⏰ Таймаут на админской итерации {iteration + 1}")
            if len(participants) > 0:
                logger.warning(f"🔄 Продолжаю с {len(participants)} участниками")
                break
            else:
                raise
        except Exception as e:
            logger.error(f"❌ Ошибка на админской итерации {iteration + 1}: {e}")
            if len(participants) > 0:
                logger.warning(f"🔄 Продолжаю с {len(participants)} участниками")
                break
            else:
                raise
    
    logger.info(f"👑 Админский метод: получено {len(participants)} участников за {iteration + 1} итераций")
    return participants