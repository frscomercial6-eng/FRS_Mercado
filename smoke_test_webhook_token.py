from webhook_security import salvar_token_webhook, validar_token_webhook


def main():
    token = "TOKEN-TESTE-SEGURANCA-20260613"
    assert salvar_token_webhook(token) is True

    ok, _msg = validar_token_webhook(token)
    assert ok, "Token correto deveria validar"

    ok2, _msg2 = validar_token_webhook("TOKEN-INVALIDO")
    assert not ok2, "Token inválido deveria ser rejeitado"

    print("SMOKE WEBHOOK TOKEN OK")


if __name__ == "__main__":
    main()
