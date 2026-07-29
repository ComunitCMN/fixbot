FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База и журнал фиксаций — на volume, чтобы переживали рестарт:
#   docker run -v fixbot-data:/data -e DB_PATH=/data/fixbot.db ...
VOLUME ["/data"]

CMD ["python", "bot.py"]
