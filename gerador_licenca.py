"""Compatibilidade do antigo módulo de geração.

A emissão de licenças FRS Mercado passou a usar exclusivamente o ambiente
privado oficial do FRS, com assinatura Ed25519. Este arquivo deliberadamente não
contém segredos, salts ou chaves de emissão.

Use somente a ferramenta do ambiente privado oficial do FRS.
"""

raise RuntimeError(
    "Geração legada desativada. Use o Gerador Oficial FRS Mercado externo."
)


def generate_license_key(*args, **kwargs):
    """API legada removida; não emite nem valida chaves."""
    raise RuntimeError(
        "Geração legada desativada. Use o Gerador Oficial FRS Mercado externo."
    )


if __name__ == "__main__":
    raise SystemExit(
        "Geração legada desativada. Use o ambiente privado oficial do FRS."
    )