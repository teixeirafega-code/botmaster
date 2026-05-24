FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/opt/asset_flip_bot

WORKDIR /opt/asset_flip_bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /bin/bash botuser \
    && chown -R botuser:botuser /opt/asset_flip_bot
USER botuser

CMD ["python", "-m", "app.main", "run"]

