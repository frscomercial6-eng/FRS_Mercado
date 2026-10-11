"""Checklist operacional — SMOKE TEST ISOLADO (Item 13 / B1).

POR QUE ESTE ARQUIVO NÃO PODE APAGAR O BANCO DO CLIENTE
----------------------------------------------------
Até o Item 13, `main()` chamava `_reset_db()`, que fazia
`Path(get_db_path()).unlink()`. Como `get_db_path()` resolve para
`%APPDATA%\\FRS_Mercado\\data\\mercado.db`, rodar este checklist na máquina do
Mário apagaria vendas, estoque, financeiro, licença e cadastros. O roteiro era
automatizado e não exigia confirmação nenhuma.

O QUE MUDA AQUI
---------------
1. O checklist passa a exigir um banco de teste ISOLADO. `_reset_db()` aborta
   (não apaga nada) se o banco resolvido estiver fora de uma raiz temporária.
2. A raiz de dados de todo o roteiro passa a ser um diretório temporário criado
   por este módulo, de modo que vendas/usuários/licença criadas pelos cenários
   também não toquem o banco real.
3. `carregar_configuracoes` é importado por referência (lazy) para que a
   troca de ambiente não desvie do dublê de teste.

OS TRÊS CENÁRIOS QUE REPROVAVAM (correção de HARNESS, não de produto)
--------------------------------------------------------------------
4. "Venda no PDV" montava o objeto de teste com `ModuloPDV.__new__` e três
   atributos apenas. `finalizar_venda_pdv` também usa `calculadora_tributaria`,
   os widgets de limpeza pós-venda e o `caixa_id`; sem eles a venda estourava
   `AttributeError`. O objeto agora é montado por `_pdv_de_teste()`, com a
   mesma `CalculadoraTributaria` que `ModuloPDV.__init__` usa. O item vendido
   também deixou de ser `"id": 1` fixo — `itens_venda.produto_id` tem FK para
   `produtos(id)` com `PRAGMA foreign_keys = ON`, então o produto é criado de
   verdade no banco temporário (`_criar_produto_teste`).
5. "Fechamento de Caixa" abria um caixa NOVO, depois da venda. A venda ficava
   sem `caixa_operacao_id` e a `data_abertura` do caixa era posterior à
   `data_venda`: a venda saía da janela do ciclo, `fechar_caixa` não
   consolidava nada e o cenário reprovava. Agora `_abrir_caixa_teste()` abre o
   caixa ANTES da venda e o `caixa_id` viaja no contexto compartilhado.
6. "Disparo de e-mail de erro" exigia um envio SMTP SÍNCRONO que não existe:
   `notify_error` apenas enfileira o item para a thread de telemetria. O
   cenário passou a validar o caminho assíncrono real (fila consumida, destino
   de suporte, conteúdo e log em disco), sem rede e sem SMTP.
"""
import hashlib
import os
import tempfile
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# --- Ambiente isolado, definido ANTES de qualquer import que resolva caminhos.
# Esta atribuição é Incondicional: não há flag, prompt ou confirmação que a
# desative, e é exatamente por isso que a proteção é estrutural.
_RAIZ_TESTE = tempfile.mkdtemp(prefix="frs-checklist-operacional-")
os.environ["APPDATA"] = _RAIZ_TESTE
os.environ["LOCALAPPDATA"] = _RAIZ_TESTE
os.environ["FRS_CHECKLIST_ROOT"] = _RAIZ_TESTE

import modulo_config as _config_modulo  # noqa: E402

from calculadora_tributaria import CalculadoraTributaria  # noqa: E402
from database_manager import get_db_connection, get_db_path  # noqa: E402
from modulo_pdv import ModuloPDV  # noqa: E402
import modulo_pdv  # noqa: E402
import error_notifier  # noqa: E402

# Importado por referência: os cenários trocam este atributo por um dublê, e
# `modulo_pdv` faz `from modulo_config import carregar_configuracoes`, ou seja,
# a troca precisa ocorrer no próprio módulo de origem.
carregar_configuracoes = _config_modulo.carregar_configuracoes


