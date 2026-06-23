import sqlite3
import os
# Importa a função de conexão e o caminho do banco de dados do database_manager
# para garantir que a conexão seja feita de forma consistente com o resto do sistema.
from database_manager import get_db_connection, DB_PATH

def reset_admin_users():
    """
    Conecta ao banco de dados e limpa a tabela 'usuarios'.
    Isso força o sistema a entrar no modo de 'Setup Inicial' no próximo login.
    """
    print(f"Iniciando o processo de reset da tabela 'usuarios' no banco de dados: {DB_PATH}")
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM usuarios;")
        print("Tabela de usuários limpa com sucesso. Reinicie o sistema para um novo cadastro.")
    except Exception as e:
        print(f"ERRO: Não foi possível limpar a tabela de usuários. Detalhes: {e}")
        print("Certifique-se de que o arquivo do banco de dados não está corrompido ou em uso exclusivo por outra aplicação.")

if __name__ == "__main__":
    reset_admin_users()