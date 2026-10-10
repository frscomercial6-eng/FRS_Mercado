"""Interface de progresso da atualização do FRS Mercado (Etapa 4C).

Objetivo
--------
Oferecer uma janela de progresso REAL para o download/preparação da
atualização, ligada aos bytes efetivamente recebidos pelo callback de
``updater_secure._download_asset``. Nenhum progresso é inventado: quando o
tamanho total é desconhecido, a barra fica indeterminada e nenhum percentual
fictício é exibido.

Regras de desenho
-----------------
* Toda mutação de widget acontece na thread da interface. As threads de
  trabalho chamam apenas métodos públicos que AGENDAM a atualização pelo
  ``agendador`` injetado (por padrão ``janela.after`` - o mesmo mecanismo
  seguro já adotado pelo projeto em ``Updater._run_on_ui``).
* Após o fechamento, callbacks pendentes são ignorados (flag ``_fechada`` +
  verificação ``winfo_exists``), evitando tocar em widgets destruídos.
* A lógica pura (``calcular_exibicao``, ``ESTADOS``,
  ``mensagem_fechamento_reabertura``) não depende de Tk e é testável sem
  display.

Este módulo NÃO faz rede, NÃO cria job, NÃO executa o helper, NÃO altera
versão/manifesto e NÃO contorna assinatura. A pré-visualização
(``preview_updater_ui.py``) é separada e jamais é acionada pelo fluxo de
produção.
"""
from __future__ import annotations

# Estados textuais exibidos ao usuário. Cobrem o ciclo completo pedido:
# preparando -> baixando -> verificando -> aguardando encerramento -> concluído,
# além de erro e cancelado.
ESTADOS = {
    "preparando": "Preparando atualização...",
    "baixando": "Baixando atualização...",
    "verificando": "Verificando integridade do pacote...",
    "aguardando": "Aguardando encerramento seguro do sistema...",
    "concluido": "Atualização preparada. Será instalada no encerramento seguro.",
    "erro": "Falha ao preparar a atualização.",
    "cancelado": "Atualização cancelada.",
}


def _fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def calcular_exibicao(recebido: int, total: int) -> dict:
    """Calcula o modo de exibição da barra a partir de bytes REAIS.

    Retorna um dicionário com:
      * ``modo``: ``"determinado"`` ou ``"indeterminado"``;
      * ``fracao``: valor 0..1 para a barra (``None`` se indeterminado);
      * ``texto``: rótulo do percentual/tamanho.

    Quando ``total`` é 0/negativo/desconhecido, devolve modo indeterminado e
    NUNCA inventa um percentual.
    """
    try:
        recebido = int(recebido or 0)
    except (TypeError, ValueError):
        recebido = 0
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        total = 0
    if recebido < 0:
        recebido = 0
    if total <= 0:
        return {"modo": "indeterminado", "fracao": None, "texto": "tamanho desconhecido"}
    fracao = min(1.0, recebido / float(total))
    return {
        "modo": "determinado",
        "fracao": fracao,
        "texto": f"{int(fracao * 100)}%  ({_fmt_mb(recebido)} / {_fmt_mb(total)})",
    }


def mensagem_fechamento_reabertura(versao_destino: str) -> str:
    """Texto exibido antes de encerrar para instalar a atualização.

    Reflete o comportamento REAL: o app fecha, o helper aguarda a saída,
    faz health-check e reabre na tela de login. A reabertura automática pode
    falhar; por isso NÃO é apresentada como garantia incondicional - há
    instruções de recuperação explícitas.
    """
    return (
        "O FRS Mercado será fechado temporariamente para concluir a atualização "
        f"para a versão {versao_destino}. Em seguida, o programa será reaberto "
        "automaticamente na tela normal de login. Aguarde a reabertura e não "
        "inicie outra instância durante esse processo.\n\n"
        "Se a reabertura automática não ocorrer, abra o FRS Mercado novamente "
        "pelo atalho de sempre. Seus dados (cadastros, configurações e licença) "
        "foram preservados."
    )
