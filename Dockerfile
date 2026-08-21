FROM python:3.11-slim
WORKDIR /app
COPY app.py /app/app.py
# core.py tem as constantes e os helpers; views/ tem as rotas em blueprints.
# Sem estas duas linhas o container nem sobe (ImportError logo no boot).
COPY core.py /app/core.py
COPY views/ /app/views/
# static/ tem os logos, o favicon, o CSS e os JS. Sem esta linha o app sobe, mas
# todas as telas ficam sem imagem e SEM ESTILO (404 em /static/*).
COPY static/ /app/static/
# templates/ tem as telas em Jinja. Sem esta linha, toda rota que usa
# render_template() estoura TemplateNotFound (500).
COPY templates/ /app/templates/
RUN pip install --no-cache-dir flask psycopg2-binary
EXPOSE 8000
CMD ["python", "app.py"]
