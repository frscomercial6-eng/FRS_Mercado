"""
Smoke test fiscal — SOMENTE LEITURA.

Não emite nota fiscal, não chama o ACBrMonitor, não grava no banco e não
acessa dados do cliente. A integração PlugNotas foi removida; o que resta é
conferir, em modo leitura, que a configuração fiscal (ACBr) e a do webhook
continuam íntegras e carregáveis.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from database_manager import get_db_path
from modulo_config import (
    carregar_config_fiscal,
    carregar_configuracoes,
    exibir_configuracoes,
    webhook_token_configurado,
)


# Colunas legadas do PlugNotas que NÃO podem sumir nesta fase.
COLUNAS_LEGADAS_ESPERADAS = ("api_key", "ambiente", "webhook_token_hash")

# Chaves do ACBr em config.json que precisam continuar presentes.
CHAVES_ACBR_ESPERADAS = ("fiscal_ativo", "emissor_fiscal_path")


def log(msg, fh):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    fh.write(line + "\n")
    fh.flush()


def main():
    root_dir = Path(__file__).resolve().parent
    log_dir = root_dir / "installer"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "smoke_test_fiscal.log"

    with log_file.open("w", encoding="utf-8") as fh:
        log("INICIO DO SMOKE TEST FISCAL (somente leitura)", fh)

        # Etapa 1: Abrir CONFIGS (janela) e fechar para validar que a tela carrega.
        log("Etapa 1/5: Abrindo janela de CONFIGS...", fh)
        opened = False
        try:
            root = ctk.CTk()
            root.withdraw()
            janela = exibir_configuracoes()
            if janela is not None and janela.winfo_exists():
                opened = True
                janela.update_idletasks()
                janela.destroy()
            root.destroy()
            log(f"Resultado abertura CONFIGS: {'OK' if opened else 'FALHA'}", fh)
        except Exception as e:
            log(f"ERRO ao abrir CONFIGS: {e}", fh)

        if not opened:
            log("SMOKE TEST REPROVADO: janela de CONFIGS não abriu corretamente.", fh)
            print(log_file)
            return 2

        # Etapa 2: config.json carrega e preserva as chaves do ACBr.
        log("Etapa 2/5: Lendo config.json (chaves do ACBr)...", fh)
        try:
            config = carregar_configuracoes()
            faltando = [chave for chave in CHAVES_ACBR_ESPERADAS if chave not in config]
            log(
                f"Resultado config.json: {'OK' if not faltando else 'FALHA'} | ausentes={faltando}",
                fh,
            )
        except Exception as e:
            log(f"ERRO ao ler config.json: {e}", fh)
            return 2

        if faltando:
            log("SMOKE TEST REPROVADO: chaves fiscais do ACBr ausentes em config.json.", fh)
            print(log_file)
            return 2

        # Etapa 3: config_fiscal e as colunas legadas continuam no schema.
        log("Etapa 3/5: Conferindo schema legado de config_fiscal (leitura)...", fh)
        try:
            conn = sqlite3.connect(f"file:{get_db_path()}?mode=ro", uri=True)
            try:
                tabelas = {
                    linha[0]
                    for linha in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                tem_tabela = "config_fiscal" in tabelas
                colunas = (
                    [linha[1] for linha in conn.execute("PRAGMA table_info(config_fiscal)")]
                    if tem_tabela
                    else []
                )
            finally:
                conn.close()
            ausentes = [c for c in COLUNAS_LEGADAS_ESPERADAS if c not in colunas]
            ok_schema = tem_tabela and not ausentes
            log(
                f"Resultado schema config_fiscal: {'OK' if ok_schema else 'FALHA'} | "
                f"tabela={tem_tabela} ausentes={ausentes}",
                fh,
            )
        except Exception as e:
            log(f"ERRO ao inspecionar config_fiscal: {e}", fh)
            return 2

        if not ok_schema:
            log("SMOKE TEST REPROVADO: estrutura legada de config_fiscal alterada.", fh)
            print(log_file)
            return 2

        # Etapa 4: leitura da configuração fiscal e do status do webhook.
        log("Etapa 4/5: Lendo config_fiscal e status do webhook...", fh)
        try:
            fiscal = carregar_config_fiscal()
            token_configurado = webhook_token_configurado()
            log(
                f"Resultado leitura fiscal: OK | ambiente={fiscal.get('ambiente')} "
                f"webhook_configurado={token_configurado}",
                fh,
            )
        except Exception as e:
            log(f"ERRO ao ler config_fiscal/webhook: {e}", fh)
            return 2
        # Etapa 5: reabertura em novo processo, confirmando que a leitura
        # da configuração fiscal/webhook continua funcionando.
        log("Etapa 5/5: Simulando reabertura do sistema (novo processo)...", fh)
        try:
            cmd = [
                sys.executable,
                "-c",
                (
                    "import json; "
                    "from modulo_config import carregar_config_fiscal; "
                    "print(json.dumps(carregar_config_fiscal(), ensure_ascii=False))"
                ),
            ]
            proc = subprocess.run(
                cmd,
                cwd=str(root_dir),
                capture_output=True,
                text=True,
                check=True,
            )
            stdout = proc.stdout.strip().splitlines()
            last = stdout[-1] if stdout else "{}"
            cfg_reopen = json.loads(last)
            ok_reopen = "webhook_token_configurado" in cfg_reopen
            log(f"Resultado reabertura: {'OK' if ok_reopen else 'FALHA'} | {cfg_reopen}", fh)
        except Exception as e:
            log(f"ERRO na simulação de reabertura: {e}", fh)
            return 2

        if not ok_reopen:
            log("SMOKE TEST REPROVADO: leitura da configuração fiscal não funcionou.", fh)
            print(log_file)
            return 2

        log("SMOKE TEST APROVADO: fluxo fiscal validado com sucesso.", fh)

    print(log_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