class JanelaProgressoAtualizacao:
    """Janela de progresso thread-safe da atualização.

    Parâmetros
    ----------
    parent:
        Janela Tk pai (``None`` apenas em testes headless).
    versao_origem, versao_destino:
        Versões exibidas no topo (origem -> destino).
    agendador:
        ``callable(fn)`` que executa ``fn`` na thread da interface. Quando
        omitido e há widget real, usa ``self._janela.after(0, fn)``.
    fabrica:
        ``dict`` com handles de widget para injetar em testes
        (``{"janela","barra","rotulo_estado","rotulo_info"}``). Quando
        fornecido, nenhum widget real é criado.
    """

    def __init__(self, parent=None, versao_origem="?", versao_destino="?",
                 agendador=None, fabrica=None):
        self.parent = parent
        self.versao_origem = str(versao_origem or "?")
        self.versao_destino = str(versao_destino or "?")
        self._fechada = False
        self._estado_atual = "preparando"
        self._fabrica = fabrica
        self._janela = None
        self._barra = None
        self._rotulo_estado = None
        self._rotulo_info = None
        self._agendador = agendador
        self._construir()

    def _construir(self):
        if self._fabrica is not None:
            # Injeção de widgets falsos (testes sem display).
            self._janela = self._fabrica.get("janela")
            self._barra = self._fabrica.get("barra")
            self._rotulo_estado = self._fabrica.get("rotulo_estado")
            self._rotulo_info = self._fabrica.get("rotulo_info")
            return
        import customtkinter as ctk
        self._janela = ctk.CTkToplevel(self.parent)
        self._janela.title("Atualização do FRS Mercado")
        self._janela.geometry("480x180")
        try:
            self._janela.resizable(False, False)
            self._janela.attributes("-topmost", True)
        except Exception:
            pass
        titulo = ctk.CTkLabel(
            self._janela,
            text=f"Atualizando de {self.versao_origem} para {self.versao_destino}",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        titulo.pack(padx=18, pady=(16, 6))
        self._rotulo_estado = ctk.CTkLabel(self._janela, text=ESTADOS["preparando"])
        self._rotulo_estado.pack(padx=18, pady=(0, 8))
        self._barra = ctk.CTkProgressBar(self._janela, width=440)
        self._barra.set(0.0)
        self._barra.pack(padx=18, pady=(0, 6))
        self._rotulo_info = ctk.CTkLabel(self._janela, text="")
        self._rotulo_info.pack(padx=18, pady=(0, 12))
        if self._agendador is None:
            self._agendador = lambda fn: self._janela.after(0, fn)

    def _agendar(self, fn):
        if self._fechada:
            return
        try:
            if self._agendador is not None:
                self._agendador(fn)
            else:
                fn()
        except Exception:
            # Widget possivelmente destruído entre o agendamento e a execução.
            pass

    def _widget_vivo(self, widget):
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    @property
    def fechada(self) -> bool:
        return self._fechada


    # ------------------------------------------------------------------
    # API pública (chamada por qualquer thread)
    # ------------------------------------------------------------------
    def definir_estado(self, chave: str):
        self._estado_atual = chave
        texto = ESTADOS.get(chave, chave)
        self._agendar(lambda: self._aplicar_estado(texto))

    def atualizar_bytes(self, recebido: int, total: int):
        self._agendar(lambda: self._aplicar_bytes(recebido, total))

    def indeterminado(self):
        self._agendar(self._aplicar_indeterminado)

    def concluir(self):
        self._agendar(self._aplicar_concluido)

    def falhar(self, mensagem: str = ""):
        self._agendar(lambda: self._aplicar_falha(mensagem))

    def fechar(self):
        # Agenda a destruicao ANTES de marcar fechada (o agendador normal
        # ignoraria callbacks quando _fechada=True). Usa o agendador direto
        # para garantir que a janela seja mesmo destruída na thread da UI.
        try:
            if self._agendador is not None:
                self._agendador(self._aplicar_fechar)
            else:
                self._aplicar_fechar()
        except Exception:
            pass
        self._fechada = True

    # ------------------------------------------------------------------
    # aplicadores (rodam na thread da interface)
    # ------------------------------------------------------------------
    def _aplicar_estado(self, texto: str):
        if self._fechada or not self._widget_vivo(self._rotulo_estado):
            return
        try:
            self._rotulo_estado.configure(text=texto)
        except Exception:
            pass

    def _aplicar_bytes(self, recebido: int, total: int):
        if self._fechada or not self._widget_vivo(self._barra):
            return
        exib = calcular_exibicao(recebido, total)
        try:
            if exib["modo"] == "determinado":
                self._barra.stop()
                self._barra.set(exib["fracao"])
            else:
                self._barra.start()
            if self._widget_vivo(self._rotulo_info):
                self._rotulo_info.configure(text=exib["texto"])
        except Exception:
            pass

    def _aplicar_indeterminado(self):
        if self._fechada or not self._widget_vivo(self._barra):
            return
        try:
            self._barra.start()
            if self._widget_vivo(self._rotulo_info):
                self._rotulo_info.configure(text="tamanho desconhecido")
        except Exception:
            pass

    def _aplicar_concluido(self):
        if self._fechada or not self._widget_vivo(self._barra):
            return
        try:
            self._barra.stop()
            self._barra.set(1.0)
            if self._widget_vivo(self._rotulo_estado):
                self._rotulo_estado.configure(text=ESTADOS["concluido"])
            if self._widget_vivo(self._rotulo_info):
                self._rotulo_info.configure(text="100%")
        except Exception:
            pass

    def _aplicar_falha(self, mensagem: str):
        if self._fechada or not self._widget_vivo(self._rotulo_estado):
            return
        try:
            self._barra.stop()
            self._rotulo_estado.configure(text=ESTADOS["erro"])
            if self._widget_vivo(self._rotulo_info) and mensagem:
                self._rotulo_info.configure(text=mensagem)
        except Exception:
            pass

    def _aplicar_fechar(self):
        if self._janela is None:
            return
        try:
            if self._widget_vivo(self._janela):
                self._janela.destroy()
        except Exception:
            pass

