import inspect
import json
from datetime import datetime, timedelta
from pathlib import Path

from modulo_main import AppPrincipal


def _new_app_for_test():
    app = AppPrincipal.__new__(AppPrincipal)
    app.sistema_pronto = True
    app.admin_cadastrado = True
    app._mentoria_agendada = False
    app._mentora_exibida_sessao = False
    app._poll_venda_ativa = False
    app._vendas_iniciais = 0
    app.usuario_atual = {"permissao": "Administrador"}
    app.winfo_exists = lambda: True
    return app


def teste_boot_limpo():
    app = _new_app_for_test()
    agendamentos = []

    app._obter_total_vendas_dia = lambda: 0

    def registrar_after(ms, command):
        agendamentos.append((ms, getattr(command, "__name__", str(command))))
        return len(agendamentos)

    app._registrar_after = registrar_after
    app._agendar_mentoria_ia()

    nomes = [n for _, n in agendamentos]
    delays = [d for d, _ in agendamentos]

    ok = (
        app._mentoria_agendada
        and "_monitorar_primeira_venda" in nomes
        and "_gatilho_tempo_mentora" in nomes
        and 600000 in delays
    )
    detalhe = f"agendamentos={agendamentos}"
    return ok, detalhe


def teste_gatilho_primeira_venda():
    app = _new_app_for_test()
    app._poll_venda_ativa = True
    app._vendas_iniciais = 0
    app._obter_total_vendas_dia = lambda: 1

    chamadas = []
    reagendado = []

    app.checar_mentoria_ia = lambda origem="manual": chamadas.append(origem)
    app._registrar_after = lambda ms, fn: reagendado.append((ms, getattr(fn, "__name__", str(fn))))

    app._monitorar_primeira_venda()

    ok = chamadas == ["primeira_venda"] and not app._poll_venda_ativa
    detalhe = f"chamadas={chamadas}; reagendado={reagendado}"
    return ok, detalhe


def teste_trava_24h_reabertura():
    app1 = _new_app_for_test()
    app2 = _new_app_for_test()

    cfg_path = Path("tmp_config_mentora_ux.json")
    app1._get_mentora_config_path = lambda: cfg_path
    app2._get_mentora_config_path = lambda: cfg_path

    try:
        # Cenário A: passou mais de 24h -> pode exibir
        app1._salvar_config_mentora(
            {"last_shown_date": (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")}
        )
        pode_apos_25h = app1._pode_exibir_mentora_hoje()

        # Cenário B: exibiu agora, fecha e abre novamente -> não pode exibir
        app1._registrar_exibicao_mentora()
        app2._mentora_exibida_sessao = False
        pode_reabrindo_hoje = app2._pode_exibir_mentora_hoje()

        ok = pode_apos_25h and not pode_reabrindo_hoje
        detalhe = f"pode_apos_25h={pode_apos_25h}; pode_reabrindo_hoje={pode_reabrindo_hoje}"
        return ok, detalhe
    finally:
        cfg_path.unlink(missing_ok=True)


def teste_estabilidade_na_tela():
    src = inspect.getsource(AppPrincipal.exibir_relatorio_mentoria)

    sem_bloqueio = "grab_set(" not in src and "withdraw(" not in src
    tem_toplevel = "CTkToplevel" in src
    tem_transient = "transient(self)" in src
    tem_botao_entendi = 'text="Entendi"' in src

    ok = sem_bloqueio and tem_toplevel and tem_transient and tem_botao_entendi
    detalhe = (
        f"sem_bloqueio={sem_bloqueio}; toplevel={tem_toplevel}; "
        f"transient={tem_transient}; botao_entendi={tem_botao_entendi}"
    )
    return ok, detalhe


def main():
    resultados = []

    checks = [
        ("Boot limpo", teste_boot_limpo),
        ("Gatilho após primeira venda", teste_gatilho_primeira_venda),
        ("Trava 24h na reabertura", teste_trava_24h_reabertura),
        ("Estabilidade com Mentora aberta", teste_estabilidade_na_tela),
    ]

    for nome, fn in checks:
        ok, detalhe = fn()
        resultados.append((nome, ok, detalhe))

    print("CHECKLIST UX MENTORA - RESULTADO")
    print("=" * 80)
    for nome, ok, detalhe in resultados:
        print(f"{nome}: {'APROVADO' if ok else 'REPROVADO'}")
        print(f"  Detalhe: {detalhe}")

    final_ok = all(ok for _, ok, _ in resultados)
    print("=" * 80)
    print(f"STATUS FINAL: {'APROVADO' if final_ok else 'REPROVADO'}")
    raise SystemExit(0 if final_ok else 1)


if __name__ == "__main__":
    main()
