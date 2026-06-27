# Используем тот же официальный базовый образ, что и твой текущий под
FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

# Устанавливаем Python 3 и системно зависимости для serverless handler'а
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN pip3 install runpod websocket-client requests boto3

# Копируем наши скрипты
COPY handler.py /handler.py
COPY start.sh /start.sh

# Делаем start.sh исполняемым
RUN chmod +x /start.sh

# При запуске Serverless Endpoint стартует этот скрипт
CMD ["/start.sh"]
