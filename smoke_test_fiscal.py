import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from database_manager import get_db_path
from modulo_config import carregar_config_fiscal, exibir_configuracoes, salvar_config_fiscal


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

    api_key_teste = "SMOKE-KEY-20260613"
    ambiente_teste = "PRODUCAO"

    with log_file.open("w", encoding="utf-8") as fh:
        log("INICIO DO SMOKE TEST FISCAL", fh)

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

        # Etapa 2: Preencher API key e alternar ambiente (persistência via funções do módulo).
        log("Etapa 2/5: Salvando nova API Key e Ambiente em config_fiscal...", fh)
        try:
            salvar_config_fiscal(api_key_teste, ambiente_teste)
            cfg = carregar_config_fiscal()
            ok_cfg = cfg.get("api_key") == api_key_teste and cfg.get("ambiente") == ambiente_teste
            log(f"Resultado salvar/ler config_fiscal: {'OK' if ok_cfg else 'FALHA'} | {cfg}", fh)
        except Exception as e:
            log(f"ERRO ao salvar/ler config_fiscal: {e}", fh)
            return 2

        if not ok_cfg:
            log("SMOKE TEST REPROVADO: dados não retornaram após salvar via módulo.", fh)
            print(log_file)
            return 2

        # Etapa 3: Validar persistência diretamente no banco.
        log("Etapa 3/5: Validando persistência em banco (tabela config_fiscal)...", fh)
        try:
            db_path = get_db_path()
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT api_key, ambiente FROM config_fiscal WHERE id = 1").fetchone()
            conn.close()
            ok_db = bool(row) and row[0] == api_key_teste and row[1] == ambiente_teste
            log(f"Resultado persistência banco: {'OK' if ok_db else 'FALHA'} | row={row}", fh)
        except Exception as e:
            log(f"ERRO ao consultar banco: {e}", fh)
            return 2

        if not ok_db:
            log("SMOKE TEST REPROVADO: banco não contém os valores esperados.", fh)
            print(log_file)
            return 2

        # Etapa 4: Simular reabertura do sistema em novo processo.
        log("Etapa 4/5: Simulando reabertura do sistema (novo processo Python)...", fh)
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
            ok_reopen = cfg_reopen.get("api_key") == api_key_teste and cfg_reopen.get("ambiente") == ambiente_teste
            log(f"Resultado reabertura: {'OK' if ok_reopen else 'FALHA'} | {cfg_reopen}", fh)
        except Exception as e:
            log(f"ERRO na simulação de reabertura: {e}", fh)
            return 2

        if not ok_reopen:
            log("SMOKE TEST REPROVADO: dados não persistiram após reabertura.", fh)
            print(log_file)
            return 2

        # Etapa 5: Resultado final
        log("Etapa 5/5: Consolidação final do teste.", fh)
        log("SMOKE TEST APROVADO: Fluxo fiscal validado com sucesso.", fh)

    print(log_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