# ---------------------------------------------------------------------------
# DÍVIDA REGISTRADA (NÃO CORRIGIR NESTA ETAPA — Item 13 fecha o HARNESS)
# ---------------------------------------------------------------------------
# `error_notifier.notify_error` e `error_notifier.send_error_email` aceitam o
# parâmetro `smtp_client_factory`, mas `notify_error` NÃO o repassa para
# `send_error_email`: o item enfileirado é consumido pela thread de telemetria,
# que chama `send_error_email(subject=..., body=..., to_email=...)` sem a
# fábrica. O parâmetro é, portanto, código morto.
#
# REGRA: `error_notifier.py` NÃO é alterado no Item 13. A correção (propagar a
# fábrica pela fila ou remover o parâmetro) é dívida futura. O cenário 5 foi
# reescrito para validar o comportamento assíncrono real, sem depender do
# parâmetro morto.
_DIVERSA_SMTP_CLIENT_FACTORY_MORTO = (
    "error_notifier.notify_error(smtp_client_factory=...) nao e repassado a "
    "send_error_email pela fila de telemetria; parametro morto (divida futura)."
)


class _AmbienteDoClienteError(RuntimeError):
    """O roteiro tentou rodar contra o banco de dados do cliente."""


def _raiz_temporaria(path: Path) -> bool:
    """Diz se `path` vive dentro de uma raiz temporária de TESTE.

    Não basta estar em `%TEMP%`: o próprio banco do cliente também é um arquivo
    comum e pode acabar em disco temporário por cache, cópia ou restauração.
    O que caracteriza o banco de teste é estar sob a raiz isolada que ESTE
    módulo criou, ou sob um diretório temporário nomeado como tal.
    """
    try:
        alvo = Path(os.path.abspath(str(path)))
    except (TypeError, ValueError):
        return False

    candidatos = [_RAIZ_TESTE]
    raiz_teste = os.environ.get("FRS_CHECKLIST_ROOT")
    if raiz_teste:
        candidatos.append(raiz_teste)

    for base in candidatos:
        try:
            raiz = Path(os.path.abspath(base))
        except (TypeError, ValueError):
            continue
        if alvo == raiz or raiz in alvo.parents:
            return True
    return False


def _garantir_banco_isolado() -> Path:
    """Impede qualquer operação destrutiva sobre o banco do cliente.

    Esta é a trava ESTRUTURAL do Item 13: ela não pergunta nada ao operador e
    não depende de `--simular`. Se `get_db_path()` apontar para o banco real,
    a função levanta ANTES de qualquer `unlink`, então o arquivo do cliente
    permanece intacto por construção.
    """
    db_path = Path(get_db_path())
    if not _raiz_temporaria(db_path):
        raise _AmbienteDoClienteError(
            "O checklist foi interrompido antes de tocar qualquer arquivo: "
            f"o banco resolvido não está em uma raiz temporária de teste ({db_path}). "
            "Os dados do cliente nunca podem ser apagados por este roteiro."
        )
    return db_path


class DummyFiscal:
    def exportar_venda(self, venda_id, itens, forma_pgto, valor_bruto):
        return True, f"ok:{venda_id}"


class DummyLabel:
    """Label mínima. Cobre `configure` e o `pack_forget` do aviso de limite."""

    def __init__(self):
        self.text = ""

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]

    def pack(self, *_args, **_kwargs):
        return None

    def pack_forget(self):
        return None

    def winfo_exists(self):
        return True


class DummyEntry:
    """Entry mínima. `finalizar_venda_pdv` limpa `ent_valor_pago` pós-venda."""

    def __init__(self, valor=""):
        self.value = valor

    def get(self):
        return self.value

    def delete(self, _inicio=0, _fim="end"):
        self.value = ""


