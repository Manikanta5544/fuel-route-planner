FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WEB_CONCURRENCY=2
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir "django>=6.1.2,<6.2" pydantic httpx numpy "shapely>=2" pyproj orjson cachetools redis uvicorn
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --no-access-log"]
