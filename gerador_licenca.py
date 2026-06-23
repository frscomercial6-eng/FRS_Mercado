import hashlib
from datetime import datetime, timedelta

# A chave secreta que só você tem. Mantenha-a EXTREMAMENTE SEGURA e não a compartilhe!
# Em um ambiente de produção, isso seria carregado de forma mais robusta (ex: variável de ambiente, KMS).
SECRET_SALT = "MinhaChaveSecretaSuperSeguraFRS2024!"

def generate_license_key(client_identifier: str, expiration_date_str: str) -> str:
    """
    Gera um código de ativação único (hash) combinando o identificador do cliente,
    a data de expiração e um salt secreto.
    """
    # Valida o formato da data para garantir consistência
    try:
        datetime.strptime(expiration_date_str, '%Y-%m-%d')
    except ValueError:
        raise ValueError("Formato da data de expiração inválido. Use YYYY-MM-DD.")

    # Concatena os dados e o salt, convertendo para maiúsculas para evitar problemas de case-sensitivity
    hash_val = hashlib.sha256(f"{client_identifier.strip().upper()}-{expiration_date_str}-{SECRET_SALT}".encode()).hexdigest()
    # A chave final contém a data para que o sistema saiba quando expira
    return f"{expiration_date_str}-{hash_val[:16]}"

if __name__ == "__main__":
    print("--- Gerador de Código de Ativação FRS ---")
    
    client_id = input("Digite o ID do Cliente ou Razão Social (ex: 'MERCADO DO ZE'): ").strip()
    if not client_id:
        print("O ID do Cliente/Razão Social não pode ser vazio.")
        exit()

    # Para este exemplo, o código de ativação renovará a licença por 365 dias a partir da data de ativação.
    # Portanto, a data de vencimento que você insere aqui é apenas para sua referência ou para um modelo diferente.
    # A lógica de validação no login usará a data de ativação + 365 dias.
    print("\nATENÇÃO: O código gerado abaixo renovará a licença por 365 dias a partir da data em que o cliente ativá-lo.")
    print("A data de vencimento que você pode inserir aqui é apenas para sua referência ou para um modelo de licença diferente.")
    
    vencimento = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
    license_key = generate_license_key(client_id, vencimento)
    print(f"\nCódigo de Ativação Gerado para '{client_id}':")
    print(f"LICENCA_FRS:{license_key}") # Prefixo para facilitar identificação
    print("\nCopie APENAS o hash (a parte após 'LICENCA_FRS:') e envie ao seu cliente.")