def _reset_db():
    """Recria um banco de TESTE isolado. Nunca apaga o banco do cliente.

    Se, por qualquer regressão futura, `get_db_path()` voltar a apontar para o
    banco real, `_garantir_banco_isolado()` levanta e o arquivo do cliente não é
    tocado — a proteção não depende de este corpo estar correto.
    """
    db_path = _garantir_banco_isolado()
    db_path.unlink(missing_ok=True)
    with get_db_connection() as conn:
        conn.execute("SELECT 1")


def _registrar_resultado(resultados, nome, aprovado, detalhe):
    resultados.append((nome, aprovado, detalhe))


# ---------------------------------------------------------------------------
# PREPARAÇÃO DO CENÁRIO (Item 13 — correção do HARNESS, não do produto)
# ---------------------------------------------------------------------------
def _criar_produto_teste(conn):
    """Cria o produto REAL do cenário no banco temporário.

    O harness antigo vendia o item com `"id": 1` fixo, sem existir em
    `produtos`. Como `itens_venda.produto_id` tem FK para `produtos(id)` e o
    SQLite roda com `PRAGMA foreign_keys = ON`, a venda era rejeitada. Aqui o
    produto nasce de verdade — com todas as colunas NOT NULL do schema — e o
    `id` devolvido é o que a venda referencia.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO produtos (
            codigo_barras, nome, unidade, ncm,
            aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs,
            preco_custo, margem_lucro, preco_venda,
            quantidade_atual, quantidade_minima
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "789000000001", "PRODUTO TESTE", "UN", "",
            0.0, 0.0, 0.0, 0.0, 0.0,
            5.0, 100.0, 10.0,
            100.0, 0.0,
        ),
    )
    return cur.lastrowid


def _abrir_caixa_teste(conn, saldo_inicial=50.0):
    """Abre o caixa do ciclo ANTES da venda e devolve `caixa_id`.

    O harness antigo abria o caixa DEPOIS da venda, num ciclo diferente. A
    venda ficava sem `caixa_operacao_id` e, quando o caixa era aberto, a
    `data_abertura` ficava DEPOIS da `data_venda`: a venda caía fora da janela
    do ciclo, `fechar_caixa` não consolidava nada em `financeiro` e o cenário
    reprovava sem que houvesse defeito no produto.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO caixa_operacao (saldo_inicial, status) VALUES (?, 'ABERTO')",
        (saldo_inicial,),
    )
    return cur.lastrowid


def _pdv_de_teste(caixa_id=None):
    """Monta o PDV de teste pelo padrão do projeto (`ModuloPDV.__new__` + dublês).

    O harness antigo usava só `itens_carrinho`, `fiscal` e `lbl_troco_venda`.
    `finalizar_venda_pdv` usa muito mais: a calculadora tributária, widgets de
    limpeza pós-venda e o próprio `caixa_id`. Aqui entram TODOS os atributos de
    que o método real precisa — inclusive `CalculadoraTributaria()`, a mesma
    classe que `ModuloPDV.__init__` instancia (linha 121 de `modulo_pdv.py`) e
    que os testes do projeto já usam (`run_tests.py`, `test_pdv_payment_flow`).
    """
    pdv = ModuloPDV.__new__(ModuloPDV)

    pdv.caixa_id = caixa_id
    pdv.calculadora_tributaria = CalculadoraTributaria()
    pdv.fiscal = DummyFiscal()

    # Widgets e pontos de UI tocados pela finalização.
    pdv.ent_valor_pago = DummyEntry()
    pdv.ent_quantidade = DummyEntry()
    pdv.lbl_troco_venda = DummyLabel()
    pdv.lbl_aviso_limite = DummyLabel()

    # Interações externas sem efeito neste roteiro.
    pdv.exportar_venda_fiscal = lambda _dados: True
    pdv._enviar_comando_nfce = lambda *_a, **_k: True
    pdv._executar_automacao_pos_venda = lambda *_a, **_k: None

    # Limpeza pós-venda e retorno de foco (efeito apenas visual).
    pdv._renderizar_carrinho = lambda: None
    pdv.atualizar_total_display = lambda: None
    pdv.limpar_pagamentos_recebidos = lambda: None
    pdv._avaliar_limite_caixa = lambda: None
    pdv._retornar_foco_pdv = lambda: None
    pdv._safe_focus = lambda _widget: None
    pdv._set_status = lambda _mensagem, _cor="#3498db": None
    return pdv


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


