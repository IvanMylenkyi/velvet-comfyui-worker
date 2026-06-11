# Используем тот же официальный базовый образ, что и твой текущий под
FROM runpod/comfyui:latest

# Устанавливаем системно зависимости для serverless handler'а
RUN pip3 install runpod websocket-client requests

# Копируем наши скрипты
COPY handler.py /handler.py
COPY start.sh /start.sh

# Делаем start.sh исполняемым
RUN chmod +x /start.sh

# При запуске Serverless Endpoint стартует этот скрипт
CMD ["/start.sh"]
