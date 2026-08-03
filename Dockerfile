FROM python:3.11-slim
WORKDIR /app
COPY app.py /app/app.py
RUN pip install --no-cache-dir flask psycopg2-binary
EXPOSE 8000
CMD ["python", "app.py"]