def _cenario_venda_pdv(resultados, contexto):
    caixa_id = contexto["caixa_id"]
    produto_id = contexto["produto_id"]
    try:
        pdv = _pdv_de_teste(caixa_id=caixa_id)
        pdv.itens_carrinho = [
            {
                "id": produto_id,
                "barcode": "789000000001",
                "nome": "PRODUTO TESTE",
                "unidade": "UN",
                "preco": 10.0,
                "quantidade": 2,
                "total": 20.0,
            }
        ]

        pdv.finalizar_venda_pdv("DINHEIRO", valor_pago=20.0)

        with get_db_connection() as conn:
            venda = conn.execute(
                "SELECT id, valor_total, forma_pagamento, caixa_operacao_id "
                "FROM vendas ORDER BY id DESC LIMIT 1"
            ).fetchone()
            espelho = conn.execute("SELECT COUNT(*) FROM vendas_dia").fetchone()[0]
            itens = conn.execute(
                "SELECT COUNT(*) FROM itens_venda WHERE produto_id = ?", (produto_id,)
            ).fetchone()[0]
            # `venda_fonte_unica.gravar_venda` grava "Venda #{id} ({forma}) [{origem}]".
            # O harness antigo exigia "Venda PDV #%", padrão que NENHUM módulo do
            # projeto produz — a contagem era sempre zero.
            financeiro = conn.execute(
                "SELECT COUNT(*) FROM financeiro WHERE descricao LIKE 'Venda #%' "
                "AND caixa_operacao_id = ?",
                (caixa_id,),
            ).fetchone()[0]
            estoque = conn.execute(
                "SELECT quantidade_atual FROM produtos WHERE id = ?", (produto_id,)
            ).fetchone()

        venda_ok = venda is not None and float(venda[1] or 0.0) == 20.0
        vinculada = venda is not None and venda[3] == caixa_id
        baixa_ok = estoque is not None and float(estoque[0] or 0.0) == 98.0
        aprovado = (
            venda_ok and vinculada and espelho >= 1 and itens >= 1
            and financeiro >= 1 and baixa_ok
        )
        detalhe = (
            f"venda_id={venda[0] if venda else 'N/A'}; total={venda[1] if venda else 'N/A'}; "
            f"forma={venda[2] if venda else 'N/A'}; caixa={venda[3] if venda else 'N/A'}; "
            f"vendas_dia={espelho}; itens_venda={itens}; financeiro_venda={financeiro}; "
            f"estoque_restante={estoque[0] if estoque else 'N/A'}"
        )
        _registrar_resultado(resultados, "3. Venda no PDV", aprovado, detalhe)
    except Exception as exc:
        _registrar_resultado(resultados, "3. Venda no PDV", False, str(exc))


