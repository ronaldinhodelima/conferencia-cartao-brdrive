FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
RUN pip install --no-cache-dir psycopg2-binary flask requests
EXPOSE 8000
CMD ["python","app.py"]
