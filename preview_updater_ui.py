"""Pré-visualização ISOLADA da interface de atualização (Etapa 4C).

ATENÇÃO: isto é uma DEMONSTRAÇÃO/TESTE. Não é o fluxo de produção.

* Usa versões e bytes SINTÉTICOS (nenhum dado de cliente).
* NÃO faz rede, NÃO baixa pacote real, NÃO cria job de atualização,
  NÃO executa o helper, NÃO altera versão instalada, NÃO altera manifesto.
* NÃO é importado por ``main.py``/``modulo_main.py`` nem por ``build_exe.py``:
  nunca é acionado em uma compilação normal para clientes. Só roda quando
  executado diretamente: ``python preview_updater_ui.py``.

Serve para visualizar a janela de progresso e seus estados sem depender de um
manifesto 1.0.22 publicado.
"""
from __future__ import annotations

import os
import sys
import tempfile

# Isolamento defensivo de dados: garante que, mesmo se algo tentar tocar o
# caminho de dados, vá para um APPDATA temporário e descartável.
os.environ.setdefault("APPDATA", tempfile.mkdtemp(prefix="frs-preview-"))

from updater_ui import ESTADOS, JanelaProgressoAtualizacao  # noqa: E402

VERSAO_ORIGEM_DEMO = "1.0.21"
VERSAO_DESTINO_DEMO = "1.0.22"
TAMANHO_DEMO = 150 * 1024 * 1024  # 150 MB sintéticos


def _principal():
    import customtkinter as ctk

    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.geometry("520x240")
    root.title("[DEMONSTRAÇÃO] Interface de atualização - FRS Mercado")

    aviso = ctk.CTkLabel(
        root,
        text=(
            "PRÉ-VISUALIZAÇÃO / TESTE\n"
            "Dados e versões sintéticos. Sem rede, sem job, sem helper.\n"
            f"{VERSAO_ORIGEM_DEMO} -> {VERSAO_DESTINO_DEMO}"
        ),
        text_color="#E0A800",
    )
    aviso.pack(padx=16, pady=(14, 6))

    estado_var = ctk.StringVar(value="Clique em um estado para demonstrar.")

    def abrir(janela_ref):
        janela_ref[0] = JanelaProgressoAtualizacao(
            root, versao_origem=VERSAO_ORIGEM_DEMO, versao_destino=VERSAO_DESTINO_DEMO
        )
        estado_var.set("Janela de progresso aberta (demonstração).")

    botoes = ctk.CTkFrame(root)
    botoes.pack(padx=16, pady=8, fill="x")

    janela_ref = [None]

    def _btn(texto, comando):
        return ctk.CTkButton(botoes, text=texto, command=comando)

    _btn("Abrir janela", lambda: abrir(janela_ref)).pack(side="left", padx=4, pady=4)
    _btn("Baixando 40%", lambda: janela_ref[0] and (
        janela_ref[0].definir_estado("baixando"),
        janela_ref[0].atualizar_bytes(int(TAMANHO_DEMO * 0.4), TAMANHO_DEMO),
    )).pack(side="left", padx=4, pady=4)
    _btn("Verificando", lambda: janela_ref[0] and janela_ref[0].definir_estado("verificando")).pack(side="left", padx=4, pady=4)
    _btn("Tamanho desconhecido", lambda: janela_ref[0] and (
        janela_ref[0].definir_estado("baixando"),
        janela_ref[0].indeterminado(),
    )).pack(side="left", padx=4, pady=4)
    _btn("Concluído", lambda: janela_ref[0] and janela_ref[0].concluir()).pack(side="left", padx=4, pady=4)
    _btn("Falha", lambda: janela_ref[0] and janela_ref[0].falhar("erro simulado (demo)")).pack(side="left", padx=4, pady=4)
    _btn("Fechar", lambda: janela_ref[0] and janela_ref[0].fechar()).pack(side="left", padx=4, pady=4)

    ctk.CTkLabel(root, textvariable=estado_var).pack(padx=16, pady=(4, 12))
    root.mainloop()


if __name__ == "__main__":
    print(f"[DEMONSTRAÇÃO] Estados disponíveis: {sorted(ESTADOS)}")
    print("[DEMONSTRAÇÃO] Nenhuma rede, job, helper, versão ou manifesto é tocado.")
    _principal()
    sys.exit(0)