def _cenario_fechamento_caixa(resultados, contexto):
    """Fecha o MESMO caixa em que a venda do cenário 3 foi registrada.

    O harness antigo criava um caixa NOVO aqui. Como a venda anterior não tinha
    `caixa_operacao_id` e a `data_abertura` do caixa novo era posterior à
    `data_venda`, a venda ficava fora da janela do ciclo: `fechar_caixa`
    consolidava zero e o cenário reprovava. Agora venda e fechamento pertencem
    ao mesmo contexto.
    """
    caixa_id = contexto["caixa_id"]
    original_askyesno = modulo_pdv.messagebox.askyesno
    original_showwarning = modulo_pdv.messagebox.showwarning
    original_showerror = modulo_pdv.messagebox.showerror

    try:
        modulo_pdv.messagebox.askyesno = lambda *args, **kwargs: True
        modulo_pdv.messagebox.showwarning = lambda *args, **kwargs: None
        modulo_pdv.messagebox.showerror = lambda *args, **kwargs: None

        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sangrias (valor, justificativa, caixa_operacao_id) VALUES (?, ?, ?)",
                (5.0, "Teste fechamento", caixa_id),
            )
            saldo_inicial = float(
                conn.execute(
                    "SELECT saldo_inicial FROM caixa_operacao WHERE id = ?", (caixa_id,)
                ).fetchone()[0]
                or 0.0
            )

        pdv = _pdv_de_teste(caixa_id=caixa_id)
        pdv.destroy = lambda: None

        # Pontos de UI do fechamento: o roteiro roda sem tela, então a
        # conferência cega é respondida com o próprio SISTEMA DO CICLO
        # (o mesmo valor que `processar_fechamento_inteligente` calcula menos a
        # abertura, conforme a REGRA 1.0.20). O cenário valida fechamento e
        # consolidação, não a digitação do operador.
        def _conferir(esperado):
            return {
                modalidade: round(
                    float(esperado.get(modalidade, 0.0) or 0.0)
                    - (saldo_inicial if modalidade == "DINHEIRO" else 0.0),
                    2,
                )
                for modalidade in ModuloPDV.MODALIDADES_FECHAMENTO
            }

        pdv._abrir_modal_conferencia_fechamento = _conferir
        pdv._perguntar_impressao_fechamento = lambda: False
        pdv._exibir_resumo_fechamento = lambda _dados, ao_concluir=None: None
        pdv._perguntar_abertura_novo_caixa = lambda: None
        pdv._atualizar_indicadores_caixa = lambda _aberto: None

        pdv.processar_fechamento_inteligente()

        with get_db_connection() as conn:
            status = conn.execute(
                "SELECT status FROM caixa_operacao WHERE id = ?", (caixa_id,)
            ).fetchone()
            vendas_restantes = conn.execute("SELECT COUNT(*) FROM vendas_dia").fetchone()[0]
            consolidado = conn.execute(
                "SELECT COUNT(*) FROM financeiro WHERE descricao LIKE 'Fechamento de Caixa - %' "
                "AND caixa_operacao_id = ?",
                (caixa_id,),
            ).fetchone()[0]
            conference = conn.execute(
                "SELECT COUNT(*) FROM caixa_conferencia WHERE caixa_operacao_id = ?",
                (caixa_id,),
            ).fetchone()[0]
            divergencias = conn.execute(
                "SELECT COALESCE(SUM(ABS(diferenca)), 0) FROM caixa_conferencia "
                "WHERE caixa_operacao_id = ?",
                (caixa_id,),
            ).fetchone()[0]

        aprovado = (
            status is not None and status[0] == "FECHADO"
            and vendas_restantes == 0 and consolidado >= 1
            and conference == len(ModuloPDV.MODALIDADES_FECHAMENTO)
            and float(divergencias or 0.0) == 0.0
        )
        detalhe = (
            f"status={status[0] if status else 'N/A'}; vendas_dia={vendas_restantes}; "
            f"financeiro_fechamento={consolidado}; modalidades_conferidas={conference}; "
            f"divergencia_total={float(divergencias or 0.0):.2f}"
        )
        _registrar_resultado(resultados, "4. Fechamento de Caixa", aprovado, detalhe)
    except Exception as exc:
        _registrar_resultado(resultados, "4. Fechamento de Caixa", False, str(exc))
    finally:
        modulo_pdv.messagebox.askyesno = original_askyesno
        modulo_pdv.messagebox.showwarning = original_showwarning
        modulo_pdv.messagebox.showerror = original_showerror


