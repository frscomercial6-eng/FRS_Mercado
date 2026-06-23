import hashlib
import os
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from database_manager import get_db_connection, get_db_path
from modulo_pdv import ModuloPDV
import modulo_pdv
import error_notifier


class DummyFiscal:
    def exportar_venda(self, venda_id, itens, forma_pgto, valor_bruto):
        return True, f"ok:{venda_id}"


class DummyLabel:
    def configure(self, **kwargs):
        return None


class FakeSMTP:
    last_message = None
    last_host = None
    last_port = None

    def __init__(self, host, port, timeout=20):
        FakeSMTP.last_host = host
        FakeSMTP.last_port = port

    def starttls(self):
        return None

    def login(self, user, password):
        if not user or not password:
            raise RuntimeError("Credenciais inválidas")

    def send_message(self, msg):
        FakeSMTP.last_message = msg

    def quit(self):
        return None


def _reset_db():
    db_path = Path(get_db_path())
    db_path.unlink(missing_ok=True)
    with get_db_connection() as conn:
        conn.execute("SELECT 1")


def _registrar_resultado(resultados, nome, aprovado, detalhe):
    resultados.append((nome, aprovado, detalhe))


def _cenario_primeiro_uso(resultados):
    try:
        usuario = "admin"
        senha = "1234"
        senha_hash = hashlib.sha256(senha.encode()).hexdigest()
        data_exp = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO usuarios (nome, senha_hash, permissao) VALUES (?, ?, 'Administrador')",
                (usuario, senha_hash),
            )
            conn.execute("INSERT INTO licenca (data_expiracao) VALUES (?)", (data_exp,))
            count_users = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
            lic = conn.execute("SELECT data_expiracao FROM licenca LIMIT 1").fetchone()

        aprovado = count_users == 1 and lic is not None
        detalhe = f"usuarios={count_users}; licenca={lic[0] if lic else 'ausente'}"
        _registrar_resultado(resultados, "1. Primeiro Uso (criação de Admin)", aprovado, detalhe)
    except Exception as exc:
        _registrar_resultado(resultados, "1. Primeiro Uso (criação de Admin)", False, str(exc))


def _cenario_login(resultados):
    try:
        usuario = "admin"
        senha_hash = hashlib.sha256("1234".encode()).hexdigest()
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT id, nome, permissao FROM usuarios WHERE nome = ? AND senha_hash = ?",
                (usuario, senha_hash),
            ).fetchone()

        aprovado = row is not None and row[2] == "Administrador"
        detalhe = f"auth={'ok' if aprovado else 'falhou'}"
        _registrar_resultado(resultados, "2. Fluxo de Login", aprovado, detalhe)
    except Exception as exc:
        _registrar_resultado(resultados, "2. Fluxo de Login", False, str(exc))


def _cenario_venda_pdv(resultados):
    try:
        pdv = ModuloPDV.__new__(ModuloPDV)
        pdv.itens_carrinho = [
            {
                "id": 1,
                "barcode": "789000000001",
                "nome": "PRODUTO TESTE",
                "preco": 10.0,
                "quantidade": 2,
                "total": 20.0,
            }
        ]
        pdv.fiscal = DummyFiscal()
        pdv.exportar_venda_fiscal = lambda dados: True
        pdv.lbl_troco_venda = DummyLabel()

        pdv.finalizar_venda_pdv("PIX")

        with get_db_connection() as conn:
            vendas = conn.execute("SELECT COUNT(*) FROM vendas_dia").fetchone()[0]
            fin = conn.execute(
                "SELECT COUNT(*) FROM financeiro WHERE descricao LIKE ?",
                ("Venda PDV #%",),
            ).fetchone()[0]

        aprovado = vendas >= 1 and fin >= 1
        detalhe = f"vendas_dia={vendas}; financeiro_venda={fin}"
        _registrar_resultado(resultados, "3. Venda no PDV", aprovado, detalhe)
    except Exception as exc:
        _registrar_resultado(resultados, "3. Venda no PDV", False, str(exc))


