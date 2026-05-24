FROM python:3.12-slim

WORKDIR /app

# System deps (minimal)
RUN pip install --no-cache-dir --upgrade pip

COPY yield_optimizer_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY yield_optimizer_bot /app/yield_optimizer_bot

ENV PYTHONUNBUFFERED=1

CMD ["python","-m","yield_optimizer_bot.app.main"]

