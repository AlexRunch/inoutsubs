name: Deploy to AWS Lambda

on:
  push:
    branches:
      - main

jobs:
  deploy:
    runs-on: ubuntu-latest

    steps:
    - name: Checkout code
      uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.9'

    - name: Install dependencies
      run: |
        echo "Installing dependencies..."
        python -m venv venv
        . venv/bin/activate
        pip install -r requirements.txt

    - name: Package Lambda function
      run: |
        echo "Packaging Lambda function..."
        zip -r9 function.zip lambda_function.py || exit 1  # Упаковка файла из корня

    - name: Upload to AWS Lambda
      env:
        AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
        AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
      run: |
        echo "Deploying to AWS Lambda..."
        aws lambda update-function-code --function-name telegram-subscriber-tracker --zip-file fileb://function.zip --region eu-north-1 || exit 1
