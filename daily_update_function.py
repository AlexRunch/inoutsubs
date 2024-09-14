import json
import boto3
from telethon import TelegramClient

# Конфигурация Telegram API
api_id = 24502638
api_hash = '751d5f310032a2f2b1ec888bd5fc7fcb'
bot_token = '7512734081:AAGVNe3SGMdY1AnaJwu6_mN4bKTxp3Z7hJs'

# Конфигурация DynamoDB и SES
dynamodb = boto3.resource('dynamodb', region_name='eu-north-1')
table = dynamodb.Table('telegram-subscribers')
ses_client = boto3.client('ses', region_name='eu-north-1')

def lambda_handler(event, context):
    response = table.scan()
    channels = response['Items']
    
    client = TelegramClient('session_name', api_id, api_hash).start(bot_token=bot_token)
    
    for channel_data in channels:
        channel_name = channel_data['channel_id']
        admin_email = channel_data['email']
        previous_subscribers = channel_data['subscribers']
        
        current_subscribers = client.loop.run_until_complete(get_subscribers_list(client, channel_name))
        
        new_subscribers = {key: value for key, value in current_subscribers.items() if key not in previous_subscribers}
        unsubscribed = {key: value for key, value in previous_subscribers.items() if key not in current_subscribers}
        
        if new_subscribers or unsubscribed:
            email_subject = f'Обновления по подписчикам канала {channel_name}'
            email_body = "Новые подписчики:\n" + "\n".join([f"{name}" for name in new_subscribers.values()]) + \
                         "\nОтписались:\n" + "\n".join([f"{name}" for name in unsubscribed.values()])
            
            send_email(email_subject, email_body, admin_email)
            
        table.update_item(
            Key={'channel_id': channel_name},
            UpdateExpression="set subscribers = :s",
            ExpressionAttributeValues={':s': current_subscribers}
        )
    
    return {'statusCode': 200, 'body': 'Ежедневная рассылка завершена.'}

def send_email(subject, body, recipient_email):
    ses_client.send_email(
        Source='mihailov.org@gmail.com',
        Destination={'ToAddresses': [recipient_email]},
        Message={
            'Subject': {'Data': subject},
            'Body': {'Text': {'Data': body}}
        }
    )

