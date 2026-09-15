import hashlib
from datetime import datetime, timedelta

# A chave secreta deve permanecer somente com o emissor das licenças.
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

    identifier = client_identifier.strip().upper()
    hash_val = hashlib.sha256(f"{identifier}-{expiration_date_str}-{SECRET_SALT}".encode()).hexdigest()
    return f"LICENCA_FRS:{expiration_date_str}-{hash_val[:16]}"

if __name__ == "__main__":
    print("--- Gerador de Código de Ativação FRS ---")
    
    client_id = input("Digite o ID do Cliente ou Razão Social (ex: 'MERCADO DO ZE'): ").strip()
    if not client_id:
        print("O ID do Cliente/Razão Social não pode ser vazio.")
        exit()

    print("\nATENÇÃO: O código gerado abaixo renovará a licença por 365 dias a partir da data em que o cliente ativá-lo.")
    
    vencimento = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
    license_key = generate_license_key(client_id, vencimento)
    print(f"\nCódigo de Ativação Gerado para '{client_id}':")
    print(license_key)
    print("\nEnvie o código completo, incluindo 'LICENCA_FRS:'.")