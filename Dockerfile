FROM python:3.12-alpine
WORKDIR /app
COPY bridge.py /app/bridge.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8383
VOLUME ["/app/data"]
CMD ["python", "/app/bridge.py"]
