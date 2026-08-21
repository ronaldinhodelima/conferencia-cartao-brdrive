import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.py conecta no Postgres em varios pontos no import (migrate(),
# recarregar_categorias_db()) mas engole qualquer excecao - sem essas envs,
# ele so imprime um aviso e segue com estado padrao (CATEGORIA_PT_DB vazio
# etc). Isso e o que permite importar o app de verdade aqui sem banco.
