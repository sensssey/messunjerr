FROM python:3.12.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Приложение работает не от root
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Готовность БД обеспечивают healthcheck в docker-compose.yml и повторные попытки подключения в самом приложении
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
