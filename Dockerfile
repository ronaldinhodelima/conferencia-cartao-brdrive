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
RUN pip install --no-cache-dir flask psycopg2-binary gunicorn
EXPOSE 8000
# gunicorn no lugar do servidor embutido do Flask, que atende uma requisicao por
# vez e avisa no proprio log que nao e para producao.
#
# --preload importa o app UMA vez no processo mestre e so depois faz o fork. Isso
# importa aqui porque core.py roda migrate() no import: sem preload, cada worker
# rodaria a migracao ao mesmo tempo no boot e as DDL competiriam entre si.
# E seguro porque nenhuma conexao de banco fica aberta em variavel de modulo -
# migrate() abre e fecha a dele, e get_conn() cria uma por requisicao. Se algum
# dia surgir um pool global, o preload passa a compartilhar socket entre os
# processos filhos e vira bug.
#
# --timeout 120 porque o botao "Atualizar agora" chama o worker de sync com
# urllib usando timeout de 60s; o padrao do gunicorn (30s) mataria o worker antes.
#
# UM worker com threads, e nao varios workers: core.py mantem os apelidos de
# categoria (CATEGORIA_PT_DB) em memoria e so os recarrega depois de um POST.
# Com 2 processos, cada um tem a sua copia - renomear uma categoria atualizava a
# de quem atendeu o POST, e o outro seguia servindo o nome antigo por tempo
# indeterminado. Threads compartilham a memoria, entao a atualizacao vale para
# todas as requisicoes, e ainda assim atende mais de uma por vez.
CMD ["gunicorn", "--preload", "-w", "1", "--threads", "4", "--timeout", "120", "-b", "0.0.0.0:8000", "app:app"]