def _cenario_email_erro(resultados):
    """Valida o comportamento ASSÍNCRONO real de `notify_error`.

    O harness antigo passava `smtp_client_factory=FakeSMTP` e exigia a mensagem
    SMTP enviada de volta de forma síncrona. Isso nunca acontece: `notify_error`
    grava o erro no log e apenas ENFILEIRA o item em `_telemetry_queue`; quem
    envia é a thread `frs-error-telemetry`, que chama `send_error_email` — e o
    parâmetro `smtp_client_factory` NÃO é repassado por essa chamada (dívida
    registrada em `_DIVERSA_SMTP_CLIENT_FACTORY_MORTO`). Por isso o cenário
    observava `envio=True` com `destino=None` e reprovava.

    Aqui o roteiro valida o que o produto REALMENTE faz:
      1. `notify_error` aceita o erro e devolve "enfileirado para telemetria";
      2. a thread de telemetria consome a fila e despacha o envio;
      3. o despacho leva o destino de suporte e o contexto do erro;
      4. o erro fica persistido no `error_log.txt` da raiz isolada.

    A rede é neutralizada por dublês, então o roteiro não depende de internet
    nem de SMTP real.
    """
    dispatched = threading.Event()
    enviados = {}
    original_internet = error_notifier._internet_available
    original_send = error_notifier.send_error_email

    def _sem_rede(_timeout=5):
        return True

    def _envio_simulado(subject, body, to_email=None, smtp_client_factory=None):
        enviados.update({"subject": subject, "body": body, "to": to_email})
        dispatched.set()
        return True, "envio simulado"

    try:
        error_notifier._internet_available = _sem_rede
        error_notifier.send_error_email = _envio_simulado

        try:
            raise RuntimeError("Exceção simulada para teste de e-mail")
        except Exception as exc:
            traceback_texto = traceback.format_exc()
            erro = exc
            ok, msg = error_notifier.notify_error(
                "checklist_operacional",
                erro,
                traceback_texto,
            )

        fila_entregue = dispatched.wait(timeout=30)
        destino = enviados.get("to")
        assunto = enviados.get("subject", "")
        corpo = enviados.get("body", "")

        log_path = error_notifier._error_log_path()
        log_isolado = _raiz_temporaria(log_path)
        log_gravado = log_path.exists() and "Exceção simulada" in log_path.read_text(
            encoding="utf-8", errors="replace"
        )

        aceito = bool(ok) and "background" in str(msg).lower()
        despachado = fila_entregue and destino == error_notifier.SUPPORT_EMAIL
        conteudo_ok = (
            "checklist_operacional" in assunto
            and "Exceção simulada para teste de e-mail" in corpo
        )

        aprovado = aceito and despachado and conteudo_ok and log_gravado and log_isolado
        detalhe = (
            f"aceito={ok}; retorno={msg}; fila_consumida={fila_entregue}; "
            f"destino={destino}; log_gravado={log_gravado}; log_isolado={log_isolado}"
        )
        _registrar_resultado(
            resultados,
            "5. Disparo de e-mail de erro (simulação)",
            aprovado,
            detalhe,
        )
    except Exception as exc:
        _registrar_resultado(resultados, "5. Disparo de e-mail de erro (simulação)", False, str(exc))
    finally:
        error_notifier._internet_available = original_internet
        error_notifier.send_error_email = original_send


def main():
    resultados = []

    # Trava estrutural do Item 13: antes de qualquer cenário (e antes de
    # `_reset_db`), confirmamos que o banco resolvido é de teste. Se um dia
    # `get_db_path()` voltar a apontar para `%APPDATA%\FRS_Mercado\data`, o
    # roteiro encerra aqui — sem apagar e sem escrever no banco do cliente.
    _garantir_banco_isolado()

    _reset_db()
    _cenario_primeiro_uso(resultados)
    _cenario_login(resultados)

    # Venda e fechamento pertencem ao MESMO ciclo de caixa: o caixa é aberto
    # aqui, ANTES da venda, e o `caixa_id` viaja no contexto compartilhado.
    with get_db_connection() as conn:
        caixa_id = _abrir_caixa_teste(conn)
        produto_id = _criar_produto_teste(conn)
    contexto = {"caixa_id": caixa_id, "produto_id": produto_id}

    _cenario_venda_pdv(resultados, contexto)
    _cenario_fechamento_caixa(resultados, contexto)
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
