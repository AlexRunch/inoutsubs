import json

def lambda_handler(event, context):
    # Пример простого кода, который возвращает сообщение об успешном выполнении
    return {
        'statusCode': 200,
        'body': json.dumps('Hello from Lambda!')
    }
