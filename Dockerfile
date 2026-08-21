FROM python:3.11-slim
WORKDIR /app
COPY app.py /app/app.py
# static/ tem os logos e o favicon. Sem esta linha o app sobe, mas todas as telas
# ficam sem imagem (404 em /static/*) - o Flask serve essa pasta automaticamente.
COPY static/ /app/static/
RUN pip install --no-cache-dir flask psycopg2-binary
EXPOSE 8000
CMD ["python", "app.py"]