def _cenario_fechamento_caixa(resultados):
    original_askyesno = modulo_pdv.messagebox.askyesno
    original_showwarning = modulo_pdv.messagebox.showwarning
    original_showerror = modulo_pdv.messagebox.showerror

    try:
        modulo_pdv.messagebox.askyesno = lambda *args, **kwargs: True
        modulo_pdv.messagebox.showwarning = lambda *args, **kwargs: None
        modulo_pdv.messagebox.showerror = lambda *args, **kwargs: None

        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO caixa_operacao (saldo_inicial, status) VALUES (?, 'ABERTO')", (50.0,))
            caixa_id = cur.lastrowid
            cur.execute(
                "INSERT INTO sangrias (valor, justificativa, caixa_operacao_id) VALUES (?, ?, ?)",
                (5.0, "Teste fechamento", caixa_id),
            )

        pdv = ModuloPDV.__new__(ModuloPDV)
        pdv.caixa_id = caixa_id
        pdv.destroy = lambda: None

        pdv.processar_fechamento_inteligente()

        with get_db_connection() as conn:
            status = conn.execute("SELECT status FROM caixa_operacao WHERE id = ?", (caixa_id,)).fetchone()
            vendas_restantes = conn.execute("SELECT COUNT(*) FROM vendas_dia").fetchone()[0]
            consolidado = conn.execute(
                "SELECT COUNT(*) FROM financeiro WHERE descricao LIKE 'Fechamento de Caixa - %'"
            ).fetchone()[0]

        aprovado = status is not None and status[0] == "FECHADO" and vendas_restantes == 0 and consolidado >= 1
        detalhe = f"status={status[0] if status else 'N/A'}; vendas_dia={vendas_restantes}; financeiro_fechamento={consolidado}"
        _registrar_resultado(resultados, "4. Fechamento de Caixa", aprovado, detalhe)
    except Exception as exc:
        _registrar_resultado(resultados, "4. Fechamento de Caixa", False, str(exc))
    finally:
        modulo_pdv.messagebox.askyesno = original_askyesno
        modulo_pdv.messagebox.showwarning = original_showwarning
        modulo_pdv.messagebox.showerror = original_showerror


def _cenario_email_erro(resultados):
    try:
        os.environ["FRS_SMTP_USER"] = "teste@frs.local"
        os.environ["FRS_SMTP_PASS"] = "senha_teste"
        os.environ["FRS_SMTP_FROM"] = "teste@frs.local"

        try:
            raise RuntimeError("Exceção simulada para teste de e-mail")
        except Exception as exc:
            ok, msg = error_notifier.notify_error(
                "checklist_operacional",
                exc,
                traceback.format_exc(),
                smtp_client_factory=FakeSMTP,
            )

        to_addr = None
        if FakeSMTP.last_message is not None:
            to_addr = FakeSMTP.last_message["To"]

        aprovado = ok and to_addr == error_notifier.SUPPORT_EMAIL
        detalhe = f"envio={ok}; destino={to_addr}; retorno={msg}"
        _registrar_resultado(
            resultados,
            "5. Disparo de e-mail de erro (simulação)",
            aprovado,
            detalhe,
        )
    except Exception as exc:
        _registrar_resultado(resultados, "5. Disparo de e-mail de erro (simulação)", False, str(exc))


def main():
    resultados = []

    _reset_db()
    _cenario_primeiro_uso(resultados)
    _cenario_login(resultados)
    _cenario_venda_pdv(resultados)
    _cenario_fechamento_caixa(resultados)
    _cenario_email_erro(resultados)

    print("CHECKLIST OPERACIONAL - RESULTADO")
    print("=" * 80)
    for nome, aprovado, detalhe in resultados:
        status = "APROVADO" if aprovado else "REPROVADO"
        print(f"{nome}: {status}")
        print(f"  Detalhe: {detalhe}")

    todos_aprovados = all(ap for _, ap, _ in resultados)
    print("=" * 80)
    print(f"STATUS FINAL: {'APROVADO' if todos_aprovados else 'REPROVADO'}")

    raise SystemExit(0 if todos_aprovados else 1)


if __name__ == "__main__":
    main()
