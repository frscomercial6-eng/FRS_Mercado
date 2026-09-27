import json
import os
import queue
import sqlite3
import ctypes
import shutil
import webbrowser
from ctypes import wintypes
from datetime import datetime
from tkinter import ttk, messagebox, filedialog

import customtkinter as ctk
from PIL import Image

import modulo_financeiro
from calculadora_tributaria import CalculadoraTributaria
from database_manager import get_db_connection, obter_caminho_dados, registrar_log
from modulo_config import carregar_configuracoes, obter_limite_sangria_preventiva
from modulo_estoque import aplicar_baixa_fefo
from modulo_fiscal import ModuloExportacaoFiscal, FiscalManager
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero
from webhook_delivery import iniciar_servidor_webhook
from error_notifier import ensure_error_telemetry_started


ROTULO_SEM_CLIENTE_ORCAMENTO = "Sem cliente cadastrado"



def calcular_impostos_liquidos(valor_venda, ncm):
    """Calcula imposto por NCM respeitando transição para IVA Dual a partir de 2027."""
    calc = CalculadoraTributaria()
    resultado = calc.calcular_impostos(valor_venda, datetime.now().date(), ncm=ncm)
    return {
        "aliquota": float(resultado.get("aliquota", 0.0)),
        "valor_imposto": float(resultado.get("valor_imposto", 0.0)),
        "valor_liquido": float(resultado.get("valor_liquido", 0.0)),
        "regime": resultado.get("regime", "ATUAL"),
        "aliquotas": resultado.get("aliquotas", {}),
        "valores": resultado.get("valores", {}),
    }


def calcular_dv_ean13(base12):
    """Calcula o dígito verificador EAN-13 (módulo 10) dos 12 primeiros dígitos."""
    digitos = "".join(ch for ch in str(base12 or "") if ch.isdigit())
    if len(digitos) != 12:
        return None
    soma = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digitos))
    return str((10 - (soma % 10)) % 10)


def parse_etiqueta_balanca_filizola(codigo):
    """Parser isolado da etiqueta de peso/preço variável (Filizola Platina 15).

    Formato: 2 | PPPPP | VVVVVV | DV — 13 dígitos numéricos com DV EAN-13 válido,
    onde PPPPP é o PLU do produto (5 dígitos) e VVVVVV é o total da pesagem em
    centavos (valor já calculado pela balança).

    Retorna {"plu": "00010", "valor": 1.17} ou None se não for etiqueta válida.
    O PLU é devolvido EXATO (zeros à esquerda preservados): o chamador deve
    buscar por igualdade, sem normalizar zeros, sem fallback e sem LIKE.
    """
    texto = "".join(ch for ch in str(codigo or "").strip() if ch.isdigit())
    if len(texto) != 13 or not texto.startswith("2"):
        return None
    if calcular_dv_ean13(texto[:12]) != texto[12]:
        return None
    plu = texto[1:6]
    try:
        valor = round(int(texto[6:12]) / 100.0, 2)
    except ValueError:
        return None
    if valor <= 0:
        return None
    return {"plu": plu, "valor": valor}


class ModuloPDV(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        ensure_error_telemetry_started()
        self.title("Caixa PDV - Mercado FRS")
        self.geometry("1100x750")
        self.attributes("-fullscreen", True)

        self.caixa_id = None
        self.fiscal = ModuloExportacaoFiscal()
        self.fiscal_manager = FiscalManager()
        self.config = carregar_configuracoes()
        self.itens_carrinho = []
        self.item_selecionado_idx = None
        self.multiplicador_atual = 1
        # FASE 1 UN/KG: modo atual da mascara do campo Qtd do PDV.
        # "UN" = inteiro (padrao historico); "KG" = decimal ate 3 casas.
        self._mascara_qtd_pdv = "UN"
        # Atalho <End>: ABRIR A GAVETA (somente isso — nenhum outro fluxo).
        self._ligar_atalho_end_gaveta()
        self.limite_caixa_atual = obter_limite_sangria_preventiva()
        self.excesso_caixa_atual = 0.0
        self.fila_pedidos_delivery = queue.Queue()
        self.calculadora_tributaria = CalculadoraTributaria()
        self.modal_abertura = None
        self._id_after_verificacao_caixa = None
        self.forma_pagamento_selecionada = "DINHEIRO"
        self.forma_pagamento_selecionada = "DINHEIRO"
        # Pagamentos recebidos na venda corrente (regra 2): acumulam o valor
        # efetivamente recebido, sem alterar o total da venda.
        self.valor_pago_acumulado = 0.0
        self.pagamentos_parciais = []  # [(forma_pagamento, valor_recebido)]
        self.clientes_orcamento_map = {}
        # Contextos documentais são opcionais e não participam do pagamento normal.
        self._vales_para_quitar = []
        self._operacao_documento_tipo = None
        self._orcamento_para_vender_id = None
        self._operacao_vale_cliente_id = None
        self._cache_xml_por_ean = {}
        self._cache_imagens_produtos = {}
        self._grid_pending_refresh = False
        self._grid_rows_cache = {}

        if master is not None and not getattr(master, "usuario_atual", None):
            self.destroy()
            return

        self.bind("<F1>", lambda e: self.selecionar_forma_pagamento("DINHEIRO"))
        self.bind("<F2>", lambda e: self.selecionar_forma_pagamento("PIX"))
        self.bind("<F3>", lambda e: self.selecionar_forma_pagamento("DEBITO"))
        self.bind("<F4>", lambda e: self.selecionar_forma_pagamento("CREDITO"))
        self.bind("<F5>", lambda e: self.selecionar_forma_pagamento("VOUCHER"))
        self.bind("<F6>", lambda e: self.abrir_modal_diversos())
        self.bind("<F7>", lambda e: self.modal_sangria())
        self.bind("<F8>", lambda e: self.salvar_vale_atual())
        self.bind("<F9>", lambda e: self.finalizar_venda_com_confirmacoes())
        self.bind("<F12>", lambda e: self.finalizar_venda_com_confirmacoes())
        self.bind("<Delete>", self._ao_pressionar_delete_cancelar_item)
        # Ao minimizar, restaura uma eventual tela de abertura já existente.
        self.bind("<Map>", self._ao_reexibir_pdv, add="+")
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar_janela)

        try:
            info_webhook = iniciar_servidor_webhook(self._enfileirar_pedido_delivery)
            registrar_log(
                None,
                "Webhook Delivery",
                "Sucesso",
                f"Webhook interno ativo em http://{info_webhook['host']}:{info_webhook['port']}/receber_pedido_externo",
            )
        except Exception as e:
            registrar_log(None, "Webhook Delivery", "Falha", f"Erro ao iniciar webhook interno: {e}")

        # Desenha a interface base imediatamente para evitar janela preta/vazia.
        self.configurar_interface_pdv()
        self._set_status("Inicializando PDV...", "#4aa3ff")
        self._id_after_verificacao_caixa = self.after(100, self.verificar_caixa_aberto)

    def _safe_focus(self, widget):
        try:
            if self.winfo_exists() and widget is not None and widget.winfo_exists():
                widget.focus_set()
                if hasattr(widget, "icursor"):
                    widget.icursor("end")
                if hasattr(widget, "_entry") and hasattr(widget._entry, "icursor"):
                    widget._entry.icursor("end")
        except Exception:
            pass

    def _safe_after(self, ms, callback):
        try:
            if self.winfo_exists():
                self.after(ms, callback)
        except Exception:
            pass

    def _set_status(self, mensagem, cor="#3498db"):
        try:
            if hasattr(self, "lbl_status_operacao") and self.lbl_status_operacao.winfo_exists():
                self.lbl_status_operacao.configure(text=mensagem, text_color=cor)
        except Exception:
            pass

    def _atualizar_indicadores_caixa(self, aberto):
        """Sincroniza SOMENTE o indicador compacto do topo com o estado real.

        Usa somente ``caixa_id`` como fonte (aberto = caixa_id não-None).
        O painel grande (tela do cliente) NÃO é tocado aqui: ele exibe
        exclusivamente os valores reais da venda (VALOR PAGO | TROCO |
        TOTAL DA VENDA), atualizados por ``atualizar_troco_display`` e
        ``atualizar_total_display``.
        """
        texto = "🟢 CAIXA ABERTO" if aberto else "🔴 CAIXA FECHADO"
        cor = "#2ecc71" if aberto else "#ff6666"
        try:
            if hasattr(self, "lbl_estado_caixa_topo") and self.lbl_estado_caixa_topo.winfo_exists():
                self.lbl_estado_caixa_topo.configure(text=texto, text_color=cor)
        except Exception:
            pass

    def _formatar_moeda_br(self, valor):
        try:
            numero = float(valor)
        except Exception:
            numero = 0.0

        txt = f"{numero:,.2f}"
        txt = txt.replace(",", "#").replace(".", ",").replace("#", ".")
        return f"R$ {txt}"

    def verificar_caixa_aberto(self):
        self._id_after_verificacao_caixa = None
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, data_abertura FROM caixa_operacao WHERE status = 'ABERTO' ORDER BY id DESC LIMIT 1"
                )
                res = cursor.fetchone()
        except Exception as e:
            registrar_log(None, "Verificação de Caixa", "Falha", f"Erro: {e}")
            self._set_status(f"Falha ao verificar caixa: {e}", "#ff6666")
            return

        if res:
            caixa_id, data_abertura = res
            data_abertura = str(data_abertura or "")[:10]
            hoje = datetime.now().strftime("%Y-%m-%d")

            if data_abertura == hoje:
                self.caixa_id = caixa_id
                registrar_log(None, "Verificação de Caixa", "Sucesso", f"Caixa {caixa_id} já aberto hoje.")
                self._set_status(f"Caixa {caixa_id} aberto. PDV pronto para operação.", "#2ecc71")
                self._atualizar_indicadores_caixa(True)
                self._retornar_foco_pdv()
                return

            try:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE caixa_operacao SET status = 'FECHADO', data_fechamento = CURRENT_TIMESTAMP WHERE id = ?",
                        (caixa_id,),
                    )
                registrar_log(
                    None,
                    "Verificação de Caixa",
                    "Aviso",
                    f"Caixa {caixa_id} de dia anterior foi fechado automaticamente para exigir nova abertura.",
                )
            except Exception as e:
                registrar_log(None, "Verificação de Caixa", "Falha", f"Erro ao fechar caixa antigo: {e}")

        # Sem caixa válido para hoje: fonte única de verdade = caixa_id None,
        # com o indicador compacto sincronizado (🔴 CAIXA FECHADO).
        self.caixa_id = None
        self._atualizar_indicadores_caixa(False)
        self.abrir_caixa_modal()

    def abrir_caixa_modal(self):
        try:
            if self.modal_abertura is not None and self.modal_abertura.winfo_exists():
                self.modal_abertura.deiconify()
                self.modal_abertura.lift()
                self.modal_abertura.focus_force()
                return
        except Exception:
            self.modal_abertura = None

        self.modal_abertura = ctk.CTkToplevel(self)
        self.modal_abertura.title("Abertura de Caixa - Contagem Inicial")
        self.modal_abertura.geometry("450x650")
        self.modal_abertura.transient(self)
        self.modal_abertura.lift()
        self.modal_abertura.grab_set()

        # Centraliza sobre a janela do PDV após renderização.
        self.update_idletasks()
        self.modal_abertura.update_idletasks()
        largura = 450
        altura = 650
        x = self.winfo_rootx() + max((self.winfo_width() - largura) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - altura) // 2, 0)
        self.modal_abertura.geometry(f"{largura}x{altura}+{x}+{y}")

        ctk.CTkLabel(
            self.modal_abertura,
            text="CONTAGEM DE DINHEIRO (ABERTURA)",
            font=("Arial", 16, "bold"),
        ).pack(pady=10)

        scroll_contagem = ctk.CTkScrollableFrame(self.modal_abertura, width=400, height=450)
        scroll_contagem.pack(pady=10, padx=20)

        cedulas_moedas = {
            "200.00": 0,
            "100.00": 0,
            "50.00": 0,
            "20.00": 0,
            "10.00": 0,
            "5.00": 0,
            "2.00": 0,
            "1.00": 0,
            "0.50": 0,
            "0.25": 0,
            "0.10": 0,
            "0.05": 0,
        }
        entries_contagem = {}

        for valor in cedulas_moedas.keys():
            frame = ctk.CTkFrame(scroll_contagem)
            frame.pack(fill="x", pady=2)
            ctk.CTkLabel(frame, text=f"{self._formatar_moeda_br(valor)}:", width=100).pack(side="left", padx=10)
            entry = ctk.CTkEntry(frame, width=150, placeholder_text="Quantidade")
            entry.pack(side="right", padx=10)
            aplicar_padrao_entrada_numerica(entry, inteiro=True)
            entries_contagem[valor] = entry

        # ENTRADA DIRETA DO VALOR TOTAL (campo independente e OPCIONAL).
        # Permite informar o valor inicial do caixa sem usar as denominações.
        # A contagem por denominações acima permanece exatamente como estava.
        ctk.CTkFrame(scroll_contagem, height=2, fg_color="#3a3a3a").pack(fill="x", pady=(10, 6))

        frame_abertura_direta = ctk.CTkFrame(scroll_contagem)
        frame_abertura_direta.pack(fill="x", pady=2)
        ctk.CTkLabel(
            frame_abertura_direta,
            text="Abertura de caixa: R$",
            width=170,
            font=("Arial", 13, "bold"),
        ).pack(side="left", padx=10)
        entry_abertura = ctk.CTkEntry(frame_abertura_direta, width=150, placeholder_text="Valor total")
        entry_abertura.pack(side="right", padx=10)
        aplicar_padrao_entrada_numerica(entry_abertura, inteiro=False, casas_decimais=2)

        primeira_entry = next(iter(entries_contagem.values()), None)

        def confirmar_abertura():
            total_denominacoes = 0.0
            try:
                for valor, entry in entries_contagem.items():
                    qtd = parse_numero(
                        entry.get(),
                        "Quantidade",
                        permitir_vazio=True,
                        default=0,
                        inteiro=True,
                        minimo=0,
                    )
                    total_denominacoes += float(valor) * qtd

                # PREVALÊNCIA DO VALOR DIRETO, sem conciliação: se o operador
                # digitou o total no campo "Abertura de caixa: R$", esse valor é
                # o saldo inicial. Campo em branco => vale a contagem por
                # denominações (fluxo antigo, inalterado).
                texto_abertura = entry_abertura.get().strip()
                if texto_abertura:
                    total_inicial = float(
                        parse_numero(
                            texto_abertura,
                            "Abertura de caixa",
                            permitir_vazio=True,
                            default=0.0,
                            minimo=0,
                        )
                    )
                else:
                    total_inicial = total_denominacoes

                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("INSERT INTO caixa_operacao (saldo_inicial) VALUES (?)", (total_inicial,))
                    self.caixa_id = cursor.lastrowid

                try:
                    self.modal_abertura.grab_release()
                except Exception:
                    pass
                self.modal_abertura.destroy()
                self.modal_abertura = None
                total_fmt = self._formatar_moeda_br(total_inicial)
                # O status compacto NÃO exibe o valor do saldo inicial: o
                # saldo pertence ao caixa (persistido em caixa_operacao e
                # registrado no log de auditoria logo abaixo).
                self._set_status(f"Caixa {self.caixa_id} aberto. PDV pronto para operação.", "#2ecc71")
                registrar_log(None, "Abertura de Caixa", "Sucesso", f"Caixa {self.caixa_id} aberto com {total_fmt}")
                self._atualizar_indicadores_caixa(True)
                self._retornar_foco_pdv()
            except Exception as e:
                self._set_status(f"Erro ao abrir caixa: {e}", "#ff6666")
                registrar_log(None, "Abertura de Caixa", "Falha", f"Erro: {e}")

        ctk.CTkButton(
            self.modal_abertura,
            text="CONFIRMAR ABERTURA",
            fg_color="green",
            command=confirmar_abertura,
        ).pack(pady=20)

        def _focar_modal_abertura():
            try:
                self.modal_abertura.lift()
                self.modal_abertura.focus_force()
                if primeira_entry is not None and primeira_entry.winfo_exists():
                    primeira_entry.focus_set()
            except Exception:
                pass

        def _on_close_modal_abertura():
            # X cancela a abertura (sem confirmação extra): NENHUM caixa é
            # criado, ``self.caixa_id`` permanece None e o indicador segue
            # em 🔴 CAIXA FECHADO. O caixa só nasce no CONFIRMAR ABERTURA.
            try:
                self.modal_abertura.grab_release()
            except Exception:
                pass
            try:
                self.modal_abertura.destroy()
            except Exception:
                pass
            self.modal_abertura = None
            self.caixa_id = None
            self._atualizar_indicadores_caixa(False)
            self._set_status("Abertura de caixa cancelada. Caixa permanece fechado.", "#f1c40f")

        self.modal_abertura.protocol("WM_DELETE_WINDOW", _on_close_modal_abertura)
        self.after(10, _focar_modal_abertura)

    def _carregar_logo_mercado(self):
        """Localiza o logo do mercado (logo_mercado_mario.<ext>) somente para leitura.

        Procura na pasta Assets do projeto (e, em build empacotado, na pasta do
        executável), preservando a extensão existente do arquivo. Não grava
        nada em disco e não depende de banco de dados.
        """
        import sys

        nome_base = "logo_mercado_mario"
        extensoes_imagem = (".jpeg", ".jpg", ".png", ".bmp", ".gif", ".webp")
        diretorios = []
        try:
            diretorios.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
        except Exception:
            pass
        try:
            diretorios.append(os.path.join(os.getcwd(), "assets"))
        except Exception:
            pass
        if getattr(sys, "frozen", False):
            try:
                diretorios.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "assets"))
            except Exception:
                pass
        for diretorio in diretorios:
            try:
                if not os.path.isdir(diretorio):
                    continue
                for nome in os.listdir(diretorio):
                    base, ext = os.path.splitext(nome)
                    if base == nome_base and ext.lower() in extensoes_imagem:
                        caminho = os.path.join(diretorio, nome)
                        try:
                            return Image.open(caminho)
                        except Exception:
                            continue
            except Exception:
                continue
        return None

    def _posicionar_logo_fundo(self):
        """Exibe o logo do mercado como fundo centralizado do PDV.

        O logo é posicionado atrás dos widgets (lower), centralizado e
        proporcional, sem cobrir campos, botões ou lista de produtos, e
        sem interferir no foco do teclado ou no scanner.
        """
        if not getattr(self, "main_pdv", None):
            return
        pil_img = self._carregar_logo_mercado()
        if pil_img is None:
            return
        try:
            largura_area = int(self.main_pdv.winfo_width() or 0)
            altura_area = int(self.main_pdv.winfo_height() or 0)
            if largura_area < 50:
                largura_area = 1100
            if altura_area < 50:
                altura_area = 750
            max_w = max(int(largura_area * 0.60), 1)
            max_h = max(int(altura_area * 0.60), 1)
            ratio = min(max_w / float(pil_img.width), max_h / float(pil_img.height))
            w = max(1, int(pil_img.width * ratio))
            h = max(1, int(pil_img.height * ratio))
            if pil_img.mode != "RGBA":
                pil_img = pil_img.convert("RGBA")
            logo_img = pil_img.resize((w, h), getattr(Image, "Resampling", Image).LANCZOS)
            self._logo_ctk_image = ctk.CTkImage(
                light_image=logo_img, dark_image=logo_img, size=(w, h)
            )
            self._logo_id = ctk.CTkLabel(
                self.main_pdv, text="", image=self._logo_ctk_image, fg_color="transparent"
            )
            self._logo_id.place(relx=0.5, rely=0.5, anchor="center")
            self._logo_id.lower()
        except Exception:
            pass

    def configurar_interface_pdv(self):
        for widget in self.winfo_children():
            widget.destroy()

        self.main_pdv = ctk.CTkFrame(self, fg_color="black", corner_radius=0)
        self.main_pdv.pack(fill="both", expand=True)

        self.top_bar = ctk.CTkFrame(self.main_pdv, height=50, fg_color="#1a1a1a", corner_radius=0)
        self.top_bar.pack(side="top", fill="x")
        self.btn_menu_retratil = ctk.CTkButton(
            self.top_bar,
            text="≡",
            width=48,
            height=34,
            font=("Roboto", 22, "bold"),
            fg_color="#333333",
            hover_color="#444444",
            command=self._alternar_menu_operacoes,
        )
        self.btn_menu_retratil.pack(side="left", padx=(12, 4), pady=8)
        self.lbl_estado_caixa_topo = ctk.CTkLabel(self.top_bar, text="🔴 CAIXA FECHADO", font=("Roboto", 16, "bold"), text_color="#ff6666")
        self.lbl_estado_caixa_topo.pack(side="left", padx=6)

        # Menu retrátil de operações secundárias (abre/fecha pelo ícone "≡").
        self.menu_operacoes = ctk.CTkFrame(self.main_pdv, fg_color="#181818", corner_radius=0, height=0)
        self.menu_operacoes.pack(side="top", fill="x")
        self.menu_operacoes.pack_propagate(False)
        self._menu_operacoes_aberto = False

        self.menu_operacoes_miolo = ctk.CTkFrame(self.menu_operacoes, fg_color="transparent")

        linha_superior = ctk.CTkFrame(self.menu_operacoes_miolo, fg_color="transparent")
        linha_superior.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(linha_superior, text="OPERAÇÕES", font=("Roboto", 12, "bold"), text_color="gray").pack(side="left", padx=(0, 12))
        ctk.CTkButton(linha_superior, text="SANGRIA (F7)", fg_color="#c0392b", width=140, command=self.modal_sangria).pack(side="left", padx=6)
        ctk.CTkButton(linha_superior, text="SUPRIMENTO", fg_color="#2980b9", width=150, command=self.modal_suprimento).pack(side="left", padx=6)
        ctk.CTkButton(linha_superior, text="CANCELAR ITEM (DEL)", fg_color="#d35400", width=170, command=self.cancelar_item).pack(side="left", padx=6)
        ctk.CTkButton(linha_superior, text="CANCELAR VENDA ATUAL", fg_color="#c0392b", hover_color="#962d22", width=190, command=self.cancelar_venda_atual).pack(side="left", padx=6)
        ctk.CTkButton(linha_superior, text="FECHAR CAIXA", fg_color="#8e44ad", width=150, command=self.processar_fechamento_inteligente).pack(side="left", padx=6)

        linha_orcamento = ctk.CTkFrame(self.menu_operacoes_miolo, fg_color="transparent")
        linha_orcamento.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(linha_orcamento, text="CLIENTE (ORÇAMENTO / VALE)", font=("Roboto", 11, "bold"), text_color="gray").pack(side="left", padx=(10, 10))
        self.combo_cliente_orcamento = ctk.CTkOptionMenu(linha_orcamento, values=[ROTULO_SEM_CLIENTE_ORCAMENTO], width=220)
        self.combo_cliente_orcamento.pack(side="left", padx=6)
        ctk.CTkButton(linha_orcamento, text="ATUALIZAR CLIENTES", fg_color="#455a64", width=150, command=lambda: self._carregar_clientes_orcamento(preservar_selecao=True)).pack(side="left", padx=6)
        ctk.CTkButton(linha_orcamento, text="CADASTRO RÁPIDO", fg_color="#00897b", width=150, command=self.cadastrar_cliente_rapido).pack(side="left", padx=6)

        linha_documentos = ctk.CTkFrame(self.menu_operacoes_miolo, fg_color="transparent")
        linha_documentos.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(linha_documentos, text="DOCUMENTOS", font=("Roboto", 11, "bold"), text_color="gray").pack(side="left", padx=(10, 10))
        ctk.CTkButton(linha_documentos, text="SALVAR ORÇAMENTO", fg_color="#1565c0", width=160, command=self.salvar_orcamento_atual).pack(side="left", padx=6)
        ctk.CTkButton(linha_documentos, text="ABRIR ORÇAMENTO", fg_color="#5d4037", width=160, command=self.abrir_orcamento_por_numero).pack(side="left", padx=6)
        ctk.CTkButton(linha_documentos, text="SALVAR VALE (F8)", fg_color="#ef6c00", width=140, command=self.salvar_vale_atual).pack(side="left", padx=6)
        ctk.CTkButton(linha_documentos, text="ABRIR VALE", fg_color="#6a1b9a", width=140, command=self.abrir_vale_por_cliente).pack(side="left", padx=6)
        self._carregar_clientes_orcamento()

        self.centro_container = ctk.CTkFrame(self.main_pdv, fg_color="black")
        self.centro_container.pack(fill="both", expand=True, padx=10, pady=10)

        self.grid_container = ctk.CTkFrame(self.centro_container, fg_color="#121212")
        self.grid_container.pack(side="left", fill="both", expand=True)

        self.input_topo = ctk.CTkFrame(self.grid_container, fg_color="#1a1a1a", height=70)
        self.input_topo.pack(fill="x", padx=8, pady=(8, 6))

        # Campo Qtd visualmente minimo (decisao de interface 25/09/2026):
        # APENAS geometria do widget (width/padx). O widget permanece na
        # hierarquia e no fluxo de foco; binds, logica de quantidade, 12*,
        # UN/KG e fluxo TAB homologado NAO foram alterados.
        self.ent_quantidade = ctk.CTkEntry(self.input_topo, width=1, height=50, font=("Roboto", 20, "bold"), placeholder_text="", border_width=0, corner_radius=0)
        self.ent_quantidade.pack(side="left", padx=(0, 0), pady=10)

        self.ent_quantidade.bind("<Return>", lambda _e: self._safe_focus(self.ent_cod_barras))
        self.ent_quantidade.bind("<Tab>", self._focar_produto_pelo_tab)
        aplicar_padrao_entrada_numerica(self.ent_quantidade, inteiro=True)
        self._mascara_qtd_pdv = "UN"
        # Campo de quantidade inicia vazio; vazio assume 1 por padrão.
        self.ent_quantidade.delete(0, "end")

        self.ent_cod_barras = ctk.CTkEntry(
            self.input_topo,
            height=50,
            font=("Roboto", 22, "bold"),
            placeholder_text="Código ou Nome do Produto (Enter para adicionar)",
        )
        self.ent_cod_barras.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=10)
        self.ent_cod_barras.bind("<Return>", self.processar_entrada_produto)
        self.ent_cod_barras.bind("<Tab>", self._processar_entrada_produto_tab)

        self.header_vendas = ctk.CTkFrame(self.grid_container, fg_color="#262626", height=40)
        self.header_vendas.pack(fill="x")

        cols = [("Cód", 130), ("Produto", 540), ("Qtd", 90), ("Total", 140), ("Img", 55)]
        for texto, largura in cols:
            ctk.CTkLabel(self.header_vendas, text=texto, width=largura, font=("Roboto", 12, "bold"), text_color="gray").pack(side="left", padx=5)

        self.scroll_vendas = ctk.CTkScrollableFrame(self.grid_container, fg_color="#121212", corner_radius=0)
        self.scroll_vendas.pack(fill="both", expand=True)

        self.painel_lateral = ctk.CTkFrame(self.centro_container, width=220, fg_color="#1a1a1a")
        self.painel_lateral.pack(side="right", fill="y", padx=(10, 0))
        self.painel_lateral.pack_propagate(False)

        self.scroll_operacoes = ctk.CTkScrollableFrame(self.painel_lateral, fg_color="#1a1a1a", corner_radius=0)
        self.scroll_operacoes.pack(fill="both", expand=True)

        ctk.CTkLabel(self.scroll_operacoes, text="VALOR PAGO", font=("Roboto", 11, "bold"), text_color="gray").pack(pady=(10, 2))
        self.ent_valor_pago = ctk.CTkEntry(self.scroll_operacoes, width=180, placeholder_text="0,00")
        self.ent_valor_pago.pack(padx=10, pady=(0, 8))
        self.ent_valor_pago.bind("<KeyRelease>", lambda _e: self.atualizar_troco_display())
        self.ent_valor_pago.bind("<Return>", lambda _e: self.acrescentar_valor_pago())
        aplicar_padrao_entrada_numerica(self.ent_valor_pago, inteiro=False, casas_decimais=2)

        self.btn_acrescentar_pago = ctk.CTkButton(
            self.scroll_operacoes,
            text="ACRESCENTAR\nPAGAMENTO",
            fg_color="#2c3e50",
            height=40,
            font=("Roboto", 11, "bold"),
            command=self.acrescentar_valor_pago,
        )
        self.btn_acrescentar_pago.pack(fill="x", padx=10, pady=(0, 8))

        ctk.CTkLabel(self.scroll_operacoes, text="PAGAMENTO RAPIDO", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=(12, 10))
        ctk.CTkButton(self.scroll_operacoes, text="DINHEIRO (F1)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("DINHEIRO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="PIX (F2)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("PIX")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="CARTAO DEBITO (F3)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("DEBITO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="CARTAO CREDITO (F4)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("CREDITO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="VOUCHER (F5)", fg_color="#16a085", command=lambda: self.selecionar_forma_pagamento("VOUCHER")).pack(fill="x", padx=10, pady=2)

        ctk.CTkButton(
            self.scroll_operacoes,
            text="DIVERSOS (F6)",
            fg_color="#8e44ad",
            height=30,
            font=("Roboto", 12, "bold"),
            command=self.abrir_modal_diversos,
        ).pack(fill="x", padx=10, pady=(8, 2))

        # BOTÃO VISUAL "MÚLTIPLO PAGTO (F8)" REMOVIDO NESTA RODADA.
        # Apenas o componente de interface foi retirado do painel lateral; o
        # método abrir_modal_pagamento_multiplo() e TODO o fluxo interno de
        # múltiplos pagamentos (pagamentos_parciais, valor_pago_acumulado,
        # divisão, restante/troco e registro "MISTO") permanecem intactos e
        # continuam existindo sem qualquer alteração de regra. O atalho F8,
        # liberado, passou a acionar a função existente de SALVAR VALE.

        ctk.CTkButton(
            self.scroll_operacoes,
            text="FINALIZAR VENDA (F9)",
            fg_color="#27ae60",
            height=44,
            font=("Roboto", 14, "bold"),
            command=self.finalizar_venda_com_confirmacoes,
        ).pack(fill="x", padx=10, pady=(16, 8))

        ctk.CTkButton(
            self.scroll_operacoes,
            text="MINIMIZAR",
            fg_color="#4a4a4a",
            command=self._minimizar_pdv,
        ).pack(fill="x", padx=10, pady=(0, 12))

        self.footer = ctk.CTkFrame(self.main_pdv, height=110, fg_color="#1a1a1a", corner_radius=0)
        self.footer.pack(side="bottom", fill="x")

        self.footer_content = ctk.CTkFrame(self.footer, fg_color="transparent")
        self.footer_content.pack(expand=True, fill="both", pady=6)

        # === PAINEL GRANDE — TELA DO CLIENTE (3 áreas horizontais equivalentes) ===
        # ESQUERDA: VALOR PAGO (amarelo) | CENTRO: TROCO (vermelho) | DIREITA: TOTAL DA VENDA (verde).
        # Este painel é da VENDA: o status do caixa fica SÓ no indicador compacto do topo.
        self.pago_frame = ctk.CTkFrame(self.footer_content, fg_color="transparent")
        self.pago_frame.pack(side="left", expand=True, fill="both")

        ctk.CTkLabel(self.pago_frame, text="VALOR PAGO", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=(10, 0))
        self.lbl_pago_venda = ctk.CTkLabel(
            self.pago_frame,
            text="R$ 0,00",
            font=("Roboto", 40, "bold"),
            text_color="#f1c40f",
        )
        self.lbl_pago_venda.pack()

        self.lbl_restante_venda = ctk.CTkLabel(
            self.pago_frame,
            text="",
            font=("Roboto", 12, "bold"),
            text_color="#f39c12",
        )
        self.lbl_restante_venda.pack()

        self.troco_frame = ctk.CTkFrame(self.footer_content, fg_color="transparent")
        self.troco_frame.pack(side="left", expand=True, fill="both")

        ctk.CTkLabel(self.troco_frame, text="TROCO", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=(10, 0))
        self.lbl_troco_venda = ctk.CTkLabel(
            self.troco_frame,
            text="R$ 0,00",
            font=("Roboto", 40, "bold"),
            text_color="#e74c3c",
        )
        self.lbl_troco_venda.pack()

        self.total_frame = ctk.CTkFrame(self.footer_content, fg_color="transparent")
        self.total_frame.pack(side="left", expand=True, fill="both")

        ctk.CTkLabel(self.total_frame, text="TOTAL DA VENDA", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=(10, 0))
        self.lbl_total_venda = ctk.CTkLabel(self.total_frame, text="R$ 0,00", font=("Roboto", 40, "bold"), text_color="#2ecc71")
        self.lbl_total_venda.pack()

        self.lbl_status_operacao = ctk.CTkLabel(self.main_pdv, text="PDV pronto para operação.", font=("Arial", 11, "bold"), text_color="#2ecc71")
        self.lbl_status_operacao.pack(side="bottom", pady=(0, 4))

        self.lbl_aviso_limite = ctk.CTkLabel(
            self.main_pdv,
            text="",
            font=("Arial", 11, "bold"),
            text_color="#f39c12",
            cursor="hand2",
        )
        self.lbl_aviso_limite.pack(side="bottom", pady=(0, 4))
        self.lbl_aviso_limite.pack_forget()
        self.lbl_aviso_limite.bind("<Button-1>", lambda e: self.modal_sangria(preencher_excesso=True))

        # === RODAPÉ FRS Solutions (identidade, discreta) ===
        self.lbl_rodape_frs = ctk.CTkLabel(
            self.main_pdv,
            text="Desenvolvido por FRS Solutions — www.frssolutions.com.br",
            font=("Arial", 9, "normal"),
            text_color="#7f8c8d",
            cursor="hand2",
        )
        self.lbl_rodape_frs.pack(side="bottom", pady=(0, 2))
        self.lbl_rodape_frs.bind(
            "<Button-1>",
            lambda e: webbrowser.open("https://www.frssolutions.com.br/"),
        )

        # Posiciona o logo do mercado como fundo (após widgets principais)
        self._safe_after(100, self._posicionar_logo_fundo)

        self._retornar_foco_pdv()
        self._safe_after(300, self._processar_fila_delivery)

    def _alternar_menu_operacoes(self):
        """Abre/fecha o menu retrátil de operações secundárias (ícone ≡)."""
        try:
            if getattr(self, "_menu_operacoes_aberto", False):
                self.menu_operacoes_miolo.pack_forget()
                self.menu_operacoes.configure(height=0)
                self._menu_operacoes_aberto = False
            else:
                self.menu_operacoes_miolo.pack(fill="x", padx=14, pady=8)
                altura = self.menu_operacoes_miolo.winfo_reqheight() + 16
                self.menu_operacoes.configure(height=altura)
                self._menu_operacoes_aberto = True
        except Exception:
            pass

    def _avanco_tab_para_valor_pago(self):
        """TAB → VALOR PAGO somente com a venda pronta em DINHEIRO (regra 4).

        Demais formas (PIX/DÉBITO/CRÉDITO/VOUCHER/DIVERSOS/MÚLTIPLO)
        mantêm exatamente o comportamento de TAB anterior.
        """
        try:
            return (
                str(self.forma_pagamento_selecionada or "").strip().upper() == "DINHEIRO"
                and bool(self.itens_carrinho)
            )
        except Exception:
            return False

    def _focar_produto_pelo_tab(self, _event=None):
        if self._avanco_tab_para_valor_pago():
            self._safe_focus(self.ent_valor_pago)
            return "break"
        self._safe_focus(self.ent_cod_barras)
        return "break"

    def _enfileirar_pedido_delivery(self, payload):
        try:
            self.fila_pedidos_delivery.put_nowait(payload)
        except Exception:
            pass

    def _processar_fila_delivery(self):
        if not self.winfo_exists():
            return

        try:
            while True:
                payload = self.fila_pedidos_delivery.get_nowait()
                self._aplicar_pedido_delivery(payload)
        except queue.Empty:
            pass
        except Exception as e:
            registrar_log(None, "Webhook Delivery", "Falha", f"Erro no processamento da fila: {e}")
        finally:
            self._safe_after(300, self._processar_fila_delivery)

    def _normalizar_bool(self, valor):
        if isinstance(valor, bool):
            return valor
        if isinstance(valor, (int, float)):
            return valor == 1
        txt = str(valor or "").strip().lower()
        return txt in {"1", "true", "sim", "yes", "ok", "aprovado", "pago"}

    def _normalizar_origem_venda(self, payload):
        origem_raw = str(payload.get("origem") or payload.get("canal") or payload.get("plataforma") or "DELIVERY").strip()
        origem_upper = origem_raw.upper()
        if "IFOOD" in origem_upper:
            return "IFOOD", "iFood"
        if "APP" in origem_upper and "PROPR" in origem_upper:
            return "APP_PROPRIO", "App Próprio"
        if "LOJA" in origem_upper or "BALCAO" in origem_upper:
            return "LOJA_FISICA", "Loja Física"
        return "APP_PROPRIO", origem_raw or "Delivery"

    def _resolver_item_delivery(self, item, idx):
        nome = str(item.get("nome") or item.get("descricao") or f"Item Delivery {idx}").strip()
        codigo = str(item.get("codigo") or item.get("id") or "").strip()

        try:
            quantidade = parse_numero(item.get("quantidade", 1), "Quantidade", inteiro=True, minimo=1)
        except Exception:
            quantidade = 1
        quantidade = max(1, quantidade)

        try:
            preco = parse_numero(item.get("preco", 0), "Preço", permitir_vazio=True, default=0.0, minimo=0)
        except Exception:
            preco = 0.0
        preco = max(0.0, preco)

        produto = None
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if codigo:
                cursor.execute(
                    "SELECT id, codigo_barras, nome, ncm FROM produtos WHERE codigo_barras = ? LIMIT 1",
                    (codigo,),
                )
                produto = cursor.fetchone()

                if not produto and codigo.isdigit():
                    cursor.execute(
                        "SELECT id, codigo_barras, nome, ncm FROM produtos WHERE id = ? LIMIT 1",
                        (int(codigo),),
                    )
                    produto = cursor.fetchone()

            if not produto and nome:
                cursor.execute(
                    "SELECT id, codigo_barras, nome, ncm FROM produtos WHERE UPPER(nome) = UPPER(?) LIMIT 1",
                    (nome,),
                )
                produto = cursor.fetchone()

        if not produto:
            return None

        total_item = round(preco * quantidade, 2)
        return {
            "id": int(produto[0]),
            "barcode": produto[1] or "",
            "nome": nome or (produto[2] or "Item Delivery"),
            "preco": preco,
            "quantidade": quantidade,
            "total": total_item,
            "ncm": produto[3] or "",
            "origem": "DELIVERY",
        }

    def _alertar_novo_pedido(self, canal_label):
        aviso = f"Novo Pedido {canal_label} Chegou"
        self._set_status(aviso, "#f1c40f")
        try:
            self.bell()
        except Exception:
            pass

    def _selecionar_cliente_orcamento(self, cliente_id):
        if cliente_id is None:
            self.combo_cliente_orcamento.set(ROTULO_SEM_CLIENTE_ORCAMENTO)
            return
        self._carregar_clientes_orcamento(preservar_selecao=False)
        for label, mapped_id in self.clientes_orcamento_map.items():
            if mapped_id == int(cliente_id):
                self.combo_cliente_orcamento.set(label)
                return

    def cadastrar_cliente_rapido(self):
        nome = ctk.CTkInputDialog(
            text="Informe somente o NOME do cliente:",
            title="Cadastro rápido de cliente",
        ).get_input()
        nome = str(nome or "").strip()
        if not nome:
            return None
        try:
            with get_db_connection() as conn:
                existente = conn.execute(
                    "SELECT id FROM clientes WHERE nome = ? COLLATE NOCASE LIMIT 1",
                    (nome,),
                ).fetchone()
                if existente:
                    cliente_id = int(existente[0])
                else:
                    cursor = conn.execute("INSERT INTO clientes (nome) VALUES (?)", (nome,))
                    cliente_id = int(cursor.lastrowid)
            self._carregar_clientes_orcamento(preservar_selecao=False)
            self._selecionar_cliente_orcamento(cliente_id)
            registrar_log(None, "PDV Cliente", "Sucesso", f"Cadastro rápido: cliente {cliente_id} - {nome}")
            self._set_status(f"Cliente cadastrado e selecionado: {nome}", "#2ecc71")
            return cliente_id
        except Exception as e:
            self._set_status(f"Falha no cadastro rápido: {e}", "#ff6666")
            registrar_log(None, "PDV Cliente", "Falha", f"Erro: {e}")
            return None

    def _carregar_clientes_orcamento(self, preservar_selecao=False):
        selecao_anterior = None
        if preservar_selecao:
            try:
                selecao_anterior = self.combo_cliente_orcamento.get()
            except Exception:
                selecao_anterior = None

        try:
            with get_db_connection() as conn:
                clientes = conn.execute(
                    "SELECT id, nome FROM clientes ORDER BY nome COLLATE NOCASE ASC"
                ).fetchall()
        except Exception:
            clientes = []

        self.clientes_orcamento_map = {ROTULO_SEM_CLIENTE_ORCAMENTO: None}
        labels = [ROTULO_SEM_CLIENTE_ORCAMENTO]
        for cliente_id, nome in clientes:
            label = f"{cliente_id} - {nome}"
            self.clientes_orcamento_map[label] = int(cliente_id)
            labels.append(label)

        self.combo_cliente_orcamento.configure(values=labels)
        self.combo_cliente_orcamento.set(
            selecao_anterior if selecao_anterior in labels else ROTULO_SEM_CLIENTE_ORCAMENTO
        )

    def _cliente_selecionado_id(self):
        if not hasattr(self, "combo_cliente_orcamento"):
            return None
        return self.clientes_orcamento_map.get(self.combo_cliente_orcamento.get())

    def _preparar_carrinho_documental(self, itens, cliente_id, documento_tipo, vales_ids=None, orcamento_id=None):
        if self.itens_carrinho and not messagebox.askyesno(
            "Carregar documento",
            "O carrinho atual possui itens. Substituí-lo pelo documento selecionado?",
            parent=self,
        ):
            return False
        self.itens_carrinho = list(itens)
        self.item_selecionado_idx = None
        self._vales_para_quitar = list(vales_ids or [])
        self._operacao_documento_tipo = documento_tipo
        self._orcamento_para_vender_id = int(orcamento_id) if documento_tipo == "ORCAMENTO" and orcamento_id else None
        self._operacao_vale_cliente_id = int(cliente_id) if documento_tipo == "VALE" and cliente_id else None
        self._selecionar_cliente_orcamento(cliente_id)
        if hasattr(self, "ent_valor_pago"):
            self.ent_valor_pago.delete(0, "end")
        if hasattr(self, "lbl_troco_venda"):
            self.lbl_troco_venda.configure(text="R$ 0,00")
        if hasattr(self, "limpar_pagamentos_recebidos"):
            self.limpar_pagamentos_recebidos()
        self._renderizar_carrinho()
        self.atualizar_total_display()
        if getattr(self, "_menu_operacoes_aberto", False):
            self._alternar_menu_operacoes()
        if hasattr(self, "_safe_focus"):
            self._safe_focus(getattr(self, "ent_cod_barras", None))
        return True

    def abrir_orcamento_por_numero(self, numero_orcamento=None):
        if numero_orcamento is None:
            entrada = ctk.CTkInputDialog(
                text="NÚMERO DO ORÇAMENTO:",
                title="ABRIR ORÇAMENTO",
            ).get_input()
        else:
            entrada = numero_orcamento
        try:
            orcamento_id = int(str(entrada or "").strip())
        except (TypeError, ValueError):
            self._set_status("Número de orçamento inválido.", "#ff6666")
            return
        try:
            with get_db_connection() as conn:
                cabecalho = conn.execute(
                    "SELECT id, cliente_id, status FROM orcamentos WHERE id = ?",
                    (orcamento_id,),
                ).fetchone()
                if not cabecalho:
                    messagebox.showwarning("Abrir orçamento", "Orçamento não encontrado.", parent=self)
                    return
                if str(cabecalho[2]) != "ORCAMENTO":
                    messagebox.showinfo("Abrir orçamento", "Este orçamento já foi convertido em venda.", parent=self)
                    return
                itens = conn.execute(
                    """
                    SELECT produto_id, codigo_barras, descricao_produto, ncm,
                           quantidade, unidade, valor_unitario, subtotal
                    FROM orcamento_itens
                    WHERE orcamento_id = ?
                    ORDER BY id
                    """,
                    (orcamento_id,),
                ).fetchall()
            if not itens:
                messagebox.showwarning("Abrir orçamento", "Orçamento sem itens.", parent=self)
                return
            itens_carrinho = [
                {
                    "id": item[0], "barcode": item[1] or "", "nome": item[2],
                    "ncm": item[3] or "", "quantidade": float(item[4] or 0.0),
                    "unidade": str(item[5] or "UN").upper(),
                    "preco": float(item[6] or 0.0), "total": float(item[7] or 0.0),
                    "origem": "ORCAMENTO",
                }
                for item in itens
            ]
            if self._preparar_carrinho_documental(
                itens_carrinho, cabecalho[1], "ORCAMENTO", orcamento_id=orcamento_id
            ):
                self._set_status(f"Orçamento #{orcamento_id} carregado no PDV.", "#2ecc71")
        except Exception as e:
            self._set_status(f"Falha ao abrir orçamento: {e}", "#ff6666")
            registrar_log(None, "PDV Orçamento", "Falha", f"Erro ao abrir: {e}")

    def salvar_orcamento_atual(self):
        if not self.itens_carrinho:
            self._set_status("Adicione itens antes de salvar orçamento.", "#ff6666")
            return

        # Recarrega a lista no ato do salvamento para não manter um cache antigo
        # de clientes cadastrados em outra tela.
        self._carregar_clientes_orcamento(preservar_selecao=True)

        cliente_label = self.combo_cliente_orcamento.get() if hasattr(self, "combo_cliente_orcamento") else ROTULO_SEM_CLIENTE_ORCAMENTO
        if cliente_label not in self.clientes_orcamento_map:
            self._carregar_clientes_orcamento(preservar_selecao=False)
            cliente_label = ROTULO_SEM_CLIENTE_ORCAMENTO
        cliente_id = self.clientes_orcamento_map.get(cliente_label)

        valor_total = round(sum(float(i.get("total", 0.0)) for i in self.itens_carrinho), 2)
        valor_impostos = 0.0
        itens_preparados = []
        for item in self.itens_carrinho:
            resultado = calcular_impostos_liquidos(item.get("total", 0.0), item.get("ncm", ""))
            valor_impostos += resultado["valor_imposto"]
            itens_preparados.append((item, resultado))

        valor_impostos = round(valor_impostos, 2)
        valor_liquido = round(valor_total - valor_impostos, 2)
        if valor_liquido < 0:
            valor_liquido = 0.0

        from modulo_orcamento import OBSERVACOES_PADRAO_ORCAMENTO, solicitar_observacoes_orcamento

        observacao = solicitar_observacoes_orcamento(self, OBSERVACOES_PADRAO_ORCAMENTO)
        if observacao is None:
            return

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO orcamentos (
                        cliente_id, status, valor_total,
                        valor_impostos_retidos, valor_liquido, observacao
                    ) VALUES (?, 'ORCAMENTO', ?, ?, ?, ?)
                    """,
                    (cliente_id, valor_total, valor_impostos, valor_liquido, observacao),
                )
                orcamento_id = cursor.lastrowid

                for item, resultado in itens_preparados:
                    produto_id = item.get("id")
                    try:
                        produto_id = int(produto_id)
                    except Exception:
                        produto_id = None

                    cursor.execute(
                        """
                        INSERT INTO orcamento_itens (
                            orcamento_id, produto_id, codigo_barras, descricao_produto, ncm,
                            quantidade, unidade, valor_unitario, subtotal
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            orcamento_id,
                            produto_id,
                            str(item.get("barcode", "") or ""),
                            str(item.get("nome", "Item")),
                            str(item.get("ncm", "") or ""),
                            round(float(item.get("quantidade", 0) or 0), 3),
                            str(item.get("unidade", "UN") or "UN").upper(),
                            float(item.get("preco", 0.0) or 0.0),
                            float(item.get("total", 0.0) or 0.0),
                        ),
                    )

            self._set_status(
                f"Orçamento #{orcamento_id} salvo | Total: {self._formatar_moeda_br(valor_total)}",
                "#4aa3ff",
            )
            registrar_log(None, "PDV Orçamento", "Sucesso", f"Orçamento {orcamento_id} salvo para cliente {cliente_id or 'sem cliente'}")
            from modulo_orcamento import gerar_pdf_orcamento

            try:
                caminho_pdf = gerar_pdf_orcamento(
                    orcamento_id,
                    config=self.config,
                    parent=self,
                    notificar=False,
                )
                if caminho_pdf:
                    registrar_log(None, "PDV Orçamento", "Sucesso", f"PDF salvo: {caminho_pdf}")
                    self._set_status(
                        f"Orçamento #{orcamento_id} salvo | PDF: {caminho_pdf}",
                        "#4aa3ff",
                    )
                else:
                    self._set_status(
                        f"Orçamento #{orcamento_id} salvo; geração de PDF cancelada.",
                        "#f1c40f",
                    )
            except Exception as e:
                registrar_log(None, "PDV Orçamento", "Aviso", f"Orçamento {orcamento_id} salvo, mas PDF falhou: {e}")
                self._set_status(f"Orçamento #{orcamento_id} salvo; PDF não gerado: {e}", "#f1c40f")
            self._limpar_contexto_documental()
            self.itens_carrinho = []
            self._renderizar_carrinho()
            self.atualizar_total_display()
            self.ent_valor_pago.delete(0, "end")
            self.lbl_troco_venda.configure(text="R$ 0,00")
            self.limpar_pagamentos_recebidos()
        except Exception as e:
            self._set_status(f"Falha ao salvar orçamento: {e}", "#ff6666")
            registrar_log(None, "PDV Orçamento", "Falha", f"Erro ao salvar orçamento: {e}")

    def _limpar_contexto_documental(self):
        self._vales_para_quitar = []
        self._operacao_documento_tipo = None
        self._orcamento_para_vender_id = None
        self._operacao_vale_cliente_id = None

    def _garantir_cliente_para_vale(self):
        if hasattr(self, "combo_cliente_orcamento") and hasattr(self, "clientes_orcamento_map"):
            self._carregar_clientes_orcamento(preservar_selecao=True)
        cliente_id = self._cliente_selecionado_id()
        if cliente_id:
            return cliente_id
        self._set_status("Selecione um cliente ou use CADASTRO RÁPIDO.", "#f1c40f")
        return self.cadastrar_cliente_rapido()

    def salvar_vale_atual(self):
        if not self.itens_carrinho:
            self._set_status("Adicione itens antes de salvar o Vale.", "#ff6666")
            return None
        cliente_id = self._garantir_cliente_para_vale()
        if not cliente_id:
            return None
        total = round(sum(float(item.get("total", 0.0) or 0.0) for item in self.itens_carrinho), 2)
        try:
            with get_db_connection() as conn:
                numero = int(conn.execute("SELECT COALESCE(MAX(numero), 0) + 1 FROM vales").fetchone()[0])
                cursor = conn.execute(
                    "INSERT INTO vales (numero, cliente_id, status, total) VALUES (?, ?, 'PENDENTE', ?)",
                    (numero, int(cliente_id), total),
                )
                vale_id = int(cursor.lastrowid)
                for item in self.itens_carrinho:
                    try:
                        produto_id = int(item.get("id"))
                    except (TypeError, ValueError):
                        produto_id = None
                    conn.execute(
                        """
                        INSERT INTO vale_itens (
                            vale_id, produto_id, codigo_barras, descricao_produto, ncm,
                            quantidade, unidade, preco_unitario, subtotal
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            vale_id, produto_id, str(item.get("barcode", "") or ""),
                            str(item.get("nome", "Item")), str(item.get("ncm", "") or ""),
                            round(float(item.get("quantidade", 0.0) or 0.0), 3),
                            str(item.get("unidade", "UN") or "UN").upper(),
                            float(item.get("preco", 0.0) or 0.0),
                            float(item.get("total", 0.0) or 0.0),
                        ),
                    )
            self._limpar_contexto_documental()
            self.itens_carrinho = []
            self._renderizar_carrinho()
            self.atualizar_total_display()
            if hasattr(self, "ent_valor_pago"):
                self.ent_valor_pago.delete(0, "end")
            if hasattr(self, "lbl_troco_venda"):
                self.lbl_troco_venda.configure(text="R$ 0,00")
            if hasattr(self, "limpar_pagamentos_recebidos"):
                self.limpar_pagamentos_recebidos()
            try:
                self.imprimir_cupom_vale(vale_id)
            except Exception as e:
                registrar_log(None, "PDV Vale", "Aviso", f"Vale {numero} salvo, impressão falhou: {e}")
            registrar_log(None, "PDV Vale", "Sucesso", f"Vale {numero} salvo como PENDENTE para cliente {cliente_id}")
            self._set_status(f"Vale #{numero} salvo como PENDENTE.", "#2ecc71")
            return vale_id
        except Exception as e:
            self._set_status(f"Falha ao salvar Vale: {e}", "#ff6666")
            registrar_log(None, "PDV Vale", "Falha", f"Erro ao salvar: {e}")
            return None

    def abrir_tela_orcamentos(self):
        try:
            from modulo_orcamento import ModuloOrcamento

            ModuloOrcamento(self)
        except Exception as e:
            self._set_status(f"Erro ao abrir orçamentos: {e}", "#ff6666")

    def _aplicar_pedido_delivery(self, payload):
        if not isinstance(payload, dict):
            self._set_status("Webhook delivery recebido em formato inválido.", "#ff6666")
            return

        itens = payload.get("itens")
        cliente = str(payload.get("cliente", "Cliente Delivery")).strip() or "Cliente Delivery"
        valor_total_informado = payload.get("valor", None)
        pagamento_aprovado = self._normalizar_bool(payload.get("pagamento_aprovado"))
        origem_canal, origem_label = self._normalizar_origem_venda(payload)

        if not isinstance(itens, list) or not itens:
            self._set_status("Pedido delivery ignorado: sem itens.", "#ff6666")
            return

        self._alertar_novo_pedido(origem_label)

        if pagamento_aprovado:
            itens_resolvidos = []
            nao_resolvidos = []
            for idx, item in enumerate(itens, start=1):
                if not isinstance(item, dict):
                    continue
                try:
                    resolvido = self._resolver_item_delivery(item, idx)
                except Exception:
                    resolvido = None
                if resolvido:
                    itens_resolvidos.append(resolvido)
                else:
                    nome_item = str(item.get("nome") or item.get("descricao") or f"Item {idx}")
                    nao_resolvidos.append(nome_item)

            if itens_resolvidos and not nao_resolvidos:
                self.itens_carrinho = itens_resolvidos
                valor_pago = 0.0
                if valor_total_informado is not None:
                    try:
                        valor_pago = parse_numero(valor_total_informado, "Valor pago", permitir_vazio=True, default=0.0, minimo=0)
                    except Exception:
                        valor_pago = 0.0

                self.finalizar_venda_pdv(
                    "PIX",
                    valor_pago=valor_pago,
                    imprimir_cupom=False,
                    origem_venda=origem_canal,
                    status_pedido="APROVADO",
                    status_pagamento="PAGO",
                )
                self._set_status(f"Novo Pedido {origem_label} Chegou | Venda registrada automaticamente.", "#2ecc71")
                registrar_log(
                    None,
                    "Webhook Delivery",
                    "Sucesso",
                    f"Pedido {origem_label} pago e aprovado processado automaticamente. Cliente: {cliente}",
                )
                return

            self._set_status(
                f"Pedido {origem_label} pago recebido, mas itens sem cadastro: {', '.join(nao_resolvidos[:3])}",
                "#ff6666",
            )
            registrar_log(
                None,
                "Webhook Delivery",
                "Falha",
                f"Falha no processamento automático ({origem_label}). Itens não reconhecidos: {nao_resolvidos}",
            )

        adicionados = 0
        total_calculado = 0.0

        for idx, item in enumerate(itens, start=1):
            if not isinstance(item, dict):
                continue

            nome = str(item.get("nome") or item.get("descricao") or f"Item Delivery {idx}").strip()
            codigo = str(item.get("codigo") or item.get("id") or f"DEL-{idx}").strip()

            try:
                quantidade = parse_numero(item.get("quantidade", 1), "Quantidade", inteiro=True, minimo=1)
            except Exception:
                quantidade = 1
            quantidade = max(1, quantidade)

            try:
                preco = parse_numero(item.get("preco", 0), "Preço", permitir_vazio=True, default=0.0, minimo=0)
            except Exception:
                preco = 0.0
            preco = max(0.0, preco)

            total_item = round(preco * quantidade, 2)
            total_calculado += total_item

            self.itens_carrinho.append(
                {
                    "id": codigo,
                    "barcode": "",
                    "nome": nome,
                    "preco": preco,
                    "quantidade": quantidade,
                    "total": total_item,
                    "ncm": "",
                    "origem": "DELIVERY",
                    "cliente": cliente,
                }
            )
            adicionados += 1

        if adicionados == 0:
            self._set_status("Pedido delivery ignorado: itens inválidos.", "#ff6666")
            return

        self._renderizar_carrinho()
        self.atualizar_total_display()

        if valor_total_informado is not None:
            self._set_status(
                f"Pedido Delivery ({cliente}) adicionado com {adicionados} itens. Total informado: {self._formatar_moeda_br(valor_total_informado)}",
                "#4aa3ff",
            )
        else:
            self._set_status(
                f"Pedido Delivery ({cliente}) adicionado com {adicionados} itens. Total calculado: {self._formatar_moeda_br(total_calculado)}",
                "#4aa3ff",
            )

        registrar_log(
            None,
            "Webhook Delivery",
            "Sucesso",
            f"Pedido de {cliente} incluído no PDV. Itens: {adicionados}. Total: {self._formatar_moeda_br(total_calculado)}",
        )

    def processar_entrada_produto(self, event=None):
        entrada_original = self.ent_cod_barras.get().strip()
        entrada = entrada_original
        # Só limpa o campo após processamento bem-sucedido para evitar "colocar e apagar".
        processado_com_sucesso = False

        if not entrada:
            # Recuperação: o leitor pode ter bipado com o foco no campo de quantidade.
            # Resgata o código preso lá e processa como produto, sem abrir busca genérica.
            qtd_resgatada = self.ent_quantidade.get().strip() if hasattr(self, "ent_quantidade") else ""
            digitos_resgatados = "".join(ch for ch in qtd_resgatada if ch.isdigit())
            if len(digitos_resgatados) >= 4:
                self.ent_quantidade.delete(0, "end")
                self.ent_cod_barras.delete(0, "end")
                self.ent_cod_barras.insert(0, digitos_resgatados)
                entrada_original = digitos_resgatados
                entrada = digitos_resgatados
                self._set_status("Código resgatado do campo de quantidade. Processando...", "#f1c40f")
            else:
                return

        qtd_digitada = 1
        qtd_especificada = False
        qtd_texto = self.ent_quantidade.get().strip() if hasattr(self, "ent_quantidade") else ""
        if qtd_texto:
            try:
                qtd_digitada = parse_numero(qtd_texto, "Quantidade", inteiro=True, minimo=1)
                qtd_especificada = True
            except ValueError:
                self._set_status("Quantidade inválida. Informe número inteiro.", "#ff6666")
                # Em caso de erro, restaura o texto original no campo.
                self.ent_cod_barras.delete(0, "end")
                self.ent_cod_barras.insert(0, entrada_original)
                return

        # Suporte ao operador de multiplicação na leitura: "12*<bipagem>" (ex.: 12*7891234567).
        if "*" in entrada:
            parte_qtd, _, parte_produto = entrada.partition("*")
            parte_qtd = parte_qtd.strip()
            parte_produto = parte_produto.strip()
            if parte_qtd:
                try:
                    qtd_digitada = parse_numero(parte_qtd, "Quantidade", inteiro=True, minimo=1)
                    qtd_especificada = True
                except ValueError:
                    self._set_status("Quantidade inválida. Use ex.: 12*Código do produto.", "#ff6666")
                    self.ent_cod_barras.delete(0, "end")
                    self.ent_cod_barras.insert(0, entrada_original)
                    return
            if not parte_produto:
                # "12*" sem o código na sequência: pré-define a quantidade para a próxima bipagem.
                self.multiplicador_atual = qtd_digitada
                self.ent_cod_barras.delete(0, "end")
                self.ent_cod_barras.configure(placeholder_text=f"Qtd: {self.multiplicador_atual} x ...")
                self._set_status(f"Quantidade definida: {self.multiplicador_atual}. Bipa o produto agora.", "#f1c40f")
                self._safe_focus(self.ent_cod_barras)
                processado_com_sucesso = True
            else:
                entrada = parte_produto
        elif entrada.isdigit() and len(entrada) <= 3:
            # Primeiro tenta localizar um produto com este código de barras curto
            produto_curto = self.buscar_produto_por_ean(entrada)
            if produto_curto:
                # Produto encontrado → adiciona ao carrinho respeitando a
                # quantidade digitada no campo Qtd (ou o multiplicador atual).
                qtd_item_curto = qtd_digitada if qtd_especificada else self.multiplicador_atual
                self._adicionar_item_produto(produto_curto, qtd_item_curto)
                return
            # Caso não exista, mantém o comportamento original de definir multiplicador
            self.multiplicador_atual = int(entrada)
            self.ent_cod_barras.delete(0, "end")
            self.ent_cod_barras.configure(placeholder_text=f"Qtd: {self.multiplicador_atual} x ...")
            self._set_status(f"Quantidade definida: {self.multiplicador_atual}", "#f1c40f")
            processado_com_sucesso = True

        # Limpa o campo apenas quando o processamento foi bem-sucedido.
        if processado_com_sucesso:
            return

        # Campo de quantidade vazio assume 1 por padrão quando nada foi definido explicitamente.
        qtd_item = qtd_digitada if qtd_especificada else self.multiplicador_atual

        if entrada.isdigit():
            produto = self.buscar_produto_por_ean(entrada)
            if produto:
                self._adicionar_item_produto(produto, qtd_item)
                return
            # Etiqueta de balança Filizola (2|PPPPP|VVVVVV|DV): somente após a
            # busca exata falhar, para não alterar o fluxo normal de EAN.
            # PLU exato de 5 dígitos (sem normalizar zeros, sem fallback, sem LIKE).
            etiqueta = parse_etiqueta_balanca_filizola(entrada)
            if etiqueta is not None:
                produto_plu = self.buscar_produto_por_ean(etiqueta["plu"])
                if produto_plu:
                    self._adicionar_item_balanca(produto_plu, etiqueta["valor"])
                    return
                self._set_status(
                    f"Produto PLU {etiqueta['plu']} não cadastrado.",
                    "#ff6666",
                )
                self.multiplicador_atual = 1
                self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
                return
            if not produto:
                produtos = self.buscar_produtos_para_selecao(entrada)
                if not produtos:
                    self._tratar_produto_nao_cadastrado(entrada)
                    self.multiplicador_atual = 1
                    self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
                    return
                self._abrir_modal_selecao_produtos(entrada, produtos, qtd_item)
                return
            self._adicionar_item_produto(produto, qtd_item)
            return

        produtos = self.buscar_produtos_por_nome(entrada)
        if not produtos:
            self._set_status(f"Produto {entrada} não encontrado.", "#ff6666")
            self.multiplicador_atual = 1
            self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
            return
        # Para entrada textual, abre modal de seleção imediatamente.
        self._abrir_modal_selecao_produtos(entrada, produtos, qtd_item)

    def _processar_entrada_produto_tab(self, _event=None):
        # Venda pronta em DINHEIRO + campo vazio: TAB só navega para o
        # campo VALOR PAGO. Com texto digitado, o processamento de entrada
        # (produtos) acontece exatamente como antes.
        entrada = ""
        try:
            entrada = str(self.ent_cod_barras.get() or "").strip()
        except Exception:
            entrada = ""
        if not entrada and self._avanco_tab_para_valor_pago():
            self._safe_focus(self.ent_valor_pago)
            return "break"
        self.processar_entrada_produto()
        return "break"

    def _unidade_do_produto(self, produto):
        """Unidade canônica do produto (UN/KG) com fallback legado."""
        try:
            if isinstance(produto, (tuple, list)) and len(produto) > 10:
                uni = str(produto[10] or "").strip().upper()
                if uni in ("UN", "KG"):
                    return uni
        except Exception:
            pass
        try:
            variacao = produto[8] if isinstance(produto, (tuple, list)) and len(produto) > 8 else ""
            categoria = produto[9] if isinstance(produto, (tuple, list)) and len(produto) > 9 else ""
            nome = produto[2] if isinstance(produto, (tuple, list)) and len(produto) > 2 else ""
        except Exception:
            return "UN"
        try:
            from modulo_estoque import produto_e_vendido_por_kg
            if produto_e_vendido_por_kg(nome, variacao, categoria):
                return "KG"
        except Exception:
            pass
        return "UN"

    def _reconfigurar_mascara_qtd_pdv(self, unidade):
        """Máscara do campo Qtd do PDV coerente com a unidade (FASE 1 UN/KG).

        UN: inteiro (comportamento histórico — multiplicador 12* inclusive).
        KG: decimal até 3 casas (1,250).

        Só reaplica os bindings quando o modo MUDA: evita acumular
        <KeyRelease>/<FocusOut> a cada bipagem.
        """
        if not hasattr(self, "ent_quantidade"):
            return
        desejado = "KG" if str(unidade).strip().upper() == "KG" else "UN"
        if getattr(self, "_mascara_qtd_pdv", None) == desejado:
            return
        self._mascara_qtd_pdv = desejado
        try:
            self.ent_quantidade.unbind("<KeyRelease>")
            self.ent_quantidade.unbind("<FocusOut>")
        except Exception:
            pass
        try:
            if desejado == "KG":
                aplicar_padrao_entrada_numerica(self.ent_quantidade, inteiro=False, casas_decimais=3)
                try:
                    self.ent_quantidade.configure(placeholder_text="Qtd/Peso (KG)")
                except Exception:
                    pass
            else:
                aplicar_padrao_entrada_numerica(self.ent_quantidade, inteiro=True)
                try:
                    self.ent_quantidade.configure(placeholder_text="Qtd")
                except Exception:
                    pass
        except Exception:
            pass

    def _adicionar_item_produto(self, produto, qtd_item):
        if hasattr(self, "_unidade_do_produto"):
            unidade = self._unidade_do_produto(produto)
        else:
            unidade = ModuloPDV._unidade_do_produto(self, produto)
        # Normaliza o produto para tupla: busca_venda retorna tuplas; algumas
        # caminhos mais antigos podem passar (id, barcode, nome[, preco..., ...]).
        produto = produto if isinstance(produto, (tuple, list)) else tuple(produto)
        # 0=id,1=barcode,2=nome,3=preco_venda,7=ncm,8=variacao,9=categoria,10=unidade
        if len(produto) == 8:
            produto = produto + ("", "", "", "")
        elif len(produto) == 9:
            produto = produto + ("", "", "")
        elif len(produto) == 10:
            produto = produto + ("", "")

        try:
            preco_unitario = parse_numero(produto[3], "Preço", permitir_vazio=True, default=0.0, minimo=0)
        except ValueError:
            self._set_status(f"Preço inválido para o produto {produto[2]}.", "#ff6666")
            return
        preco_unitario = round(float(preco_unitario), 2)

        if unidade == "KG":
            # FASE 1 — produto vendido por KG: a quantidade é o PESO (kg),
            # decimal até 3 casas. Se o operador digitou um peso válido no
            # campo Qtd (diferente de 1, padrão vazio do PDV), reaproveita-o;
            # caso contrário pergunta o peso — nunca assume 1 KG silenciosamente.
            peso = None
            try:
                qtd_numerica = float(qtd_item) if qtd_item is not None else None
            except (TypeError, ValueError):
                qtd_numerica = None
            if qtd_numerica is not None and qtd_numerica > 0 and qtd_numerica != 1:
                peso = qtd_numerica
            if peso is None:
                peso = self._obter_peso_kg(produto)
                if peso is None:
                    return
            peso = round(float(peso), 3)
            if peso <= 0:
                self._set_status("Peso KG inválido. Informe um peso maior que zero.", "#ff6666")
                return
            item = {
                "id": produto[0],
                "barcode": produto[1] or "",
                "nome": produto[2],
                "preco": preco_unitario,
                "quantidade": peso,
                "total": round(peso * preco_unitario, 2),
                "ncm": produto[7] if len(produto) > 7 else "",
                "unidade": "KG",
                "origem": "BALCAO",
            }
        else:
            # FASE 1 — UN (preserva comportamento atual): qtd_item inteira,
            # multiplicador 12* intacto.
            item = {
                "id": produto[0],
                "barcode": produto[1] or "",
                "nome": produto[2],
                "preco": preco_unitario,
                "quantidade": qtd_item,
                "total": round(preco_unitario * qtd_item, 2),
                "ncm": produto[7] if len(produto) > 7 else "",
                "unidade": "UN",
                "origem": "BALCAO",
            }

        self.itens_carrinho.append(item)
        self._renderizar_carrinho()
        self.atualizar_total_display()
        if unidade == "KG":
            self._set_status(f"Item adicionado: {item['nome']} ({peso:g} KG)", "#2ecc71")
        else:
            self._set_status(f"Item adicionado: {item['nome']}", "#2ecc71")
        if hasattr(self, "_reconfigurar_mascara_qtd_pdv"):
            self._reconfigurar_mascara_qtd_pdv(unidade)
        self.multiplicador_atual = 1
        self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
        self.ent_cod_barras.delete(0, "end")
        self.ent_quantidade.delete(0, "end")
        # Devolve o foco ao campo principal para que a próxima bipagem não caia
        # no campo de quantidade (causa de busca genérica indevida).
        self._safe_focus(self.ent_cod_barras)

    def _adicionar_item_balanca(self, produto, valor_etiqueta):
        """Adiciona item de etiqueta de balança: qtd=1, preço = total da etiqueta.

        Não abre o diálogo de preço do KG e não reconstrói peso: a balança já
        calculou o total. Não cadastra o EAN completo da etiqueta no banco.
        """
        produto = produto if isinstance(produto, (tuple, list)) else tuple(produto)
        if len(produto) == 8:
            produto = produto + ("", "")
        try:
            preco_unitario = round(float(valor_etiqueta), 2)
        except (TypeError, ValueError):
            self._set_status("Valor da etiqueta de balança inválido.", "#ff6666")
            return
        if preco_unitario <= 0:
            self._set_status("Valor da etiqueta de balança inválido.", "#ff6666")
            return
        item = {
            "id": produto[0],
            "barcode": produto[1] or "",
            "nome": produto[2],
            "preco": preco_unitario,
            "quantidade": 1,
            "total": preco_unitario,
            "ncm": produto[7] if len(produto) > 7 else "",
            "unidade": "UN",
            "origem": "BALCAO",
        }
        self.itens_carrinho.append(item)
        self._renderizar_carrinho()
        self.atualizar_total_display()
        self._set_status(f"Item adicionado: {item['nome']}", "#2ecc71")
        if hasattr(self, "_reconfigurar_mascara_qtd_pdv"):
            self._reconfigurar_mascara_qtd_pdv("UN")
        self.multiplicador_atual = 1
        self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
        self.ent_cod_barras.delete(0, "end")
        self.ent_quantidade.delete(0, "end")
        self._safe_focus(self.ent_cod_barras)

    def _abrir_modal_selecao_produtos(self, termo, produtos, qtd_item):
        modal = ctk.CTkToplevel(self)
        modal.title("Selecionar Produto")
        modal.geometry("780x520")
        try:
            modal.grab_set()
        except Exception:
            # Janela ainda não visível: grab/foco são garantidos após renderização.
            pass

        ctk.CTkLabel(
            modal,
            text=f"Foram encontrados {len(produtos)} produtos para '{termo}'",
            font=("Arial", 14, "bold"),
        ).pack(pady=(14, 10), padx=12)

        frame_tabela = ctk.CTkFrame(modal)
        frame_tabela.pack(fill="both", expand=True, padx=12, pady=8)

        colunas = ("nome", "codigo", "preco")
        arvore = ttk.Treeview(frame_tabela, columns=colunas, show="headings", selectmode="browse", height=14)
        arvore.heading("nome", text="Nome do Produto")
        arvore.heading("codigo", text="Código")
        arvore.heading("preco", text="Preço")
        arvore.column("nome", width=420, anchor="w")
        arvore.column("codigo", width=180, anchor="w")
        arvore.column("preco", width=120, anchor="e")
        arvore.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(frame_tabela, orient="vertical", command=arvore.yview)
        scrollbar.pack(side="right", fill="y")
        arvore.configure(yscrollcommand=scrollbar.set)

        mapa_produtos = {}
        for idx, produto in enumerate(produtos):
            item_id = str(idx)
            mapa_produtos[item_id] = produto
            arvore.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    produto[2],
                    produto[1] or "(sem código)",
                    self._formatar_moeda_br(produto[3]),
                ),
            )

        if produtos:
            arvore.selection_set("0")
            arvore.focus("0")

        def selecionar_produto_teclado(_event=None):
            selecionados = arvore.selection()
            if not selecionados:
                return "break"

            produto = mapa_produtos.get(selecionados[0])
            if produto is None:
                return "break"

            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()
            self._adicionar_item_produto(produto, qtd_item)
            self._safe_focus(self.ent_cod_barras)
            return "break"

        def selecionar_produto_mouse(_event=None):
            return selecionar_produto_teclado()

        arvore.bind("<Return>", selecionar_produto_teclado)
        arvore.bind("<Double-1>", selecionar_produto_mouse)
        arvore.bind("<KP_Enter>", selecionar_produto_teclado)

        def fechar_modal():
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()
            self._safe_focus(self.ent_cod_barras)

        ctk.CTkLabel(
            modal,
            text="Seta para cima/baixo: navegar | Enter: selecionar | Esc: cancelar",
            text_color="#bfc7d5",
            font=("Arial", 10, "bold"),
        ).pack(pady=(2, 4))

        botoes = ctk.CTkFrame(modal, fg_color="transparent")
        botoes.pack(pady=(4, 12))
        ctk.CTkButton(botoes, text="Selecionar (Enter)", command=selecionar_produto_teclado).pack(side="left", padx=6)
        ctk.CTkButton(botoes, text="Cancelar", fg_color="#666666", command=fechar_modal).pack(side="left", padx=6)

        modal.bind("<Escape>", lambda _e: fechar_modal())
        modal.bind("<Return>", selecionar_produto_teclado)
        modal.protocol("WM_DELETE_WINDOW", fechar_modal)

        def _preparar_foco_modal():
            """Garante grab+foco no Treeview APÓS a janela ficar visível.

            Sem isto, no Windows o foco pode permanecer no campo de código do
            PDV: ↑/↓ não navegam e ENTER reprocessa a busca (abrindo outro
            modal) em vez de confirmar o produto selecionado.
            """
            try:
                if not modal.winfo_exists():
                    return
                try:
                    modal.grab_set()
                except Exception:
                    pass
                try:
                    filhos = arvore.get_children()
                    if filhos and not arvore.selection():
                        arvore.selection_set(filhos[0])
                        arvore.focus(filhos[0])
                    arvore.focus_force()
                    arvore.focus_set()
                except Exception:
                    pass
            except Exception:
                pass

        modal.after(120, _preparar_foco_modal)

    def _renderizar_carrinho(self):
        """Renderização otimizada com cache e batch update."""
        # Debounce: agenda refresh único se múltiplas adições rápidas ocorrerem
        if getattr(self, '_grid_pending_refresh', False):
            return

        self._grid_pending_refresh = True
        self._safe_after(50, self._executar_renderizacao_carrinho)

    def _executar_renderizacao_carrinho(self):
        """Executa a renderização real com otimizações de performance."""
        self._grid_pending_refresh = False
        
        try:
            # Limpa grid existente
            for widget in self.scroll_vendas.winfo_children():
                widget.destroy()

            self.item_selecionado_idx = None
            
            # Batch render: processa todos os itens de uma vez
            for idx, item in enumerate(self.itens_carrinho):
                self._adicionar_linha_grid_otimizado(item, idx)
                
        except Exception as e:
            registrar_log(None, "PDV Grid", "Erro", f"Erro ao renderizar carrinho: {e}")

    def _adicionar_linha_grid_otimizado(self, item, idx):
        """Versão otimizada com cache de imagens e operações mínimas."""
        row = ctk.CTkFrame(self.scroll_vendas, fg_color="transparent")
        row.pack(fill="x", pady=2)

        origem = str(item.get("origem", "BALCAO")).upper()
        cor_base = "#143350" if origem == "DELIVERY" else "transparent"
        row.configure(fg_color=cor_base)
        row._cor_base = cor_base

        # Cache de seleção para evitar recriar funções
        def selecionar(_event=None, r=row, cor=cor_base):
            self.item_selecionado_idx = idx
            for filho in self.scroll_vendas.winfo_children():
                try:
                    cor_filho = getattr(filho, "_cor_base", "transparent")
                    filho.configure(fg_color=cor_filho)
                except Exception:
                    pass
            r.configure(fg_color="#2a2a2a")
            self._set_status(f"Item selecionado: {item['nome']}", "#f1c40f")

        # Busca imagem do cache
        barcode = item.get("barcode", "default")
        cache_key = f"{barcode}_{id(item)}"
        
        if cache_key in getattr(self, '_cache_imagens_produtos', {}):
            ctk_img = self._cache_imagens_produtos[cache_key]
        else:
            caminho_img = self.buscar_imagem_produto(barcode)
            try:
                img_pil = Image.open(caminho_img) if caminho_img else None
                ctk_img = ctk.CTkImage(light_image=img_pil, dark_image=img_pil, size=(28, 28)) if img_pil else None
            except Exception:
                ctk_img = None
            
            if not hasattr(self, '_cache_imagens_produtos'):
                self._cache_imagens_produtos = {}
            self._cache_imagens_produtos[cache_key] = ctk_img

        # Cria labels
        lbl_id = ctk.CTkLabel(row, text=str(item["id"]), width=130)
        # Prefixo de origem só quando relevante (delivery/diversos);
        # itens de balcão não exibem "[BALCAO]" (apenas apresentação).
        prefixo = f"[{origem}] " if origem not in ("BALCAO", "LOJA_FISICA") else ""
        lbl_nome = ctk.CTkLabel(row, text=f"{prefixo}{item['nome']}", width=540, anchor="w")
        lbl_qtd = ctk.CTkLabel(row, text=self._formatar_quantidade_cupom(item.get("quantidade"), item.get("unidade")), width=90)
        lbl_total = ctk.CTkLabel(row, text=self._formatar_moeda_br(item["total"]), width=140, font=("Roboto", 12, "bold"))
        
        for w in (lbl_id, lbl_nome, lbl_qtd, lbl_total):
            w.pack(side="left", padx=5)
            w.bind("<Button-1>", selecionar)

        # Imagem
        lbl_img = ctk.CTkLabel(row, image=ctk_img, text="[img]" if not ctk_img else "", width=55)
        lbl_img.pack(side="left", padx=5)
        lbl_img.bind("<Button-1>", selecionar)

        # Scroll para o último item (apenas uma vez)
        try:
            if self.scroll_vendas.winfo_exists():
                self.scroll_vendas._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _adicionar_linha_grid(self, item, idx):
        row = ctk.CTkFrame(self.scroll_vendas, fg_color="transparent")
        row.pack(fill="x", pady=2)

        origem = str(item.get("origem", "BALCAO")).upper()
        cor_base = "#143350" if origem == "DELIVERY" else "transparent"
        row.configure(fg_color=cor_base)
        row._cor_base = cor_base

        def selecionar(_event=None):
            self.item_selecionado_idx = idx
            for filho in self.scroll_vendas.winfo_children():
                try:
                    cor_filho = getattr(filho, "_cor_base", "transparent")
                    filho.configure(fg_color=cor_filho)
                except Exception:
                    pass
            row.configure(fg_color="#2a2a2a")
            self._set_status(f"Item selecionado: {item['nome']}", "#f1c40f")

        barcode = item.get("barcode", "default")
        caminho_img = self.buscar_imagem_produto(barcode)

        prefixo_otimizado = f"[{origem}] " if origem not in ("BALCAO", "LOJA_FISICA") else ""
        widgets = [
            ctk.CTkLabel(row, text=str(item["id"]), width=130),
            ctk.CTkLabel(row, text=f"{prefixo_otimizado}{item['nome']}", width=540, anchor="w"),
            ctk.CTkLabel(row, text=str(item["quantidade"]), width=90),
            ctk.CTkLabel(row, text=self._formatar_moeda_br(item["total"]), width=140, font=("Roboto", 12, "bold")),
        ]
        for w in widgets:
            w.pack(side="left", padx=5)
            w.bind("<Button-1>", selecionar)

        try:
            img_pil = Image.open(caminho_img) if caminho_img else None
            ctk_img = ctk.CTkImage(light_image=img_pil, dark_image=img_pil, size=(28, 28)) if img_pil else None
            lbl_img = ctk.CTkLabel(row, image=ctk_img, text="[img]" if not ctk_img else "", width=55)
        except Exception:
            lbl_img = ctk.CTkLabel(row, text="[img]", width=55)

        lbl_img.pack(side="left", padx=5)
        lbl_img.bind("<Button-1>", selecionar)

        try:
            if self.scroll_vendas.winfo_exists():
                self.scroll_vendas._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def atualizar_total_display(self):
        total = sum(i["total"] for i in self.itens_carrinho)
        self.lbl_total_venda.configure(text=self._formatar_moeda_br(total))
        self.atualizar_troco_display()

    def _ler_valor_pago_digitado(self):
        if not hasattr(self, "ent_valor_pago"):
            return None

        txt = self.ent_valor_pago.get().strip()
        if not txt:
            return 0.0
        try:
            return parse_numero(txt, "Valor pago", permitir_vazio=True, default=0.0, minimo=0)
        except ValueError:
            return None

    def atualizar_troco_display(self):
        if not hasattr(self, "lbl_troco_venda"):
            return

        total = sum(i["total"] for i in self.itens_carrinho)

        valor_digitado = self._ler_valor_pago_digitado()
        if valor_digitado is None:
            valor_digitado = 0.0

        # Total efetivamente recebido: acumulado + valor ainda digitado no campo.
        valor_pago = round(self.valor_pago_acumulado + valor_digitado, 2)

        troco = max(0.0, round(valor_pago - total, 2))
        # Tela do cliente: só o valor. O título da coluna ("TROCO" /
        # "VALOR PAGO") já identifica cada informação do painel grande.
        self.lbl_troco_venda.configure(text=self._formatar_moeda_br(troco))

        if hasattr(self, "lbl_pago_venda"):
            self.lbl_pago_venda.configure(text=self._formatar_moeda_br(valor_pago))
        if hasattr(self, "lbl_restante_venda"):
            restante = max(0.0, round(total - valor_pago, 2))
            if restante > 0:
                self.lbl_restante_venda.configure(text=f"RESTANTE {self._formatar_moeda_br(restante)}")
            else:
                self.lbl_restante_venda.configure(text="")

    def acrescentar_valor_pago(self):
        """Acumula o valor digitado no total efetivamente recebido (regras 2 e 3).

        O valor digitado representa o valor recebido nesta forma de pagamento;
        soma-se ao valor pago acumulado. Se o recebido superar o total da venda,
        a diferença é troco e aparece no display próprio (regra 5). O total da
        venda não é alterado (regra 1).
        """
        total = sum(i["total"] for i in self.itens_carrinho)
        valor_digitado = self._ler_valor_pago_digitado()

        if valor_digitado is None:
            self._set_status("Valor pago inválido.", "#ff6666")
            return

        if valor_digitado <= 0:
            self._set_status("Informe o valor recebido para acrescentar ao pagamento.", "#f39c12")
            return

        self.valor_pago_acumulado = round(self.valor_pago_acumulado + valor_digitado, 2)
        self.pagamentos_parciais.append((self.forma_pagamento_selecionada, valor_digitado))

        if self.valor_pago_acumulado > total:
            troco = round(self.valor_pago_acumulado - total, 2)
            self._set_status(
                f"Pagamento de {self._formatar_moeda_br(valor_digitado)} registrado "
                f"({self.forma_pagamento_selecionada}). Troco: {self._formatar_moeda_br(troco)}",
                "#2ecc71",
            )
        elif self.valor_pago_acumulado == total:
            self._set_status(
                f"Pagamento de {self._formatar_moeda_br(valor_digitado)} registrado "
                f"({self.forma_pagamento_selecionada}). Venda totalmente paga.",
                "#2ecc71",
            )
        else:
            restante = round(total - self.valor_pago_acumulado, 2)
            self._set_status(
                f"Pagamento de {self._formatar_moeda_br(valor_digitado)} registrado "
                f"({self.forma_pagamento_selecionada}). Restante: {self._formatar_moeda_br(restante)}",
                "#2ecc71",
            )

        self.ent_valor_pago.delete(0, "end")
        self.atualizar_troco_display()
        # FLUXO DINHEIRO (usabilidade): ENTER confirmou o valor do recebido
        # e o foco segue para ACRESCENTAR PAGAMENTO. Demais formas de
        # pagamento: comportamento inalterado.
        if str(self.forma_pagamento_selecionada or "").strip().upper() == "DINHEIRO" and hasattr(self, "_safe_focus"):
            self._safe_focus(self.btn_acrescentar_pago)

    def limpar_pagamentos_recebidos(self):
        """Zera o total pago acumulado ao encerrar a venda atual (regra 2)."""
        self.valor_pago_acumulado = 0.0
        self.pagamentos_parciais = []
        if hasattr(self, "ent_valor_pago"):
            self.ent_valor_pago.delete(0, "end")
        self.atualizar_troco_display()

    def _validar_divisao_pagamento(self, detalhes, total):
        """Valida a divisão do total entre formas no Múltiplo Pagamento.

        ``detalhes`` é [(forma, valor)] com valores já parseados (> 0).
        Reutiliza as regras existentes de pagamento: nenhuma regra de troco
        paralela é criada — excesso acima do total só é aceito quando há
        DINHEIRO na divisão (troco segue o fluxo existente, regra 5).

        Retorna (ok: bool, mensagem: str, soma: float).
        """
        total = round(float(total or 0.0), 2)
        if not detalhes:
            return False, "Informe o valor de pelo menos uma forma de pagamento.", 0.0

        soma = round(sum(v for _f, v in detalhes), 2)
        tem_dinheiro = any(str(f).strip().upper() == "DINHEIRO" for f, _v in detalhes)

        if soma < total - 0.0049:
            restante = round(total - soma, 2)
            return (
                False,
                f"Soma {self._formatar_moeda_br(soma)} menor que o total "
                f"{self._formatar_moeda_br(total)}. Restante: {self._formatar_moeda_br(restante)}.",
                soma,
            )

        if soma > total + 0.0049 and not tem_dinheiro:
            excesso = round(soma - total, 2)
            return (
                False,
                f"Soma {self._formatar_moeda_br(soma)} excede o total "
                f"{self._formatar_moeda_br(total)} (excesso {self._formatar_moeda_br(excesso)}) "
                "e não há DINHEIRO na divisão para gerar troco.",
                soma,
            )

        return True, "", soma

    def abrir_modal_pagamento_multiplo(self):
        """Popup de MÚLTIPLO PAGAMENTO.

        O botão visual deste recurso e o atalho F8 foram removidos do painel
        em 25/09/2026 por decisão de interface; o F8 passou a acionar
        ``salvar_vale_atual``. A implementação abaixo permanece integralmente
        intacta e continua disponível para qualquer reaproveitamento: nenhuma
        regra de pagamento, condição ou fluxo interno foi alterado.

        Reaproveita integralmente a estrutura existente de pagamentos:
        ``pagamentos_parciais`` e ``valor_pago_acumulado`` são repovoados
        atomicamente com a divisão confirmada (nenhum segundo sistema é
        criado) e a finalização passa pelo fluxo existente
        (``finalizar_venda_com_confirmacoes`` → ``_validar_e_obter_valor_pagamento``
        → ``finalizar_venda_pdv`` → ``_resolver_forma_pagamento_registro``,
        que registra "MISTO" quando há mais de uma forma).

        - restante recalculado dinamicamente;
        - PIX/DÉBITO/CRÉDITO/VOUCHER sugerem o restante ao serem escolhidos;
        - DINHEIRO mantém entrada manual do valor recebido;
        - não confirma abaixo do total; excesso só com DINHEIRO (troco).
        """
        if not self.itens_carrinho:
            self._set_status("Adicione itens antes de abrir o Múltiplo Pagamento.", "#ff6666")
            return

        total = round(sum(i["total"] for i in self.itens_carrinho), 2)
        FORMAS = ["DINHEIRO", "PIX", "DEBITO", "CREDITO", "VOUCHER"]

        modal = ctk.CTkToplevel(self)
        modal.title("MÚLTIPLO PAGAMENTO")
        modal.geometry("520x520")
        modal.resizable(False, False)
        try:
            modal.transient(self)
            modal.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            modal,
            text=f"TOTAL DA VENDA: {self._formatar_moeda_br(total)}",
            font=("Roboto", 16, "bold"),
        ).pack(pady=(14, 2))

        lbl_restante = ctk.CTkLabel(modal, text="", font=("Roboto", 14, "bold"))
        lbl_restante.pack(pady=(0, 6))

        area_formas = ctk.CTkFrame(modal, fg_color="transparent")
        area_formas.pack(fill="both", expand=True, padx=10, pady=(2, 2))

        pagamentos = []  # [{"forma": str, "ent": CTkEntry}]

        def _parse_campo(txt):
            try:
                return parse_numero(txt, "Valor", permitir_vazio=True, default=0.0, minimo=0)
            except ValueError:
                return None

        def _soma():
            soma = 0.0
            for est in pagamentos:
                v = _parse_campo(est["ent"].get())
                if v is not None:
                    soma += v
            return round(soma, 2)

        def _atualizar_display():
            soma = _soma()
            restante = round(total - soma, 2)
            if abs(restante) <= 0.0049:
                lbl_restante.configure(
                    text=f"RESTANTE: {self._formatar_moeda_br(0.0)}",
                    text_color="#2ecc71",
                )
            elif restante > 0:
                lbl_restante.configure(
                    text=f"RESTANTE: {self._formatar_moeda_br(restante)}",
                    text_color="#f39c12",
                )
            else:
                lbl_restante.configure(
                    text=f"EXCESSO (TROCO): {self._formatar_moeda_br(-restante)}",
                    text_color="#3498db",
                )

        def _adicionar_linha(forma_inicial="DINHEIRO", valor_inicial=""):
            if len(pagamentos) >= len(FORMAS):
                return
            linha = ctk.CTkFrame(area_formas, fg_color="transparent")
            linha.pack(fill="x", padx=8, pady=3)

            estado = {"forma": forma_inicial}
            ent_valor = ctk.CTkEntry(linha, width=130, placeholder_text="0,00")
            if valor_inicial:
                ent_valor.insert(0, valor_inicial)

            def _trocar_forma(escolha):
                estado["forma"] = escolha
                # Sugestão automática: formas não monetárias preenchem o
                # restante (quitação integral sem digitação). DINHEIRO
                # preserva entrada manual do valor recebido.
                if escolha != "DINHEIRO" and not ent_valor.get().strip():
                    restante = round(total - _soma(), 2)
                    if restante > 0:
                        ent_valor.insert(0, f"{restante:.2f}".replace(".", ","))
                _atualizar_display()

            menu = ctk.CTkOptionMenu(linha, values=FORMAS, width=170, command=_trocar_forma)
            menu.set(forma_inicial)
            menu.pack(side="left", padx=(6, 4))
            ent_valor.pack(side="left", padx=4)
            ent_valor.bind("<KeyRelease>", lambda _e: _atualizar_display())

            estado["ent"] = ent_valor
            estado["menu"] = menu
            pagamentos.append(estado)

        def _remover_ultima():
            if not pagamentos:
                return
            est = pagamentos.pop()
            try:
                est["menu"].destroy()
                est["ent"].destroy()
            except Exception:
                pass
            _atualizar_display()

        # Pré-carrega pagamentos já acumulados no fluxo normal (F1-F5 +
        # ACRESCENTAR PAGAMENTO), preservando-os na divisão.
        iniciais = [
            (str(f or "").strip().upper(), v)
            for f, v in (getattr(self, "pagamentos_parciais", None) or [])
        ]
        if iniciais:
            for forma, valor in iniciais:
                _adicionar_linha(
                    forma if forma in FORMAS else "DINHEIRO",
                    f"{float(valor):.2f}".replace(".", ","),
                )
        else:
            _adicionar_linha("DINHEIRO", "")

        botoes = ctk.CTkFrame(modal, fg_color="transparent")
        botoes.pack(fill="x", padx=10, pady=(2, 4))

        ctk.CTkButton(
            botoes,
            text="+ ADICIONAR FORMA",
            width=160,
            fg_color="#2c3e50",
            command=lambda: (_adicionar_linha("PIX", ""), _atualizar_display()),
        ).pack(side="left", padx=4, pady=4)

        ctk.CTkButton(
            botoes,
            text="REMOVER ÚLTIMA",
            width=140,
            fg_color="#7f8c8d",
            command=_remover_ultima,
        ).pack(side="left", padx=4, pady=4)

        def _confirmar():
            detalhes = []
            for est in pagamentos:
                v = _parse_campo(est["ent"].get())
                if v is None:
                    self._set_status(f"Valor inválido na forma {est['forma']}.", "#ff6666")
                    try:
                        est["ent"].focus_set()
                    except Exception:
                        pass
                    return
                if v > 0:
                    detalhes.append((est["forma"], round(v, 2)))

            ok, msg, soma = self._validar_divisao_pagamento(detalhes, total)
            if not ok:
                self._set_status(msg, "#ff6666")
                return

            # Repõe ATOMICAMENTE as estruturas existentes de pagamento —
            # nenhum sistema paralelo é criado; a forma gravada continua
            # sendo resolvida por _resolver_forma_pagamento_registro ("MISTO").
            self.limpar_pagamentos_recebidos()
            for forma, valor in detalhes:
                self.pagamentos_parciais.append((forma, valor))
            self.valor_pago_acumulado = soma
            if hasattr(self, "ent_valor_pago"):
                self.ent_valor_pago.delete(0, "end")  # evita dupla contagem
            self.atualizar_troco_display()

            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()
            self.finalizar_venda_com_confirmacoes()

        def _cancelar(_e=None):
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()

        ctk.CTkButton(
            botoes,
            text="CONFIRMAR",
            width=180,
            fg_color="#27ae60",
            height=34,
            font=("Roboto", 13, "bold"),
            command=_confirmar,
        ).pack(side="right", padx=4, pady=4)

        ctk.CTkButton(
            botoes,
            text="CANCELAR",
            width=110,
            fg_color="#4a4a4a",
            command=_cancelar,
        ).pack(side="right", padx=4, pady=4)

        modal.bind("<Escape>", _cancelar)
        _atualizar_display()

    def abrir_modal_diversos(self):
        """Cobrança avulsa (DIVERSOS): soma ao TOTAL DA VENDA (não é pagamento)."""
        modal = ctk.CTkToplevel(self)
        modal.title("DIVERSOS - Item avulso")
        modal.geometry("380x270")
        modal.resizable(False, False)
        try:
            modal.transient(self)
            modal.grab_set()
        except Exception:
            pass

        # Pré-preenchimento (somente leitura): reaproveita o Qtd do PDV se válido.
        # Não consome e não limpa ent_quantidade nesta versão.
        qtd_inicial = "1"
        try:
            if hasattr(self, "ent_quantidade") and self.ent_quantidade.winfo_exists():
                txt_qtd = self.ent_quantidade.get().strip()
                if txt_qtd:
                    qtd_pre = parse_numero(txt_qtd, "Quantidade", inteiro=True, minimo=1)
                    qtd_inicial = str(qtd_pre)
        except Exception:
            qtd_inicial = "1"

        ctk.CTkLabel(modal, text="QUANTIDADE", font=("Roboto", 12, "bold")).pack(pady=(14, 2))
        ent_qtd = ctk.CTkEntry(modal, width=150, placeholder_text="1")
        ent_qtd.pack(padx=14, pady=(0, 8))
        aplicar_padrao_entrada_numerica(ent_qtd, inteiro=True)
        ent_qtd.delete(0, "end")
        ent_qtd.insert(0, qtd_inicial)

        ctk.CTkLabel(modal, text="VALOR (R$) UNITÁRIO", font=("Roboto", 12, "bold")).pack(pady=(0, 2))
        ent_valor = ctk.CTkEntry(modal, width=150, placeholder_text="0,00")
        ent_valor.pack(padx=14, pady=(0, 10))
        aplicar_padrao_entrada_numerica(ent_valor, inteiro=False, casas_decimais=2)

        def confirmar(_event=None):
            try:
                qtd = parse_numero(ent_qtd.get(), "Quantidade DIVERSOS", inteiro=True, minimo=1)
            except ValueError:
                self._set_status("Quantidade DIVERSOS inválida. Informe número inteiro maior que zero.", "#ff6666")
                return
            try:
                valor = parse_numero(ent_valor.get(), "Valor DIVERSOS", permitir_vazio=False, minimo=0.01)
            except ValueError:
                self._set_status("Valor DIVERSOS inválido. Informe um valor maior que zero.", "#ff6666")
                return
            preco_unitario = round(valor, 2)
            total_item = round(qtd * preco_unitario, 2)
            self.itens_carrinho.append(
                {
                    "id": "DIVERSOS",
                    "barcode": "",
                    "nome": "DIVERSOS",
                    "preco": preco_unitario,
                    "quantidade": qtd,
                    "total": total_item,
                    "ncm": "",
                    "unidade": "UN",
                    "origem": "DIVERSOS",
                }
            )
            self._renderizar_carrinho()
            self.atualizar_total_display()
            self._set_status(f"DIVERSOS adicionado: {qtd} x {self._formatar_moeda_br(preco_unitario)}", "#2ecc71")
            modal.destroy()
            self._safe_focus(self.ent_cod_barras)

        ctk.CTkButton(modal, text="CONFIRMAR (Enter)", fg_color="#8e44ad", command=confirmar).pack(pady=(4, 10), padx=14, fill="x")
        ent_qtd.bind("<Return>", confirmar)
        ent_valor.bind("<Return>", confirmar)
        self._safe_focus(ent_qtd)

    def buscar_produto_venda(self, codigo_barras):
        hoje = datetime.now().strftime("%Y-%m-%d")
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao, ncm, variacao, categoria, unidade
                    FROM produtos WHERE codigo_barras = ?
                    """,
                    (codigo_barras,),
                )
                p = cursor.fetchone()
        except Exception as e:
            self._set_status(f"Falha ao buscar produto: {e}", "#ff6666")
            registrar_log(None, "PDV", "Falha", f"Erro em buscar_produto_venda: {e}")
            return None

        if p and p[5] and p[6] and p[5] <= hoje <= p[6]:
                        registrar_log(None, "PDV", "Info", f"Preço promocional aplicado: {p[2]}")
        return p

    def buscar_produto_por_ean(self, codigo_ean):
        """Busca rápida por EAN (somente dígitos)."""
        ean = "".join(ch for ch in str(codigo_ean or "").strip() if ch.isdigit())
        if not ean:
            return None
        return self.buscar_produto_venda(ean)

    def _unidade_do_produto(self, produto):
        """Lê a unidade do produto retornado por buscar_produto_venda/buscar_produto_por_ean.

        SELECT atual: (id, codigo_barras, nome, preco_venda, preco_base,
        inicio_promocao, fim_promocao, ncm, variacao, categoria, unidade)
        unidade é o índice 10; com fallback legado (produto_e_vendido_por_kg)
        para bancos antigos sem a coluna preenchida.
        """
        if not produto:
            return "UN"
        try:
            unidade = str(produto[10] or "UN").strip().upper() or "UN"
        except IndexError:
            unidade = "UN"
        if unidade in ("UN", "KG"):
            return unidade
        # Fallback legado para produtos cadastrados antes da coluna unidade.
        try:
            nome = str(produto[2] or "")
            variacao = str(produto[8] or "")
            categoria = str(produto[9] or "")
        except IndexError:
            return "UN"
        return "KG" if produto_e_vendido_por_kg(nome, variacao, categoria) else "UN"

    def _reconfigurar_mascara_qtd_pdv(self, unidade):
        """Máscara do campo Qtd do PDV coerente com a unidade (FASE 1 UN/KG).

        UN: inteiro (comportamento histórico — multiplicador 12* inclusive).
        KG: decimal até 3 casas (1,250).

        Só reaplica os bindings quando o modo MUDA: evita acumular
        <KeyRelease>/<FocusOut> a cada bipagem.
        """
        if not hasattr(self, "ent_quantidade"):
            return
        desejado = "KG" if str(unidade).strip().upper() == "KG" else "UN"
        if getattr(self, "_mascara_qtd_pdv", None) == desejado:
            return
        self._mascara_qtd_pdv = desejado
        try:
            self.ent_quantidade.unbind("<KeyRelease>")
            self.ent_quantidade.unbind("<FocusOut>")
        except Exception:
            pass
        if desejado == "KG":
            aplicar_padrao_entrada_numerica(self.ent_quantidade, inteiro=False, casas_decimais=3)
            try:
                self.ent_quantidade.configure(placeholder_text="Qtd/Peso (KG)")
            except Exception:
                pass
        else:
            aplicar_padrao_entrada_numerica(self.ent_quantidade, inteiro=True)
            try:
                self.ent_quantidade.configure(placeholder_text="Qtd")
            except Exception:
                pass

    def buscar_imagem_produto(self, ean):
        """Localiza a imagem do produto por EAN (DB → assets locais).

        Correção 1.0.16: este corpo estava definido erroneamente como
        '_adicionar_item_produto' (def duplicada), o que sobrescrevia o
        verdadeiro método de adição ao carrinho (linha ~1189) e fazia NADA ser
        adicionado ao carrinho (bipagem e busca manual quebradas), além de
        deixar 'buscar_imagem_produto' inexistente (chamado na renderização).
        """
        ean_txt = str(ean or "").strip()
        if not ean_txt:
            return None

        with get_db_connection() as conn:
            produto = conn.execute(
                "SELECT imagem_path FROM produtos WHERE codigo_barras = ?",
                (ean_txt,),
            ).fetchone()
        if produto and produto[0] and os.path.isfile(produto[0]):
            return produto[0]

        pastas = [
            os.path.join(os.getcwd(), "assets", "produtos"),
            obter_caminho_dados("assets", "produtos"),
        ]
        for pasta in pastas:
            for ext in [".jpg", ".jpeg", ".png"]:
                caminho = os.path.join(pasta, f"{ean_txt}{ext}")
                if os.path.exists(caminho):
                    return caminho
        return None

    def _salvar_imagem_produto_por_ean(self, origem_imagem, ean):
        if not origem_imagem or not os.path.exists(origem_imagem):
            return ""

        pasta_destino = obter_caminho_dados("assets", "produtos")
        os.makedirs(pasta_destino, exist_ok=True)
        destino = os.path.join(pasta_destino, f"{ean}.jpg")

        try:
            img = Image.open(origem_imagem)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(destino, format="JPEG", quality=92)
            return destino
        except Exception:
            try:
                shutil.copy2(origem_imagem, destino)
                return destino
            except Exception:
                return ""

    def _normalizar_path_drop(self, valor):
        txt = str(valor or "").strip()
        if not txt:
            return ""
        if txt.startswith("{") and txt.endswith("}"):
            txt = txt[1:-1]
        return txt.strip().strip('"')

    def _habilitar_tkdnd_widget(self, widget, callback_drop):
        """
        Habilita DnD explícito no Windows via tkinterdnd2 (preferencial)
        ou via package tkdnd quando disponível no runtime.
        """
        if os.name != "nt":
            return False

        dnd_files_token = "DND_Files"
        try:
            import importlib

            tkdnd_mod = importlib.import_module("tkinterdnd2")
            dnd_files_token = getattr(tkdnd_mod, "DND_FILES", "DND_Files")
        except Exception:
            dnd_files_token = "DND_Files"

        # Caminho preferencial: métodos injetados por tkinterdnd2.
        try:
            if hasattr(widget, "drop_target_register") and hasattr(widget, "dnd_bind"):
                widget.drop_target_register(dnd_files_token)
                widget.dnd_bind("<<Drop>>", callback_drop)
                return True
        except Exception:
            pass

        # Fallback: bind direto no Tk com package tkdnd.
        try:
            widget.tk.call("package", "require", "tkdnd")
            widget.tk.call("tkdnd::drop_target", "register", widget._w, dnd_files_token)

            def _bridge_drop(data):
                evt = type("DropEvt", (), {"data": data})()
                callback_drop(evt)
                return "break"

            cmd = widget.register(_bridge_drop)
            widget.tk.call("bind", widget._w, "<<Drop:DND_Files>>", f"{cmd} %D")
            widget.tk.call("bind", widget._w, "<<Drop>>", f"{cmd} %D")
            return True
        except Exception:
            return False

    def _listar_xml_candidatos(self):
        pastas = [
            os.path.join(os.getcwd(), "fiscal_in"),
            os.path.join(os.getcwd(), "exportacao_fiscal"),
            obter_caminho_dados("fiscal_in"),
            obter_caminho_dados("exportacao_fiscal"),
        ]
        candidatos = []
        vistos = set()

        for pasta in pastas:
            try:
                if not os.path.isdir(pasta):
                    continue
                for nome in os.listdir(pasta):
                    if not nome.lower().endswith(".xml"):
                        continue
                    caminho = os.path.abspath(os.path.join(pasta, nome))
                    if caminho in vistos:
                        continue
                    vistos.add(caminho)
                    candidatos.append(caminho)
            except Exception:
                continue

        candidatos.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return candidatos[:25]

    def _buscar_dados_cache_xml_por_ean(self, ean):
        ean_txt = str(ean or "").strip()
        if not ean_txt:
            return None

        if ean_txt in self._cache_xml_por_ean:
            return self._cache_xml_por_ean.get(ean_txt)

        for xml_path in self._listar_xml_candidatos():
            try:
                dados_xml = self.fiscal_manager.processar_xml_entrada(xml_path)
                for item in dados_xml.get("itens", []):
                    item_ean = str(item.get("ean") or "").strip()
                    if not item_ean:
                        continue
                    self._cache_xml_por_ean[item_ean] = item
            except Exception:
                continue

            if ean_txt in self._cache_xml_por_ean:
                return self._cache_xml_por_ean.get(ean_txt)

        return None

    def _abrir_modal_cadastro_rapido_produto(self, ean):
        dados_xml = self._buscar_dados_cache_xml_por_ean(ean) or {}
        imagem_existente = self.buscar_imagem_produto(ean)

        modal = ctk.CTkToplevel(self)
        modal.title("Cadastro Rápido de Produto")
        modal.geometry("560x620")
        modal.transient(self)
        modal.grab_set()

        ctk.CTkLabel(modal, text="Produto não cadastrado", font=("Arial", 18, "bold"), text_color="#ff6666").pack(pady=(12, 6))
        ctk.CTkLabel(modal, text="Finalize o cadastro para continuar a venda.", font=("Arial", 11)).pack(pady=(0, 10))

        form = ctk.CTkFrame(modal)
        form.pack(fill="both", expand=True, padx=14, pady=10)

        ctk.CTkLabel(form, text="Código de Barras (EAN):").pack(anchor="w", padx=12, pady=(12, 2))
        entry_ean = ctk.CTkEntry(form)
        entry_ean.pack(fill="x", padx=12)
        entry_ean.insert(0, str(ean))

        ctk.CTkLabel(form, text="Nome do Produto:").pack(anchor="w", padx=12, pady=(10, 2))
        entry_nome = ctk.CTkEntry(form)
        entry_nome.pack(fill="x", padx=12)
        entry_nome.insert(0, str(dados_xml.get("descricao") or f"Produto {ean}"))

        ctk.CTkLabel(form, text="NCM:").pack(anchor="w", padx=12, pady=(10, 2))
        entry_ncm = ctk.CTkEntry(form)
        entry_ncm.pack(fill="x", padx=12)
        entry_ncm.insert(0, str(dados_xml.get("ncm") or ""))

        preco_sugerido = float(dados_xml.get("preco") or 0.0)
        ctk.CTkLabel(form, text="Preço de Custo:").pack(anchor="w", padx=12, pady=(10, 2))
        entry_custo = ctk.CTkEntry(form)
        entry_custo.pack(fill="x", padx=12)
        entry_custo.insert(0, f"{preco_sugerido:.2f}" if preco_sugerido > 0 else "0,00")
        aplicar_padrao_entrada_numerica(entry_custo, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(form, text="Margem (%):").pack(anchor="w", padx=12, pady=(10, 2))
        entry_margem = ctk.CTkEntry(form)
        entry_margem.pack(fill="x", padx=12)
        entry_margem.insert(0, "30")
        aplicar_padrao_entrada_numerica(entry_margem, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(form, text="Preço de Venda:").pack(anchor="w", padx=12, pady=(10, 2))
        entry_preco = ctk.CTkEntry(form)
        entry_preco.pack(fill="x", padx=12)
        entry_preco.insert(0, f"{preco_sugerido:.2f}" if preco_sugerido > 0 else "0,00")
        aplicar_padrao_entrada_numerica(entry_preco, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(form, text="Estoque Inicial:").pack(anchor="w", padx=12, pady=(10, 2))
        entry_qtd = ctk.CTkEntry(form)
        entry_qtd.pack(fill="x", padx=12)
        entry_qtd.insert(0, str(int(float(dados_xml.get("quantidade") or 0))))
        aplicar_padrao_entrada_numerica(entry_qtd, inteiro=True)

        ctk.CTkLabel(form, text="Validade (AAAA-MM-DD):").pack(anchor="w", padx=12, pady=(10, 2))
        entry_validade = ctk.CTkEntry(form)
        entry_validade.pack(fill="x", padx=12)
        entry_validade.insert(0, str(dados_xml.get("validade") or ""))

        img_state = {"path": imagem_existente or ""}
        drop_frame = ctk.CTkFrame(form, fg_color="#1e1e1e")
        drop_frame.pack(fill="x", padx=12, pady=(14, 8))
        lbl_img = ctk.CTkLabel(
            drop_frame,
            text=(
                f"Imagem encontrada: {os.path.basename(imagem_existente)}"
                if imagem_existente
                else "Arraste uma imagem aqui ou clique para selecionar"
            ),
            text_color="#d9d9d9",
        )
        lbl_img.pack(pady=12)

        def selecionar_imagem_manual(_event=None):
            caminho = filedialog.askopenfilename(filetypes=[("Imagens", "*.jpg *.jpeg *.png")])
            if not caminho:
                return
            img_state["path"] = caminho
            lbl_img.configure(text=f"Imagem selecionada: {os.path.basename(caminho)}", text_color="#2ecc71")

        def processar_drop(event):
            caminho = self._normalizar_path_drop(getattr(event, "data", ""))
            if caminho and os.path.exists(caminho):
                img_state["path"] = caminho
                lbl_img.configure(text=f"Imagem arrastada: {os.path.basename(caminho)}", text_color="#2ecc71")

        drop_frame.bind("<Button-1>", selecionar_imagem_manual)
        lbl_img.bind("<Button-1>", selecionar_imagem_manual)
        dnd_ok = False
        dnd_ok = self._habilitar_tkdnd_widget(drop_frame, processar_drop) or dnd_ok
        dnd_ok = self._habilitar_tkdnd_widget(lbl_img, processar_drop) or dnd_ok
        if not dnd_ok:
            try:
                drop_frame.bind("<<Drop>>", processar_drop)
                lbl_img.bind("<<Drop>>", processar_drop)
                dnd_ok = True
            except Exception:
                dnd_ok = False
        if not dnd_ok:
            lbl_img.configure(text="Clique para selecionar imagem (drag-and-drop indisponível neste ambiente)")

        def salvar_cadastro():
            try:
                ean_salvar = "".join(ch for ch in entry_ean.get().strip() if ch.isdigit())
                if not ean_salvar:
                    raise ValueError("Código de barras inválido.")

                nome = entry_nome.get().strip()
                if not nome:
                    raise ValueError("Informe o nome do produto.")

                preco_custo = parse_numero(entry_custo.get(), "Preço de custo", permitir_vazio=True, default=0.0, minimo=0)
                margem = parse_numero(entry_margem.get(), "Margem", permitir_vazio=True, default=0.0, minimo=0)
                preco_venda = parse_numero(entry_preco.get(), "Preço de venda", permitir_vazio=True, default=0.0, minimo=0)
                qtd = parse_numero(entry_qtd.get(), "Quantidade", permitir_vazio=True, default=0, inteiro=True, minimo=0)
                validade = entry_validade.get().strip()

                ncm = entry_ncm.get().strip()
                imagem_final = self._salvar_imagem_produto_por_ean(img_state.get("path", ""), ean_salvar)

                # Unidade explícita também no cadastro rápido (FASE 1 UN/KG).
                # A origem é o XML quando disponível; default UN.
                unidade_cadastro = str(dados_xml.get("unidade") or "UN").strip().upper()
                if unidade_cadastro not in ("UN", "KG"):
                    unidade_cadastro = "UN"

                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO produtos (
                            codigo_barras, nome, variacao, unidade, ncm, preco_custo, margem_lucro, preco_venda,
                            quantidade_atual, quantidade_minima, validade, imagem_path
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ean_salvar,
                            nome,
                            str(dados_xml.get("unidade") or "UN"),
                            unidade_cadastro,
                            ncm,
                            float(preco_custo),
                            float(margem),
                            float(preco_venda),
                            int(qtd),
                            0,
                            validade,
                            imagem_final,
                        ),
                    )

                try:
                    modal.grab_release()
                except Exception:
                    pass
                modal.destroy()

                produto = self.buscar_produto_por_ean(ean_salvar)
                if produto:
                    self._adicionar_item_produto(produto, 1)
                    self._set_status(f"Produto {nome} cadastrado e adicionado à venda.", "#2ecc71")
                    registrar_log(None, "PDV Cadastro Rápido", "Sucesso", f"Produto EAN {ean_salvar} cadastrado no PDV.")
            except sqlite3.IntegrityError:
                messagebox.showwarning("Cadastro", "Este EAN já está cadastrado no sistema.", parent=self)
            except Exception as e:
                messagebox.showerror("Cadastro", f"Falha ao cadastrar produto: {e}", parent=self)

        footer = ctk.CTkFrame(form, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(8, 12))
        ctk.CTkButton(footer, text="Salvar e Adicionar", fg_color="#27ae60", command=salvar_cadastro).pack(side="left", padx=(0, 8))

        def fechar_modal():
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()

        ctk.CTkButton(footer, text="Cancelar", fg_color="#555555", command=fechar_modal).pack(side="left")
        modal.protocol("WM_DELETE_WINDOW", fechar_modal)

    def _tratar_produto_nao_cadastrado(self, ean):
        self._set_status("Produto não cadastrado.", "#ff6666")
        try:
            messagebox.showinfo("Produto não cadastrado", "Produto não cadastrado.", parent=self)
        except Exception:
            pass
        if hasattr(self, "ent_cod_barras") and self.ent_cod_barras.winfo_exists():
            self.ent_cod_barras.delete(0, "end")
            self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
        self.multiplicador_atual = 1
        self._retornar_foco_pdv()

    def buscar_produtos_por_nome(self, termo):
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao, ncm, variacao, categoria, unidade
                    FROM produtos
                    WHERE UPPER(nome) LIKE UPPER(?)
                    ORDER BY nome ASC
                    LIMIT 30
                    """,
                    (f"%{termo}%",),
                )
                return cursor.fetchall()
        except Exception as e:
            self._set_status(f"Falha ao buscar produto por nome: {e}", "#ff6666")
            registrar_log(None, "PDV", "Falha", f"Erro em buscar_produtos_por_nome: {e}")
            return []

    def buscar_produtos_para_selecao(self, termo):
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao, ncm, variacao, categoria, unidade
                    FROM produtos
                    WHERE codigo_barras LIKE ? OR UPPER(nome) LIKE UPPER(?)
                    ORDER BY nome ASC
                    LIMIT 50
                    """,
                    (f"%{termo}%", f"%{termo}%"),
                )
                return cursor.fetchall()
        except Exception as e:
            self._set_status(f"Falha ao buscar produtos para seleção: {e}", "#ff6666")
            registrar_log(None, "PDV", "Falha", f"Erro em buscar_produtos_para_selecao: {e}")
            return []

    def selecionar_forma_pagamento(self, forma_pgto):
        total = sum(i["total"] for i in self.itens_carrinho)
        restante = round(total - self.valor_pago_acumulado, 2)
        if forma_pgto == "DINHEIRO" and restante <= 0 and total > 0:
            # Total já recebido: valor excedente em dinheiro é troco (regra 5).
            self._set_status(
                f"Total da venda já recebido ({self._formatar_moeda_br(self.valor_pago_acumulado)}). "
                f"Pagamento em dinheiro refere-se ao troco.",
                "#f39c12",
            )
        self.forma_pagamento_selecionada = forma_pgto
        self._set_status(f"Forma de pagamento selecionada: {forma_pgto}", "#4aa3ff")

    def _resolver_forma_pagamento_registro(self):
        """Define a forma de pagamento a ser gravada na venda.

        Pagamentos acumulados em formas distintas são registrados como 'MISTO'
        (conforme alinhado); caso contrário, a forma efetivamente usada.
        """
        formas = {str(f).upper() for f, _ in self.pagamentos_parciais if f}
        if len(formas) > 1:
            return "MISTO"
        if len(formas) == 1:
            return next(iter(formas))
        return self.forma_pagamento_selecionada or "DINHEIRO"

    def _validar_e_obter_valor_pagamento(self, forma_pgto):
        if not self.itens_carrinho:
            self._set_status("Adicione itens antes de processar pagamento.", "#ff6666")
            return None

        valor_total = round(
            sum(float(i.get("total", round(i["quantidade"] * i["preco"], 2)) or 0.0) for i in self.itens_carrinho),
            2,
        )
        valor_pago = self.valor_pago_acumulado

        # Correção de regressão: para formas de pagamento instantâneas e sensíveis
        # a taxa (PIX, DÉBITO, CRÉDITO, VOUCHER), quando não há pagamento parcial/misto
        # nem digitação, assume-se automaticamente o valor total da venda como pago,
        # sem exigir que o operador informe o valor pago ou clique em
        # "ACRESCENTAR PAGAMENTO".
        #
        # A lógica de DINHEIRO, DIVERSOS, troco, pagamento misto e acumulador não é
        # alterada; a correção se restringe ao cálculo de valor_pago neste ponto.
        if forma_pgto in ("PIX", "DEBITO", "CREDITO", "VOUCHER") and not self.pagamentos_parciais:
            valor_pago = round(float(valor_total), 2)

        valor_pago_lido = self._ler_valor_pago_digitado()
        if valor_pago_lido is None:
            self._set_status("Valor pago inválido.", "#ff6666")
            return None
        if valor_pago_lido > 0:
            valor_pago = round(self.valor_pago_acumulado + valor_pago_lido, 2)

        restante = round(valor_total - valor_pago, 2)
        if valor_pago < valor_total - 0.0049:
            self._set_status(
                f"Valor pago ({self._formatar_moeda_br(valor_pago)}) menor que o total da venda "
                f"({self._formatar_moeda_br(valor_total)}). Acrescente o restante de "
                f"{self._formatar_moeda_br(restante)}.",
                "#ff6666",
            )
            return None

        if valor_pago_lido > 0:
            self.pagamentos_parciais.append((forma_pgto, valor_pago_lido))

        self.atualizar_troco_display()
        return valor_pago

    def processar_pagamento(self, forma_pgto):
        # Mantido para compatibilidade com pontos antigos do sistema.
        self.selecionar_forma_pagamento(forma_pgto)

    def finalizar_venda_com_confirmacoes(self):
        forma_pgto = self.forma_pagamento_selecionada or "DINHEIRO"
        valor_pago = self._validar_e_obter_valor_pagamento(forma_pgto)
        if valor_pago is None:
            return

        escolha = self._perguntar_tipo_impressao()

        emitir_nfce = escolha == "NFCE"
        self.finalizar_venda_pdv(
            forma_pgto,
            valor_pago,
            imprimir_cupom=(escolha == "CUPOM"),
            emitir_nfce=emitir_nfce,
        )

    def _perguntar_tipo_impressao(self):
        """Janela modal com 3 opções de documento ao finalizar a venda.

        Atalhos locais (válidos somente com o popup aberto):
        F10 = Cupom | F11 = NFC-e | F12 = Não imprimir.
        Retorna "CUPOM", "NFCE" ou "NAO". Fechar a janela sem escolher equivale
        a "NÃO IMPRIMIR" (mesma semântica do antigo Sim/Não de cupom).
        """
        resultado = {"escolha": "NAO"}

        janela = ctk.CTkToplevel(self)
        janela.title("Documento da Venda")
        janela.geometry("440x310")
        janela.resizable(False, False)
        try:
            janela.transient(self)
            janela.grab_set()
        except Exception:
            pass

        def _escolher(valor):
            resultado["escolha"] = valor
            try:
                janela.grab_release()
            except Exception:
                pass
            janela.destroy()

        def _escolher_cupom(_evento=None):
            _escolher("CUPOM")
            return "break"

        def _tentar_nfce(_evento=None):
            # Emissão fiscal desativada: informa claramente e não emite NFC-e
            # (sem conversão silenciosa para cupom).
            if not self._fiscal_habilitado():
                try:
                    messagebox.showwarning(
                        "Emissão Fiscal",
                        "A emissão fiscal (NFC-e) NÃO está disponível.\n"
                        "'ACBrMonitor (Emissão Fiscal) Ativo' está desativado nas configurações.\n\n"
                        "Escolha outra opção de documento.",
                        parent=janela,
                    )
                except Exception:
                    pass
                return "break"
            _escolher("NFCE")
            return "break"

        def _escolher_nao_imprimir(_evento=None):
            _escolher("NAO")
            return "break"

        ctk.CTkLabel(
            janela,
            text="Como deseja emitir o documento desta venda?",
            font=("Roboto", 15, "bold"),
        ).pack(padx=16, pady=(20, 14))

        ctk.CTkButton(
            janela,
            text="IMPRIMIR CUPOM NÃO FISCAL (F10)",
            fg_color="#27ae60",
            hover_color="#219150",
            height=48,
            font=("Roboto", 13, "bold"),
            command=_escolher_cupom,
        ).pack(fill="x", padx=24, pady=6)

        ctk.CTkButton(
            janela,
            text="IMPRIMIR NOTA FISCAL (NFC-e) (F11)",
            fg_color="#2980b9",
            hover_color="#21688f",
            height=48,
            font=("Roboto", 13, "bold"),
            command=_tentar_nfce,
        ).pack(fill="x", padx=24, pady=6)

        ctk.CTkButton(
            janela,
            text="NÃO IMPRIMIR (F12)",
            fg_color="#7f8c8d",
            hover_color="#666666",
            height=48,
            font=("Roboto", 13, "bold"),
            command=_escolher_nao_imprimir,
        ).pack(fill="x", padx=24, pady=6)

        # Atalhos LOCAIS ao popup: F10=Cupom, F11=NFC-e, F12=Não imprimir.
        # Válidos somente enquanto o popup está aberto (grab_set redireciona as
        # teclas para este Toplevel e "break" impede propagação); fora dele,
        # F10/F11/F12 mantêm o comportamento global existente do PDV.
        janela.bind("<F10>", _escolher_cupom)
        janela.bind("<F11>", _tentar_nfce)
        janela.bind("<F12>", _escolher_nao_imprimir)
        try:
            janela.focus_set()
        except Exception:
            pass

        janela.protocol("WM_DELETE_WINDOW", lambda: _escolher("NAO"))
        janela.wait_window()
        return resultado["escolha"]

    def iniciar_pagamento(self, modo):
        self.processar_pagamento(modo)

    def _ao_pressionar_delete_cancelar_item(self, event=None):
        """Atalho DELETE -> cancelar_item() somente fora de campos de edição."""
        try:
            foco = self.focus_get()
        except Exception:
            foco = None
        # Foco em campo de edição: preserva o DELETE nativo do widget.
        try:
            if foco is not None:
                classe = type(foco).__name__.lower()
                if "entry" in classe or "text" in classe or "spinbox" in classe or "combobox" in classe:
                    return None
        except Exception:
            return None
        # Sem item selecionado: sem efeito, sem interferir no evento.
        if self.item_selecionado_idx is None:
            return None
        try:
            self.cancelar_item()
        except Exception:
            return None
        return "break"

    def cancelar_item(self):
        if self.item_selecionado_idx is None:
            self._set_status("Selecione um item no grid para cancelar.", "#f1c40f")
            return

        if self.item_selecionado_idx < 0 or self.item_selecionado_idx >= len(self.itens_carrinho):
            self._set_status("Item selecionado inválido.", "#ff6666")
            return

        item = self.itens_carrinho.pop(self.item_selecionado_idx)
        self.item_selecionado_idx = None
        self._renderizar_carrinho()
        self.atualizar_total_display()
        self._set_status(f"Item cancelado: {item['nome']}", "#2ecc71")

    def cancelar_venda_atual(self):
        """Mecanismo de escape do operador: abandona somente a venda/operação em andamento
        e retorna ao PDV limpo e pronto, sem registrar venda, sem alterar estoque/caixa/financeiro
        e sem fechar o programa.
        """
        # 1. Fecha qualquer janela modal temporária aberta no PDV
        try:
            for w in list(self.winfo_children()):
                if isinstance(w, (ctk.CTkToplevel,)):
                    try:
                        w.grab_release()
                    except Exception:
                        pass
                    try:
                        w.destroy()
                    except Exception:
                        pass
        except Exception:
            pass

        # 2. Fecha o menu retrátil se estiver aberto
        if getattr(self, "_menu_operacoes_aberto", False):
            try:
                self._alternar_menu_operacoes()
            except Exception:
                pass

        # 3. Limpa o contexto documental temporário (Vale/Orçamento carregado)
        if hasattr(self, "_limpar_contexto_documental"):
            self._limpar_contexto_documental()
        else:
            self._vales_para_quitar = []
            self._operacao_documento_tipo = None
            self._orcamento_para_vender_id = None
            self._operacao_vale_cliente_id = None

        # 4. Limpa itens do carrinho
        self.itens_carrinho = []
        self.item_selecionado_idx = None
        self._renderizar_carrinho()
        self.atualizar_total_display()

        # 5. Limpa pagamentos parciais / acumulados
        if hasattr(self, "limpar_pagamentos_recebidos"):
            self.limpar_pagamentos_recebidos()
        else:
            self.valor_pago_acumulado = 0.0
            self.pagamentos_parciais = []

        if hasattr(self, "ent_valor_pago") and self.ent_valor_pago.winfo_exists():
            self.ent_valor_pago.delete(0, "end")
        if hasattr(self, "lbl_troco_venda"):
            self.lbl_troco_venda.configure(text="R$ 0,00")

        # 6. Limpa campos de entrada de produtos e multiplicadores
        if hasattr(self, "ent_cod_barras") and self.ent_cod_barras.winfo_exists():
            self.ent_cod_barras.delete(0, "end")
            self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
        if hasattr(self, "ent_quantidade") and self.ent_quantidade.winfo_exists():
            self.ent_quantidade.delete(0, "end")
        self.multiplicador_atual = 1
        self.forma_pagamento_selecionada = "DINHEIRO"

        # 7. Restaura status informativo
        self._set_status("Venda cancelada. PDV pronto para nova operação.", "#f1c40f")

        # 8. Devolve o foco automaticamente à barra Código / Nome do Produto com cursor ativo
        self._retornar_foco_pdv()

    def imprimir_comprovante_simplificado(self):
        if not self.itens_carrinho:
            self._set_status("Não há itens para imprimir.", "#ff6666")
            return

        dados_venda = {
            "itens": self.itens_carrinho,
            "total": sum(i["total"] for i in self.itens_carrinho),
            "forma_pagamento": "CONSULTA",
        }
        self.imprimir_cupom(dados_venda)

    def _largura_cupom_chars(self):
        largura_mm = self.config.get("largura_cupom_mm", 80)
        try:
            largura_mm = int(largura_mm)
        except Exception:
            largura_mm = 80
        return 32 if largura_mm <= 58 else 48

    def _split_texto_largura(self, texto, largura):
        texto = str(texto or "").strip()
        if not texto:
            return [""]

        partes = []
        restante = texto
        while len(restante) > largura:
            corte = restante.rfind(" ", 0, largura + 1)
            if corte <= 0:
                corte = largura
            partes.append(restante[:corte].strip())
            restante = restante[corte:].strip()
        if restante:
            partes.append(restante)
        return partes

    def _formatar_quantidade_cupom(self, qtd, unidade="UN"):
        """Quantidade para o cupom: inteiro em UN, decimal + unidade em KG.

        UN mantém exatamente o formato anterior (int(qtd) → "3").
        KG preserva as 3 casas (1,250 KG) — sem truncamento int().
        """
        unidade_txt = str(unidade or "UN").strip().upper() or "UN"
        try:
            valor = float(qtd)
        except (TypeError, ValueError):
            return f"{qtd} {unidade_txt}".strip()
        if unidade_txt == "KG":
            return f"{valor:.3f}".replace(".", ",") + " KG"
        if valor.is_integer():
            return str(int(valor))
        return str(valor)

    def _formatar_linha_item_cupom(self, nome, qtd, total, largura, unidade="UN"):
        col_qtd = 4
        col_total = 10
        texto_qtd = self._formatar_quantidade_cupom(qtd, unidade)
        if len(texto_qtd) > col_qtd:
            col_qtd = len(texto_qtd)
        col_nome = max(8, largura - (col_qtd + col_total + 2))

        linhas_nome = self._split_texto_largura(nome, col_nome)
        primeira_linha = f"{linhas_nome[0]:<{col_nome}} {texto_qtd:>{col_qtd}} {float(total):>{col_total}.2f}"
        linhas = [primeira_linha]
        for trecho in linhas_nome[1:]:
            linhas.append(f"{trecho:<{col_nome}} {'':>{col_qtd}} {'':>{col_total}}")
        return linhas

    def _enviar_raw_impressora_padrao(self, payload):
        if os.name != "nt":
            raise RuntimeError("Impressão ESC/POS direta disponível apenas no Windows.")

        spool = ctypes.WinDLL("winspool.drv")

        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [
                ("pDocName", wintypes.LPWSTR),
                ("pOutputFile", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
            ]

        spool.GetDefaultPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        spool.GetDefaultPrinterW.restype = wintypes.BOOL
        spool.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), wintypes.LPVOID]
        spool.OpenPrinterW.restype = wintypes.BOOL
        spool.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1)]
        spool.StartDocPrinterW.restype = wintypes.DWORD
        spool.StartPagePrinter.argtypes = [wintypes.HANDLE]
        spool.StartPagePrinter.restype = wintypes.BOOL
        spool.WritePrinter.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        spool.WritePrinter.restype = wintypes.BOOL
        spool.EndPagePrinter.argtypes = [wintypes.HANDLE]
        spool.EndPagePrinter.restype = wintypes.BOOL
        spool.EndDocPrinter.argtypes = [wintypes.HANDLE]
        spool.EndDocPrinter.restype = wintypes.BOOL
        spool.ClosePrinter.argtypes = [wintypes.HANDLE]
        spool.ClosePrinter.restype = wintypes.BOOL

        needed = wintypes.DWORD(0)
        spool.GetDefaultPrinterW(None, ctypes.byref(needed))
        if needed.value <= 1:
            raise RuntimeError("Nenhuma impressora padrão configurada no Windows.")

        nome_buffer = ctypes.create_unicode_buffer(needed.value)
        if not spool.GetDefaultPrinterW(nome_buffer, ctypes.byref(needed)):
            raise RuntimeError("Falha ao obter impressora padrão.")

        handle = wintypes.HANDLE()
        if not spool.OpenPrinterW(nome_buffer.value, ctypes.byref(handle), None):
            raise RuntimeError("Falha ao abrir impressora padrão.")

        doc_id = 0
        page_started = False
        try:
            info = DOC_INFO_1("Cupom Nao Fiscal", None, "RAW")
            doc_id = spool.StartDocPrinterW(handle, 1, ctypes.byref(info))
            if doc_id == 0:
                raise RuntimeError("Falha ao iniciar job de impressão RAW.")

            if not spool.StartPagePrinter(handle):
                raise RuntimeError("Falha ao iniciar página de impressão.")
            page_started = True

            bytes_escritos = wintypes.DWORD(0)
            data = ctypes.create_string_buffer(payload)
            if not spool.WritePrinter(handle, data, len(payload), ctypes.byref(bytes_escritos)):
                raise RuntimeError("Falha ao enviar bytes ESC/POS para impressora.")
        finally:
            if page_started:
                spool.EndPagePrinter(handle)
            if doc_id:
                spool.EndDocPrinter(handle)
            spool.ClosePrinter(handle)

    def abrir_gaveta(self, forma_pagamento=None):
        """
        Aciona pulso de abertura de gaveta via ESC/POS na impressora padrão.
        RESTRITIVO: Só abre a gaveta se a forma de pagamento for DINHEIRO/ESPECIE.
        Comando ESC/POS universal: \x1B\x70\x00\x19\x81 (pulso 0, 25ms, 129 cycles).
        """
        # === TRAVA DE SEGURANÇA: SOMENTE DINHEIRO ===
        if forma_pagamento is not None:
            forma_normalizada = str(forma_pagamento).strip().lower()
            if forma_normalizada not in ("dinheiro", "espécie", "especie"):
                registrar_log(
                    None,
                    "PDV Gaveta",
                    "Bloqueado",
                    f"Gaveta NÃO acionada: forma de pagamento '{forma_pagamento}' não é dinheiro.",
                )
                self._set_status("Gaveta bloqueada: pagamento não é em dinheiro.", "#f1c40f")
                return False

        # Comando ESC/POS universal para gaveta (mais compatível)
        comando_gaveta = b"\x1B\x70\x00\x19\x81"
        try:
            self._enviar_raw_impressora_padrao(comando_gaveta)
            registrar_log(None, "PDV Gaveta", "Sucesso", "Comando de abertura de gaveta enviado.")
            return True
        except Exception as e:
            registrar_log(None, "PDV Gaveta", "Falha", f"Erro ao abrir gaveta: {e}")
            return False

    def _forma_para_gaveta_pos_venda(self, forma_pagamento):
        """DECISAO da gaveta (nenhum comando ESC/POS aqui).

        Retorna a forma a ser repassada a `abrir_gaveta` quando a venda tem
        dinheiro, ou None quando a gaveta NAO deve ser acionada.

        - DINHEIRO / ESPÉCIE: aciona.
        - MISTO (multiplo pagamento): aciona SOMENTE se houver dinheiro entre
          os parciais registrados; misto sem dinheiro (ex.: PIX + Debito) nao
          aciona.
        - PIX, DEBITO, CREDITO, VOUCHER, VALE e qualquer outra forma: NAO
          aciona.

        Esta e a unica decisao de gaveta do fluxo pos-venda: ela e usada tanto
        quando o operador imprimi o comprovante quanto quando escolhe
        "NAO IMPRIMIR", de modo que a abertura do gaveteiro independe da
        impressao.
        """
        forma_normalizada = str(forma_pagamento or "").strip().lower()
        if forma_normalizada in ("dinheiro", "espécie", "especie"):
            return forma_pagamento
        if forma_normalizada == "misto":
            formas_recebidas = {
                str(f or "").strip().upper()
                for f, _v in (getattr(self, "pagamentos_parciais", None) or [])
            }
            if formas_recebidas & {"DINHEIRO", "ESPECIE", "ESPÉCIE"}:
                return "DINHEIRO"
        return None

    def _acionar_gaveta_pos_venda(self, forma_pagamento):
        """Abre o gaveteiro de forma INDEPENDENTE da impressao do comprovante.

        Chamada pelo fluxo que imprime o cupom e tambem pelo fluxo em que o
        operador escolheu "NAO IMPRIMIR": venda paga em DINHEIRO sempre aciona
        a gaveta. A decisao fica em `_forma_para_gaveta_pos_venda` e o comando
        ESC/POS permanece em `abrir_gaveta` (nao alterado).

        Retorna (gaveta_acionada, erro).
        """
        forma_para_gaveta = self._forma_para_gaveta_pos_venda(forma_pagamento)
        if forma_para_gaveta is None:
            # Forma sem dinheiro (PIX/DEBITO/CREDITO/VOUCHER/VALE): nada a fazer.
            return False, None
        try:
            abertura_realizada = self.abrir_gaveta(forma_para_gaveta)
        except Exception as e:
            registrar_log(None, "PDV Gaveta", "Falha", f"Erro ao abrir gaveta: {e}")
            return False, e
        if abertura_realizada:
            registrar_log(None, "PDV Gaveta", "Sucesso", "Comando de abertura de gaveta enviado.")
            return True, None
        erro_gaveta = "Abertura bloqueada pela forma de pagamento ou não realizada."
        registrar_log(None, "PDV Gaveta", "Falha", erro_gaveta)
        return False, erro_gaveta

    def _obter_peso_kg(self, produto):
        """Solicita o PESO em KG (decimal, até 3 casas) de produto vendido por KG.

        Fluxo FASE 1: o preço unitário continua sendo o preço cadastrado por KG
        (produtos.preco_venda) e o total da linha é calculado como
        peso × preço/KG. Diferente do fluxo legado (_obter_preco_kg, mantido
        apenas como referência), aqui NÃO se pergunta o valor total: pergunta-se
        o peso, permitindo 1,250 KG. A conversão usa o parser numérico único do
        projeto (validacao_numerica.parse_numero), aceitando vírgula decimal.

        Retorna float (peso em kg) ou None se o operador cancelar.
        """
        nome_produto = produto[2] if isinstance(produto, (tuple, list)) and len(produto) > 2 else "KG"
        try:
            preco_kg = float(produto[3] or 0.0)
        except (TypeError, ValueError):
            preco_kg = 0.0
        dialog = ctk.CTkInputDialog(
            text=(
                f"Informe o PESO (KG) para:\n{nome_produto}\n"
                f"Preço: {self._formatar_moeda_br(preco_kg)}/KG\n"
                "Ex.: 1,250"
            ),
            title="Produto KG – Peso (KG)",
        )
        texto = dialog.get_input()
        if not texto or not str(texto).strip():
            self._set_status("Peso KG não informado. Item não adicionado.", "#ff6666")
            return None
        try:
            peso = parse_numero(texto, "Peso (KG)", permitir_vazio=False, minimo=0, maximo=9999)
        except ValueError:
            self._set_status("Peso KG inválido. Informe o peso em kg (ex: 1,250).", "#ff6666")
            return None
        peso = round(float(peso), 3)
        if peso <= 0:
            self._set_status("Peso KG deve ser maior que zero. Item não adicionado.", "#ff6666")
            return None
        return peso

    def _obter_preco_kg(self, produto):
        """
        Exibe um dialog para que o operador informe o preço total
        da mercadoria pesada externamente (balança não integrada).

        ATENÇÃO: O FRS não possui balança integrada. O operador informa
        diretamente o valor final cobrado (ex.: R$ 8,75).
        Esse valor é o preço efetivo da venda — NÃO é um peso em kg.
        Nenhum peso é calculado ou gravado.

        Retorna float (preço informado) ou None se o operador cancelar.
        """
        nome_produto = produto[2] if len(produto) > 2 else "KG"
        dialog = ctk.CTkInputDialog(
            text=f"Informe o PREÇO TOTAL (R$) para:\n{nome_produto}",
            title="Produto KG – Preço Final",
        )
        valor_str = dialog.get_input()
        if not valor_str or not valor_str.strip():
            self._set_status("Preço KG não informado. Item não adicionado.", "#ff6666")
            return None
        try:
            valor = float(valor_str.strip().replace(",", "."))
        except ValueError:
            self._set_status("Preço KG inválido. Informe apenas números (ex: 8,75).", "#ff6666")
            return None
        if valor != valor or valor in (float("inf"), float("-inf")):
            self._set_status("Preço KG inválido. Informe um número finito (ex: 8,75).", "#ff6666")
            return None
        if valor <= 0:
            self._set_status("Preço KG deve ser maior que zero. Item não adicionado.", "#ff6666")
            return None
        return valor

    def imprimir_cupom_orcamento(self, orcamento_id, segunda_via=False):
        """Compatibilidade: orçamento comercial é PDF A4, nunca cupom térmico."""
        from modulo_orcamento import gerar_pdf_orcamento
        return gerar_pdf_orcamento(orcamento_id, config=self.config, parent=self, notificar=False)

    def abrir_vale_por_cliente(self):
        self._carregar_clientes_orcamento(preservar_selecao=False)
        labels = [label for label, mapped_id in self.clientes_orcamento_map.items() if mapped_id is not None]
        if not labels:
            messagebox.showwarning("Consulta de Vale", "Cadastre um cliente antes de abrir um Vale.", parent=self)
            self._retornar_foco_pdv()
            return

        modal = ctk.CTkToplevel(self)
        modal.title("CONSULTA DE VALES")
        # Geometria ajustada (cirurgico, somente layout): garante que os
        # controles do rodape (FINALIZAR VALE / NAO FINALIZAR / FECHAR)
        # fiquem integralmente visiveis. Nenhuma logica foi alterada.
        modal.geometry("880x700")
        modal.minsize(780, 560)
        modal.transient(self)
        modal.grab_set()

        # Topo: Seleção do Cliente
        frame_topo = ctk.CTkFrame(modal, fg_color="transparent")
        frame_topo.pack(fill="x", padx=16, pady=(14, 6))

        ctk.CTkLabel(frame_topo, text="CLIENTE:", font=("Roboto", 12, "bold")).pack(side="left", padx=(0, 8))
        combo = ctk.CTkOptionMenu(frame_topo, values=labels, width=320)
        combo.set(labels[0])
        combo.pack(side="left", padx=4)

        # Container Central: Lista de Vales do Cliente + Detalhes dos Itens
        frame_corpo = ctk.CTkFrame(modal, fg_color="transparent")
        frame_corpo.pack(fill="both", expand=True, padx=16, pady=4)

        lbl_lista = ctk.CTkLabel(frame_corpo, text="VALES PENDENTES:", font=("Roboto", 11, "bold"), text_color="gray")
        lbl_lista.pack(anchor="w", pady=(0, 2))

        scroll_vales = ctk.CTkScrollableFrame(frame_corpo, height=120, fg_color="#181818")
        scroll_vales.pack(fill="x", pady=(0, 8))

        lbl_detalhe = ctk.CTkLabel(frame_corpo, text="ITENS DO VALE SELECIONADO:", font=("Roboto", 11, "bold"), text_color="gray")
        lbl_detalhe.pack(anchor="w", pady=(0, 2))

        scroll_itens = ctk.CTkScrollableFrame(frame_corpo, height=190, fg_color="#181818")
        scroll_itens.pack(fill="both", expand=True, pady=(0, 4))

        lbl_total_vale = ctk.CTkLabel(frame_corpo, text="TOTAL: R$ 0,00", font=("Roboto", 14, "bold"), text_color="#2ecc71")
        lbl_total_vale.pack(anchor="e", pady=(2, 6))

        # Estado da seleção
        estado = {
            "vale_selecionado": None,
            "itens_selecionados": [],
            "cliente_id": None,
            "vales_ids": [],
            "finalizar": False,
        }

        # Rodapé de Ações
        botoes = ctk.CTkFrame(modal, fg_color="transparent")
        botoes.pack(fill="x", padx=16, pady=(4, 14))

        btn_finalizar = ctk.CTkButton(
            botoes,
            text="FINALIZAR VALE",
            fg_color="#27ae60",
            hover_color="#219150",
            width=160,
            height=38,
            font=("Roboto", 12, "bold"),
            state="disabled",
        )
        btn_finalizar.pack(side="left", padx=6)

        def _fechar_sem_finalizar():
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()
            self._retornar_foco_pdv()

        btn_fechar = ctk.CTkButton(
            botoes,
            text="NÃO FINALIZAR / FECHAR",
            fg_color="#555555",
            hover_color="#444444",
            width=200,
            height=38,
            font=("Roboto", 12, "bold"),
            command=_fechar_sem_finalizar,
        )
        btn_fechar.pack(side="right", padx=6)

        def _exibir_itens_vale(vale_id, numero, total_vale, data_criacao):
            estado["vale_selecionado"] = vale_id
            estado["vales_ids"] = [int(vale_id)]
            for w in scroll_itens.winfo_children():
                w.destroy()

            # Cabeçalho da tabela de itens
            cab = ctk.CTkFrame(scroll_itens, fg_color="#222222")
            cab.pack(fill="x", pady=1)
            ctk.CTkLabel(cab, text="PRODUTO / DESCRIÇÃO", font=("Roboto", 10, "bold"), width=240, anchor="w").pack(side="left", padx=4)
            ctk.CTkLabel(cab, text="QTD", font=("Roboto", 10, "bold"), width=60).pack(side="left", padx=4)
            ctk.CTkLabel(cab, text="UN", font=("Roboto", 10, "bold"), width=40).pack(side="left", padx=4)
            ctk.CTkLabel(cab, text="PREÇO", font=("Roboto", 10, "bold"), width=80).pack(side="left", padx=4)
            ctk.CTkLabel(cab, text="SUBTOTAL", font=("Roboto", 10, "bold"), width=90).pack(side="left", padx=4)

            itens = []
            with get_db_connection() as conn:
                linhas = conn.execute(
                    """
                    SELECT produto_id, codigo_barras, descricao_produto, ncm,
                           quantidade, unidade, preco_unitario, subtotal
                    FROM vale_itens WHERE vale_id = ? ORDER BY id
                    """,
                    (int(vale_id),),
                ).fetchall()
                for linha in linhas:
                    item_dict = {
                        "id": linha[0], "barcode": linha[1] or "", "nome": linha[2],
                        "ncm": linha[3] or "", "quantidade": float(linha[4] or 0.0),
                        "unidade": str(linha[5] or "UN").upper(),
                        "preco": float(linha[6] or 0.0), "total": float(linha[7] or 0.0),
                        "origem": "VALE", "vale_id": int(vale_id), "numero_vale": numero,
                    }
                    itens.append(item_dict)

                    linha_f = ctk.CTkFrame(scroll_itens, fg_color="transparent")
                    linha_f.pack(fill="x", pady=1)
                    ctk.CTkLabel(linha_f, text=str(linha[2]), width=240, anchor="w").pack(side="left", padx=4)
                    ctk.CTkLabel(linha_f, text=f"{float(linha[4] or 0.0):g}", width=60).pack(side="left", padx=4)
                    ctk.CTkLabel(linha_f, text=str(linha[5] or "UN"), width=40).pack(side="left", padx=4)
                    ctk.CTkLabel(linha_f, text=self._formatar_moeda_br(float(linha[6] or 0.0)), width=80).pack(side="left", padx=4)
                    ctk.CTkLabel(linha_f, text=self._formatar_moeda_br(float(linha[7] or 0.0)), width=90).pack(side="left", padx=4)

            estado["itens_selecionados"] = itens
            lbl_total_vale.configure(text=f"TOTAL DO VALE #{numero}: {self._formatar_moeda_br(float(total_vale or 0.0))}")
            if itens:
                btn_finalizar.configure(state="normal")
            else:
                btn_finalizar.configure(state="disabled")

        def _carregar_vales_cliente(_escolha=None):
            estado["vale_selecionado"] = None
            estado["itens_selecionados"] = []
            estado["vales_ids"] = []
            btn_finalizar.configure(state="disabled")
            lbl_total_vale.configure(text="TOTAL: R$ 0,00")
            for w in scroll_vales.winfo_children():
                w.destroy()
            for w in scroll_itens.winfo_children():
                w.destroy()

            cliente_id = self.clientes_orcamento_map.get(combo.get())
            if not cliente_id:
                return
            estado["cliente_id"] = int(cliente_id)

            with get_db_connection() as conn:
                vales = conn.execute(
                    "SELECT id, numero, data_criacao, total FROM vales WHERE cliente_id = ? AND status = 'PENDENTE' ORDER BY numero DESC",
                    (int(cliente_id),),
                ).fetchall()

            if not vales:
                ctk.CTkLabel(scroll_vales, text="Nenhum vale pendente para este cliente.", text_color="#f39c12").pack(pady=10)
                return

            for idx, (v_id, v_num, v_data, v_tot) in enumerate(vales):
                data_str = str(v_data or "")[:16]
                btn_v = ctk.CTkButton(
                    scroll_vales,
                    text=f"Vale #{v_num} — {data_str} — Total: {self._formatar_moeda_br(float(v_tot or 0.0))}",
                    fg_color="#2c3e50",
                    hover_color="#1a252f",
                    anchor="w",
                    command=lambda vid=v_id, num=v_num, tot=v_tot, dt=v_data: _exibir_itens_vale(vid, num, tot, dt),
                )
                btn_v.pack(fill="x", padx=4, pady=2)
                if idx == 0:
                    _exibir_itens_vale(v_id, v_num, v_tot, v_data)

        combo.configure(command=_carregar_vales_cliente)

        def _acionar_finalizar():
            if not estado["itens_selecionados"] or not estado["cliente_id"]:
                return
            estado["finalizar"] = True
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()

        btn_finalizar.configure(command=_acionar_finalizar)

        # Carrega lista inicial do primeiro cliente selecionado
        _carregar_vales_cliente()

        modal.protocol("WM_DELETE_WINDOW", _fechar_sem_finalizar)
        modal.wait_window()

        if estado["finalizar"]:
            if self._preparar_carrinho_documental(
                estado["itens_selecionados"], estado["cliente_id"], "VALE", estado["vales_ids"]
            ):
                self._set_status(
                    f"Vale carregado no PDV. Escolha a forma de pagamento e finalize a venda.",
                    "#2ecc71",
                )

    def imprimir_cupom_vale(self, vale_id):
        """Imprime o documento do Vale usando o mesmo transporte térmico existente."""
        with get_db_connection() as conn:
            cabecalho = conn.execute(
                """
                SELECT v.id, v.numero, v.data_criacao, v.status, v.total, c.nome
                FROM vales v JOIN clientes c ON c.id = v.cliente_id
                WHERE v.id = ?
                """,
                (int(vale_id),),
            ).fetchone()
            if not cabecalho:
                raise ValueError("Vale não encontrado.")
            itens = conn.execute(
                """
                SELECT descricao_produto, quantidade, unidade, preco_unitario, subtotal
                FROM vale_itens WHERE vale_id = ? ORDER BY id
                """,
                (int(vale_id),),
            ).fetchall()
        if not itens:
            raise ValueError("Vale sem itens.")
        dados = {
            "documento_tipo": "VALE",
            "numero_documento": cabecalho[1],
            "data_documento": str(cabecalho[2] or ""),
            "cliente_nome": cabecalho[5] or "CLIENTE",
            "status_documento": cabecalho[3],
            "itens": [
                {
                    "nome": item[0], "quantidade": float(item[1] or 0.0),
                    "unidade": str(item[2] or "UN").upper(),
                    "preco_unitario": float(item[3] or 0.0), "subtotal": float(item[4] or 0.0),
                    "total": float(item[4] or 0.0),
                } for item in itens
            ],
            "total": float(cabecalho[4] or 0.0),
            "forma_pagamento": "PENDENTE",
        }
        self.imprimir_cupom(dados)

    def imprimir_cupom(self, dados_venda):
        """Imprime cupom não fiscal em impressora térmica (58mm/80mm) via ESC/POS."""
        itens = list(dados_venda.get("itens") or [])
        total = float(dados_venda.get("total") or 0.0)
        forma_pgto = str(dados_venda.get("forma_pagamento") or "N/A")
        tipo_documento = str(dados_venda.get("documento_tipo") or "").strip().upper()
        eh_orcamento = tipo_documento == "ORCAMENTO"
        eh_vale = tipo_documento == "VALE"
        eh_documento = eh_orcamento or eh_vale
        numero_documento = dados_venda.get("numero_documento")
        data_documento = str(dados_venda.get("data_documento") or "")
        cliente_nome = str(dados_venda.get("cliente_nome") or "").strip()
        segunda_via = bool(dados_venda.get("segunda_via"))
        if not itens:
            raise ValueError("Cupom não impresso: venda sem itens.")

        largura = self._largura_cupom_chars()
        separador = "-" * largura
        # Identidade do estabelecimento: Nome Fantasia (config) — sem hardcode
        # de cliente e sem "MERCADO FRS" como identidade impressa.
        nome_fantasia = str(
            self.config.get("nome_fantasia")
            or self.config.get("nome_estabelecimento")
            or self.config.get("razao_social")
            or ""
        ).strip().upper()
        cnpj_cupom = str(self.config.get("cnpj") or "").strip()
        logradouro = str(self.config.get("logradouro") or "").strip()
        numero = str(self.config.get("numero") or "").strip()
        complemento = str(self.config.get("complemento") or "").strip()
        bairro = str(self.config.get("bairro") or "").strip()
        cidade = str(self.config.get("cidade") or "").strip()
        uf = str(self.config.get("uf") or "").strip().upper()
        cep = str(self.config.get("cep") or "").strip()
        rodape = str(self.config.get("mensagem_rodape_cupom", "Obrigado pela preferência!")).strip()

        linhas = []
        if nome_fantasia:
            linhas.append(nome_fantasia.center(largura))
        if cnpj_cupom:
            linhas.append(f"CNPJ: {cnpj_cupom}".center(largura))
        linha_log = ", ".join(p for p in (f"{logradouro}, {numero}" if logradouro and numero else (logradouro or numero),) if p)
        if linha_log:
            linhas.append(linha_log.center(largura))
        if complemento:
            linhas.append(complemento.center(largura))
        if bairro:
            linhas.append(bairro.center(largura))
        linha_cidade_uf = " / ".join(p for p in (f"{cidade}/{uf}" if cidade and uf else (cidade or uf),) if p)
        if linha_cidade_uf:
            linhas.append(linha_cidade_uf.center(largura))
        if cep:
            linhas.append(f"CEP: {cep}".center(largura))
        if nome_fantasia or cnpj_cupom or linha_log or complemento or bairro or linha_cidade_uf or cep:
            linhas.append(separador)
        if eh_documento:
            status_documento = str(dados_venda.get("status_documento") or "PENDENTE").upper()
            titulo_documento = "ORCAMENTO - NAO FISCAL" if eh_orcamento else f"VALE - {status_documento}"
            rotulo_documento = "ORCAMENTO" if eh_orcamento else "VALE"
            linhas.extend(
                [
                    titulo_documento.center(largura),
                    f"{rotulo_documento}: {numero_documento}".center(largura),
                    (data_documento or datetime.now().strftime("%d/%m/%Y %H:%M:%S")).center(largura),
                    f"ESTABELECIMENTO: {nome_fantasia or 'NAO CONFIGURADO'}",
                    f"CLIENTE: {cliente_nome or 'SEM CLIENTE CADASTRADO'}",
                    *(([f"STATUS: {str(dados_venda.get('status_documento') or 'PENDENTE').upper()}"] if eh_vale else [])),
                    separador,
                    "ITEM".ljust(largura - 15) + "QTD".rjust(4) + "TOTAL".rjust(11),
                    separador,
                ]
            )
            if segunda_via:
                linhas.append("SEGUNDA VIA".center(largura))
                linhas.append(separador)
        else:
            linhas.extend(
                [
                    "CUPOM NAO FISCAL".center(largura),
                    datetime.now().strftime("%d/%m/%Y %H:%M:%S").center(largura),
                    separador,
                    "ITEM".ljust(largura - 15) + "QTD".rjust(4) + "TOTAL".rjust(11),
                    separador,
                ]
            )

        for item in itens:
            nome = item.get("nome", "ITEM")
            qtd = item.get("quantidade", 1)
            total_item = item.get("total", 0.0)
            for linha in self._formatar_linha_item_cupom(nome, qtd, total_item, largura, item.get("unidade")):
                linhas.append(linha)
            if eh_documento:
                unitario = self._formatar_moeda_br(item.get("preco_unitario", item.get("preco", 0.0)))
                subtotal = self._formatar_moeda_br(item.get("subtotal", total_item))
                linhas.append(f"  VALOR UNITARIO: {unitario}")
                linhas.append(f"  SUBTOTAL: {subtotal}")

        if eh_documento:
            rotulo_total = "TOTAL ORCAMENTO" if eh_orcamento else "TOTAL VALE"
            linhas.extend([separador, f"{rotulo_total}: {self._formatar_moeda_br(total)}"])
        else:
            linhas.extend(
                [
                    separador,
                    f"FORMA DE PAGAMENTO: {forma_pgto}",
                    f"TOTAL: {self._formatar_moeda_br(total)}",
                ]
            )

        # PARTE C (BLOCO 2) — recebido/troco: usa os valores reais do fluxo.
        # Não se aplica a orçamento: cupom não fiscal de orçamento não tem
        # pagamento, recebimento ou troco associado.
        try:
            if not eh_documento:
                parciais = list(dados_venda.get("pagamentos") or dados_venda.get("pagamentos_parciais") or [])
                if str(forma_pgto or "").strip().upper() == "MISTO" and parciais:
                    for forma_p, valor_p in parciais:
                        try:
                            linhas.append(f"  {str(forma_p).upper()}: {self._formatar_moeda_br(float(valor_p))}")
                        except Exception:
                            continue
                recebido = dados_venda.get("valor_recebido", None)
                if recebido is None:
                    recebido = dados_venda.get("valor_pago", None)
                if recebido is not None:
                    recebido_f = round(float(recebido), 2)
                    troco_f = round(recebido_f - round(total, 2), 2)
                    linhas.append(f"VALOR RECEBIDO: {self._formatar_moeda_br(recebido_f)}")
                    if troco_f > 0.0049:
                        linhas.append(f"TROCO: {self._formatar_moeda_br(troco_f)}")
        except Exception:
            pass

        linhas.append(separador)

        corpo_principal = "\n".join(linhas).encode("cp850", errors="replace")
        # Cabeçalho em destaque: centralizado, negrito e tamanho ampliado.
        # Usa o Nome Fantasia configurado; sem fallback impresso "MERCADO FRS".
        nome_destaque = nome_fantasia or (
            "ORCAMENTO - NAO FISCAL" if eh_orcamento else "VALE - PENDENTE" if eh_vale else "CUPOM NAO FISCAL"
        )
        cabecalho_destaque = (
            b"\x1ba\x01" +          # ESC a 1 -> alinhamento central
            b"\x1d!\x11" +          # GS ! 0x11 -> largura/altura dobradas
            b"\x1bE\x01" +          # ESC E 1 -> negrito on
            f"{nome_destaque}\n".encode("cp850", errors="replace") +
            b"\x1bE\x00" +          # ESC E 0 -> negrito off
            b"\x1d!\x00" +          # GS ! 0x00 -> tamanho normal
            b"\x1ba\x00"            # ESC a 0 -> alinhamento à esquerda
        )

        rodape_principal = f"{rodape.center(largura)}\n".encode("cp850", errors="replace")
        # Rodapé discreto: centralizado com fonte B (menor).
        assinatura_rodape = (
            b"\x1ba\x01" +
            b"\x1bM\x01" +          # ESC M 1 -> fonte B (menor)
            b"Desenvolvido por FRS Solutions\n" +
            b"\x1bM\x00" +
            b"\x1ba\x00"
        )

        comando_inicial = b"\x1b@\x1ba\x00"
        comando_final = b"\n\n\n\x1dV\x00"
        payload = comando_inicial + cabecalho_destaque + corpo_principal + b"\n" + rodape_principal + assinatura_rodape + comando_final

        self._enviar_raw_impressora_padrao(payload)
        registrar_log(None, "PDV Impressão", "Sucesso", "Cupom não fiscal enviado para impressora térmica.")

    def _executar_automacao_pos_venda(self, dados_cupom):
        """Dispara a impressão do cupom pós-venda e, em seguida, a abertura da
        gaveta quando a venda tem DINHEIRO.

        ROTATORES SEPARADOS DE PROPOSITO: a impressao do cupom e a abertura do
        gaveteiro sao etapas independentes — a decisao/acionamento da gaveta
        vive em `_acionar_gaveta_pos_venda`, que e reutilizada pelo fluxo
        "NAO IMPRIMIR" em `finalizar_venda_pdv`. Assim, uma falha (ou a
        ausencia) de impressao jamais impede a gaveta de abrir, e vice-versa.
        """
        erro_impressao = None

        try:
            self.imprimir_cupom(dados_cupom)
        except Exception as e:
            registrar_log(None, "PDV Impressão", "Falha", f"Erro impressão cupom: {e}")
            erro_impressao = e

        gaveta_acionada, erro_gaveta = self._acionar_gaveta_pos_venda(
            dados_cupom.get("forma_pagamento")
        )
        gaveta_permitida = erro_gaveta is None and gaveta_acionada

        if erro_impressao is None and erro_gaveta is None:
            self._set_status(
                "Cupom não fiscal impresso e gaveta acionada."
                if gaveta_acionada
                else "Cupom não fiscal impresso.",
                "#2ecc71",
            )
            return

        if erro_impressao is not None and erro_gaveta is not None:
            self._set_status(f"Falha na automação pós-venda: impressão e gaveta. ({erro_impressao})", "#ff6666")
            return

        if erro_impressao is not None:
            if gaveta_permitida:
                self._set_status(f"Cupom não impresso: {erro_impressao}. Gaveta acionada.", "#ff6666")
            else:
                self._set_status(f"Cupom não impresso: {erro_impressao}.", "#ff6666")
            return

        self._set_status(f"Cupom impresso, mas falha ao abrir gaveta: {erro_gaveta}", "#ff6666")

    def exportar_venda_fiscal(self, dados_venda):
        try:
            venda_id = dados_venda["id"]
            caminho_entrada = self.config.get("pasta_entrada_fiscal")
            if not caminho_entrada:
                return False

            nome_arquivo = f"venda_{venda_id}.json"
            caminho_final = os.path.join(caminho_entrada, nome_arquivo)

            with open(caminho_final, "w", encoding="utf-8") as f:
                json.dump(dados_venda, f, indent=4, ensure_ascii=False)

            if hasattr(self, "lbl_status_fiscal") and self.lbl_status_fiscal.winfo_exists():
                self.lbl_status_fiscal.configure(text=f"Fiscal: Aguardando retorno venda #{venda_id}...", text_color="orange")
            self.fiscal.monitorar_retorno(venda_id, self._atualizar_status_fiscal_ui)
            return True
        except Exception as e:
            self._set_status(f"Erro ao exportar JSON fiscal: {e}", "#ff6666")
            return False

    def _gerar_comando_nfce_acbr(self, venda_id, forma_pgto, itens):
        """
        Monta comando completo de NFC-e para o ACBrMonitor com layout por item.
        """
        return self.fiscal_manager.gerar_comando_nfce(venda_id, forma_pgto, itens)

    def _enviar_comando_nfce(self, venda_id, forma_pgto, itens):
        if not hasattr(self, "fiscal_manager") or self.fiscal_manager is None:
            return False

        comando = self._gerar_comando_nfce_acbr(venda_id, forma_pgto, itens)

        try:
            acbr_ativo = self.fiscal_manager.iniciar_acbr()
            if not acbr_ativo:
                registrar_log(
                    None,
                    "PDV Fiscal",
                    "Aviso",
                    "ACBrMonitor nao identificado em execucao. Comando NFC-e sera mantido para tentativa posterior.",
                )

            resposta = self.fiscal_manager.enviar_comando(comando)
            analise = self.fiscal_manager.interpretar_retorno(resposta)
            if not analise.get("sucesso"):
                mensagem_erro = analise.get("mensagem") or "Falha desconhecida no ACBrMonitor."
                self._set_status(f"Erro na emissão: {mensagem_erro}", "#ff6666")
                try:
                    messagebox.showerror("Erro Fiscal", f"Erro na emissão: {mensagem_erro}", parent=self)
                except Exception:
                    pass
                registrar_log(None, "PDV Fiscal", "Falha", f"Erro na emissão: {mensagem_erro}")
                return False

            registrar_log(None, "PDV Fiscal", "Sucesso", f"Comando NFC-e enviado. Retorno: {resposta[:200]}")
            return True
        except Exception as e:
            self._set_status(f"Erro na emissão: {e}", "#ff6666")
            try:
                messagebox.showerror("Erro Fiscal", f"Erro na emissão: {e}", parent=self)
            except Exception:
                pass
            registrar_log(None, "PDV Fiscal", "Falha", f"Erro ao enviar comando NFC-e: {e}")
            return False

    def _fiscal_habilitado(self):
        try:
            cfg = carregar_configuracoes() or {}
            self.config = cfg
            return bool(cfg.get("fiscal_ativo", False))
        except Exception:
            return False

    def _atualizar_status_fiscal_ui(self, status, mensagem):
        def update():
            if not self.winfo_exists():
                return
            if not hasattr(self, "lbl_status_fiscal") or not self.lbl_status_fiscal.winfo_exists():
                return
            if status == "SUCESSO":
                self.lbl_status_fiscal.configure(text="Fiscal: Venda Autorizada!", text_color="green")
            elif status == "TIMEOUT":
                self.lbl_status_fiscal.configure(text="Fiscal: Integrador Offline (Aguardando...)", text_color="yellow")
            else:
                self.lbl_status_fiscal.configure(text=f"Fiscal Erro: {mensagem}", text_color="red")
                self._set_status(f"Erro fiscal: {mensagem}", "#ff6666")

        self._safe_after(0, update)

    def finalizar_venda_pdv(
        self,
        forma_pgto,
        valor_pago=None,
        imprimir_cupom=False,
        emitir_nfce=None,
        origem_venda="LOJA_FISICA",
        status_pedido="APROVADO",
        status_pagamento="PAGO",
    ):
        if not self.itens_carrinho:
            return

        # Forma efetivamente usada na venda (agrega pagamentos parciais).
        # Compatível com stubs de teste que não possuem os novos atributos.
        if hasattr(self, "_resolver_forma_pagamento_registro"):
            forma_pgto = self._resolver_forma_pagamento_registro()

        valor_bruto = round(
            sum(float(i.get("total", round(i["quantidade"] * i["preco"], 2)) or 0.0) for i in self.itens_carrinho),
            2,
        )
        valor_impostos = 0.0
        total_icms = 0.0
        total_pis = 0.0
        total_cofins = 0.0
        total_ibs = 0.0
        total_cbs = 0.0
        regime_venda = "ATUAL"
        for item in self.itens_carrinho:
            aliquotas_item = {
                "aliquota_icms": item.get("aliquota_icms", 0.0),
                "aliquota_pis": item.get("aliquota_pis", 0.0),
                "aliquota_cofins": item.get("aliquota_cofins", 0.0),
                "aliquota_ibs": item.get("aliquota_ibs", 0.0),
                "aliquota_cbs": item.get("aliquota_cbs", 0.0),
            }
            resultado_impostos = self.calculadora_tributaria.calcular_impostos(
                item.get("total", 0.0),
                datetime.now().date(),
                ncm=item.get("ncm", ""),
                aliquotas_produto=aliquotas_item,
            )
            item["aliquota_imposto"] = resultado_impostos["aliquota"]
            item["valor_imposto"] = resultado_impostos["valor_imposto"]
            item["valor_liquido"] = resultado_impostos["valor_liquido"]
            item["regime_tributario"] = resultado_impostos.get("regime", "ATUAL")
            item["aliquotas"] = resultado_impostos.get("aliquotas", {})
            item["valores_impostos"] = resultado_impostos.get("valores", {})

            valores_item = item["valores_impostos"]
            total_icms += float(valores_item.get("icms", 0.0) or 0.0)
            total_pis += float(valores_item.get("pis", 0.0) or 0.0)
            total_cofins += float(valores_item.get("cofins", 0.0) or 0.0)
            total_ibs += float(valores_item.get("ibs", 0.0) or 0.0)
            total_cbs += float(valores_item.get("cbs", 0.0) or 0.0)
            if item["regime_tributario"] == "IVA_DUAL":
                regime_venda = "IVA_DUAL"
            valor_impostos += resultado_impostos["valor_imposto"]

        valor_impostos = round(valor_impostos, 2)
        total_icms = round(total_icms, 2)
        total_pis = round(total_pis, 2)
        total_cofins = round(total_cofins, 2)
        total_ibs = round(total_ibs, 2)
        total_cbs = round(total_cbs, 2)
        taxas = modulo_financeiro.obter_taxas()
        valor_liquido = round(valor_bruto - valor_impostos, 2)
        taxa_aplicada = 0.0

        if forma_pgto in ["DEBITO", "CREDITO"]:
            taxa_aplicada = taxas.get(forma_pgto, 0.0)
            valor_liquido = round(valor_liquido - (valor_bruto * (taxa_aplicada / 100)), 2)
        if valor_liquido < 0:
            valor_liquido = 0.0

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cols_v = {r[1] for r in cursor.execute("PRAGMA table_info(vendas)").fetchall()}
                cols_d = {r[1] for r in cursor.execute("PRAGMA table_info(vendas_dia)").fetchall()}
                cols_fin = {r[1] for r in cursor.execute("PRAGMA table_info(financeiro)").fetchall()}
                cx_id = getattr(self, "caixa_id", None)
                vales_para_quitar = list(dict.fromkeys(
                    int(vale_id) for vale_id in (getattr(self, "_vales_para_quitar", None) or [])
                ))
                if vales_para_quitar:
                    if not cx_id:
                        raise RuntimeError("Não há caixa aberto para quitar os vales carregados.")
                    caixa_vale = cursor.execute(
                        "SELECT status, data_abertura FROM caixa_operacao WHERE id = ?",
                        (cx_id,),
                    ).fetchone()
                    if not caixa_vale or str(caixa_vale[0] or "").upper() != "ABERTO":
                        raise RuntimeError("O caixa do Vale não está aberto para a operação atual.")
                if "caixa_operacao_id" in cols_v:
                    cursor.execute(
                        """
                        INSERT INTO vendas (
                            valor_total, valor_impostos_retidos, valor_liquido,
                            origem, status_pedido, status_pagamento, forma_pagamento,
                            valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs,
                            caixa_operacao_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            valor_bruto,
                            valor_impostos,
                            valor_liquido,
                            str(origem_venda or "LOJA_FISICA").upper(),
                            str(status_pedido or "APROVADO").upper(),
                            str(status_pagamento or "PAGO").upper(),
                            forma_pgto,
                            total_icms,
                            total_pis,
                            total_cofins,
                            total_ibs,
                            total_cbs,
                            cx_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO vendas (
                            valor_total, valor_impostos_retidos, valor_liquido,
                            origem, status_pedido, status_pagamento, forma_pagamento,
                            valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            valor_bruto,
                            valor_impostos,
                            valor_liquido,
                            str(origem_venda or "LOJA_FISICA").upper(),
                            str(status_pedido or "APROVADO").upper(),
                            str(status_pagamento or "PAGO").upper(),
                            forma_pgto,
                            total_icms,
                            total_pis,
                            total_cofins,
                            total_ibs,
                            total_cbs,
                        ),
                    )
                venda_id = cursor.lastrowid
                if "caixa_operacao_id" in cols_d:
                    cursor.execute(
                        """
                        INSERT INTO vendas_dia (
                            valor_total, valor_impostos_retidos, valor_liquido,
                            origem, status_pedido, status_pagamento, forma_pagamento,
                            valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs,
                            caixa_operacao_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            valor_bruto,
                            valor_impostos,
                            valor_liquido,
                            str(origem_venda or "LOJA_FISICA").upper(),
                            str(status_pedido or "APROVADO").upper(),
                            str(status_pagamento or "PAGO").upper(),
                            forma_pgto,
                            total_icms,
                            total_pis,
                            total_cofins,
                            total_ibs,
                            total_cbs,
                            cx_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO vendas_dia (
                            valor_total, valor_impostos_retidos, valor_liquido,
                            origem, status_pedido, status_pagamento, forma_pagamento,
                            valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            valor_bruto,
                            valor_impostos,
                            valor_liquido,
                            str(origem_venda or "LOJA_FISICA").upper(),
                            str(status_pedido or "APROVADO").upper(),
                            str(status_pagamento or "PAGO").upper(),
                            forma_pgto,
                            total_icms,
                            total_pis,
                            total_cofins,
                            total_ibs,
                            total_cbs,
                        ),
                    )

                desc = f"Venda PDV #{venda_id} ({forma_pgto}) [{str(origem_venda or 'LOJA_FISICA').upper()}]"
                if "caixa_operacao_id" in cols_fin:
                    cursor.execute(
                        """
                        INSERT INTO financeiro (
                            valor, tipo, valor_bruto, valor_impostos_retidos,
                            taxa_aplicada, descricao, caixa_operacao_id
                        )
                        VALUES (?, 'Entrada', ?, ?, ?, ?, ?)
                        """,
                        (valor_liquido, valor_bruto, valor_impostos, taxa_aplicada, desc, cx_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO financeiro (
                            valor, tipo, valor_bruto, valor_impostos_retidos,
                            taxa_aplicada, descricao
                        )
                        VALUES (?, 'Entrada', ?, ?, ?, ?)
                        """,
                        (valor_liquido, valor_bruto, valor_impostos, taxa_aplicada, desc),
                    )
                cursor.execute(
                    """
                    UPDATE financeiro
                    SET valor_icms = ?, valor_pis = ?, valor_cofins = ?, valor_ibs = ?, valor_cbs = ?
                    WHERE id = last_insert_rowid()
                    """,
                    (total_icms, total_pis, total_cofins, total_ibs, total_cbs),
                )

                for item in self.itens_carrinho:
                    unidade_item = str(item.get("unidade") or "UN").strip().upper() or "UN"
                    try:
                        produto_id = int(item.get("id"))
                        if unidade_item == "KG":
                            # KG: quantidade é PESO decimal (até 3 casas) — nunca int().
                            quantidade_vendida = round(float(item.get("quantidade", 0) or 0), 3)
                        else:
                            quantidade_vendida = int(item.get("quantidade", 0))
                    except (TypeError, ValueError):
                        continue

                    if quantidade_vendida <= 0:
                        continue

                    # Baixa FEFO: 1 linha em itens_venda por lote consumido,
                    # com rateio proporcional de subtotal/impostos (a última
                    # parcela fecha o total do item). Sem lotes: caminho legado.
                    lotes_dist = aplicar_baixa_fefo(cursor, produto_id, quantidade_vendida)
                    if lotes_dist:
                        subtotal_item = float(item.get("total", 0.0))
                        valores_item = {
                            k: float(item.get("valores_impostos", {}).get(k, 0.0) or 0.0)
                            for k in ("icms", "pis", "cofins", "ibs", "cbs")
                        }
                        restante_subtotal = subtotal_item
                        restantes_valores = dict(valores_item)
                        for idx, (lote_id, qtd_parcela) in enumerate(lotes_dist):
                            if idx == len(lotes_dist) - 1:
                                sub_parcela = round(restante_subtotal, 2)
                                val_parcela = {k: round(v, 2) for k, v in restantes_valores.items()}
                            else:
                                fator = qtd_parcela / quantidade_vendida
                                sub_parcela = round(subtotal_item * fator, 2)
                                val_parcela = {k: round(v * fator, 2) for k, v in valores_item.items()}
                                restante_subtotal -= sub_parcela
                                for k in restantes_valores:
                                    restantes_valores[k] -= val_parcela[k]
                            cursor.execute(
                                """
                                INSERT INTO itens_venda (
                                    venda_id, produto_id, quantidade, subtotal,
                                    regime_tributario,
                                    aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs,
                                    valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs,
                                    lote_id, quantidade_lote
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    venda_id,
                                    produto_id,
                                    qtd_parcela,
                                    sub_parcela,
                                    str(item.get("regime_tributario", regime_venda)),
                                    float(item.get("aliquotas", {}).get("icms", 0.0) or 0.0),
                                    float(item.get("aliquotas", {}).get("pis", 0.0) or 0.0),
                                    float(item.get("aliquotas", {}).get("cofins", 0.0) or 0.0),
                                    float(item.get("aliquotas", {}).get("ibs", 0.0) or 0.0),
                                    float(item.get("aliquotas", {}).get("cbs", 0.0) or 0.0),
                                    val_parcela["icms"], val_parcela["pis"], val_parcela["cofins"],
                                    val_parcela["ibs"], val_parcela["cbs"],
                                    lote_id,
                                    float(qtd_parcela),
                                ),
                            )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO itens_venda (
                                venda_id, produto_id, quantidade, subtotal,
                                regime_tributario,
                                aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs,
                                valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                venda_id,
                                produto_id,
                                quantidade_vendida,
                                float(item.get("total", 0.0)),
                                str(item.get("regime_tributario", regime_venda)),
                                float(item.get("aliquotas", {}).get("icms", 0.0) or 0.0),
                                float(item.get("aliquotas", {}).get("pis", 0.0) or 0.0),
                                float(item.get("aliquotas", {}).get("cofins", 0.0) or 0.0),
                                float(item.get("aliquotas", {}).get("ibs", 0.0) or 0.0),
                                float(item.get("aliquotas", {}).get("cbs", 0.0) or 0.0),
                                float(item.get("valores_impostos", {}).get("icms", 0.0) or 0.0),
                                float(item.get("valores_impostos", {}).get("pis", 0.0) or 0.0),
                                float(item.get("valores_impostos", {}).get("cofins", 0.0) or 0.0),
                                float(item.get("valores_impostos", {}).get("ibs", 0.0) or 0.0),
                                float(item.get("valores_impostos", {}).get("cbs", 0.0) or 0.0),
                            ),
                        )

                        cursor.execute(
                            """
                            UPDATE produtos
                            SET quantidade_atual = CASE
                                WHEN quantidade_atual - ? < 0 THEN 0
                                ELSE quantidade_atual - ?
                            END
                            WHERE id = ?
                            """,
                            (quantidade_vendida, quantidade_vendida, produto_id),
                        )

                # Orçamento aberto no PDV: apenas o vínculo documental é
                # atualizado junto da venda normal; pagamento/caixa não mudam.
                orcamento_para_vender_id = getattr(self, "_orcamento_para_vender_id", None)
                if orcamento_para_vender_id:
                    cursor.execute(
                        """
                        UPDATE orcamentos
                        SET status = 'VENDA', forma_pagamento = ?, convertido_venda_id = ?,
                            valor_impostos_retidos = ?, valor_liquido = ?
                        WHERE id = ? AND status = 'ORCAMENTO'
                        """,
                        (
                            forma_pgto,
                            venda_id,
                            valor_impostos,
                            valor_liquido,
                            int(orcamento_para_vender_id),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Orçamento não pôde ser vinculado à venda; operação cancelada.")

                # Quitação de Vale ocorre na mesma transação da venda normal.
                # Não há pagamento, caixa ou cupom paralelo: apenas atualiza
                # os documentos que já estavam carregados no PDV.
                vales_para_quitar = list(dict.fromkeys(
                    int(vale_id) for vale_id in (getattr(self, "_vales_para_quitar", None) or [])
                ))
                if vales_para_quitar:
                    marcadores = ",".join("?" for _ in vales_para_quitar)
                    cursor.execute(
                        f"""
                        UPDATE vales
                        SET status = 'QUITADO', data_quitacao = CURRENT_TIMESTAMP,
                            venda_id = ?, caixa_operacao_id = ?, forma_pagamento = ?
                        WHERE id IN ({marcadores}) AND status = 'PENDENTE'
                        """,
                        [venda_id, cx_id, str(forma_pgto or "").upper(), *vales_para_quitar],
                    )
                    if cursor.rowcount != len(vales_para_quitar):
                        raise RuntimeError("Não foi possível quitar todos os vales carregados; venda cancelada.")
                    try:
                        registrar_log(
                            None,
                            "PDV Vale",
                            "Sucesso",
                            f"Vales {vales_para_quitar} quitados pela venda {venda_id} ({forma_pgto})",
                            conn=conn,
                        )
                    except Exception:
                        # A rastreabilidade principal está no UPDATE de `vales`;
                        # log auxiliar não deve desfazer uma venda já persistida.
                        pass
        except Exception as e:
            self._set_status(f"Falha ao registrar venda: {e}", "#ff6666")
            registrar_log(None, "PDV", "Falha", f"Erro ao registrar venda: {e}")
            return

        sucesso = True
        if emitir_nfce is False:
            # Escolha manual do operador (Cupom ou Não Imprimir): sem emissão
            # fiscal/NFC-e e sem artefatos do fluxo ACBr para esta venda.
            registrar_log(
                None,
                "PDV Fiscal",
                "Info",
                f"Venda {venda_id} finalizada SEM emissão de NFC-e (escolha do operador).",
            )
        else:
            fiscal_disponivel = self._fiscal_habilitado()
            if emitir_nfce is True and not fiscal_disponivel:
                # NFC-e solicitada, mas emissão fiscal desativada: informa
                # claramente, NÃO emite e NÃO converte para cupom.
                try:
                    messagebox.showwarning(
                        "Emissão Fiscal",
                        "A emissão fiscal (NFC-e) NÃO está disponível.\n"
                        "'ACBrMonitor (Emissão Fiscal) Ativo' está desativado nas configurações.\n"
                        "A venda foi registrada SEM nota fiscal e SEM cupom não fiscal.",
                        parent=self,
                    )
                except Exception:
                    pass
                self._set_status(
                    "Emissão fiscal indisponível (fiscal_ativo desativado). Venda registrada sem NFC-e.",
                    "#ff6666",
                )
                registrar_log(
                    None,
                    "PDV Fiscal",
                    "Aviso",
                    f"Venda {venda_id}: NFC-e solicitada, mas emissão fiscal desativada. Venda registrada sem NFC-e.",
                )
            elif fiscal_disponivel:
                sucesso, _caminho = self.fiscal.exportar_venda(venda_id, self.itens_carrinho, forma_pgto, valor_bruto)

                dados_json = {
                    "id": venda_id,
                    "total": valor_bruto,
                    "impostos_retidos": valor_impostos,
                    "liquido": valor_liquido,
                    "pagamento": forma_pgto,
                    "itens": self.itens_carrinho,
                }
                self.exportar_venda_fiscal(dados_json)
                self._enviar_comando_nfce(venda_id, forma_pgto, self.itens_carrinho)
            else:
                registrar_log(None, "PDV Fiscal", "Info", f"Venda {venda_id} finalizada sem integração fiscal (modo opcional).")
        if imprimir_cupom:
            self._executar_automacao_pos_venda(
                {
                    "id": venda_id,
                    "itens": self.itens_carrinho,
                    "total": valor_bruto,
                    "impostos_retidos": valor_impostos,
                    "liquido": valor_liquido,
                    "forma_pagamento": forma_pgto,
                    # PARTE C (BLOCO 2): valor efetivamente recebido — permite ao
                    # cupom apresentar RECEBIDO/TROCO (reutiliza cálculo existente).
                    "valor_recebido": valor_pago,
                    # MISTO: parciais reais (forma, valor) para detalhe no cupom.
                    "pagamentos": list(getattr(self, "pagamentos_parciais", None) or []),
                }
            )
        else:
            # GAVETA INDEPENDENTE DA IMPRESSAO: o operador escolheu "NAO
            # IMPRIMIR" (F12) e mesmo assim a venda paga em DINHEIRO precisa
            # abrir o gaveteiro. Reutiliza exatamente a mesma decisao/acao do
            # fluxo com cupom (`_acionar_gaveta_pos_venda`), sem imprimir nada
            # e sem tocar em pagamento, troco, vale ou fechamento.
            _gd_acionada, _gd_erro = self._acionar_gaveta_pos_venda(forma_pgto)
            if _gd_erro is not None:
                self._set_status(f"Venda registrada, mas falha ao abrir gaveta: {_gd_erro}", "#ff6666")
            elif _gd_acionada:
                self._set_status("Gaveta acionada (sem impressão de comprovante).", "#2ecc71")

        # RESÍDUO VISUAL PÓS-VENDA REMOVIDO (regra de exibição): a faixa
        # intermediária NÃO mostra mais o antigo resumo da venda — nem a
        # linha de conclusão e nem os valores de Bruto, Impostos, Líquido
        # ou Recebido — tampouco qualquer outro resumo pós-venda. A faixa
        # é limpa no bloco final desta finalização e permanece somente
        # "Desenvolvido por FRS Solutions".
        # Lógica da venda/pagamentos/gaveta/cupom/fechamento: NÃO alterada.
        if sucesso:
            registrar_log(None, "PDV", "Sucesso", f"Venda {venda_id} ({forma_pgto}) exportada.")

        if hasattr(self, "_limpar_contexto_documental"):
            self._limpar_contexto_documental()
        else:
            self._vales_para_quitar = []
            self._operacao_documento_tipo = None
            self._orcamento_para_vender_id = None
            self._operacao_vale_cliente_id = None
        self.itens_carrinho = []
        self._renderizar_carrinho()
        self.atualizar_total_display()
        self.ent_valor_pago.delete(0, "end")
        self.lbl_troco_venda.configure(text="R$ 0,00")
        if hasattr(self, "limpar_pagamentos_recebidos"):
            self.limpar_pagamentos_recebidos()
        else:
            self.valor_pago_acumulado = 0.0
            self.pagamentos_parciais = []
        self._avaliar_limite_caixa()
        # LIMPEZA FINAL DA FAIXA INTERMEDIÁRIA (somente exibição): venda
        # finalizada, sem resíduo de resumo/automação/status na tela.
        # Ao final permanece SOMENTE "Desenvolvido por FRS Solutions".
        # O painel grande inferior (VALOR PAGO | TROCO | TOTAL DA VENDA)
        # NÃO é tocado aqui — tamanho, posição e cores inalterados.
        self._set_status("")
        if hasattr(self, "_retornar_foco_pdv"):
            self._retornar_foco_pdv()
        elif hasattr(self, "_safe_focus"):
            self._safe_focus(getattr(self, "ent_cod_barras", getattr(self, "ent_quantidade", None)))

    def _retornar_foco_pdv(self):
        target = getattr(self, "ent_cod_barras", getattr(self, "ent_quantidade", None))
        if hasattr(self, "_safe_after") and hasattr(self, "_safe_focus"):
            self._safe_after(30, lambda: self._safe_focus(target))
        elif hasattr(self, "_safe_focus"):
            self._safe_focus(target)

    def _to_float(self, texto):
        return parse_numero(texto, "Valor", minimo=0)

    def _obter_dinheiro_atual_caixa(self):
        if not self.caixa_id:
            return 0.0

        with get_db_connection() as conn:
            import modulo_financeiro as _fin
            saldo_row = conn.execute("SELECT saldo_inicial FROM caixa_operacao WHERE id = ?", (self.caixa_id,)).fetchone()
            saldo_inicial = float(saldo_row[0] or 0.0) if saldo_row else 0.0
            abertura, fechamento = _fin._janela_caixa(conn, self.caixa_id)
            vend = _fin._vendas_por_forma_tabela_caixa(conn, "vendas", self.caixa_id, abertura, fechamento)
            tem_v = any(float(t or 0.0) > 0 for _, t in vend)
            if not tem_v:
                vend = list(vend) + list(_fin._vendas_por_forma_tabela_caixa(conn, "vendas_dia", self.caixa_id, abertura, fechamento))
            vendas_dinheiro = 0.0
            for forma, tot in vend:
                if str(forma or "").strip().upper() == "DINHEIRO":
                    vendas_dinheiro += float(tot or 0.0)
            movs = _fin._movs_caixa(conn, self.caixa_id, abertura, fechamento)
            total_sangrias = float(movs.get("total_sangrias", 0.0) or 0.0)
            total_suprimentos = float(movs.get("total_reforcos", 0.0) or 0.0)

        return (saldo_inicial + vendas_dinheiro + total_suprimentos) - total_sangrias

    def _avaliar_limite_caixa(self):
        try:
            self.limite_caixa_atual = obter_limite_sangria_preventiva()
            dinheiro_caixa = self._obter_dinheiro_atual_caixa()
            excesso = round(dinheiro_caixa - self.limite_caixa_atual, 2)
            self.excesso_caixa_atual = excesso if excesso > 0 else 0.0

            if excesso > 0:
                self.lbl_aviso_limite.configure(
                    text=(
                        f"Atenção: Limite de caixa excedido. Recomenda-se sangria "
                        f"(limite {self._formatar_moeda_br(self.limite_caixa_atual)} | excesso {self._formatar_moeda_br(excesso)})"
                    ),
                    text_color="#f39c12",
                )
                self.lbl_aviso_limite.pack(side="bottom", pady=(0, 4))
                self._set_status(f"Caixa excedeu o limite de {self._formatar_moeda_br(self.limite_caixa_atual)}. Recomendada sangria.", "#f39c12")
            else:
                self.lbl_aviso_limite.pack_forget()
                self.excesso_caixa_atual = 0.0
        except Exception as e:
            registrar_log(None, "PDV Limite Caixa", "Falha", f"Erro ao avaliar limite: {e}")

    def _abrir_modal_movimento(self, tipo, valor_sugerido=0.0, motivo_padrao=""):
        modal = ctk.CTkToplevel(self)
        modal.title(f"Registrar {tipo}")
        modal.geometry("420x300")
        modal.grab_set()

        ctk.CTkLabel(modal, text=f"VALOR ({tipo})", font=("Arial", 12, "bold")).pack(pady=(20, 5))
        ent_valor = ctk.CTkEntry(modal, width=240)
        if valor_sugerido and valor_sugerido > 0:
            ent_valor.insert(0, self._formatar_moeda_br(valor_sugerido))
        ent_valor.pack()
        aplicar_padrao_entrada_numerica(ent_valor, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(modal, text="MOTIVO / DESCRIÇÃO", font=("Arial", 12, "bold")).pack(pady=(12, 5))
        ent_obs = ctk.CTkEntry(modal, width=320)
        if motivo_padrao:
            ent_obs.insert(0, motivo_padrao)
        ent_obs.pack()

        def fechar_modal():
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()
            self._retornar_foco_pdv()

        def confirmar_movimento():
            try:
                valor = self._to_float(ent_valor.get())
                obs = ent_obs.get().strip() or f"{tipo.title()} manual"
                if valor <= 0:
                    self._set_status(f"Valor inválido para {tipo.lower()}.", "#ff6666")
                    return

                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cols_fin = {r[1] for r in cursor.execute("PRAGMA table_info(financeiro)").fetchall()}
                    cx_mov = self.caixa_id
                    if tipo == "SANGRIA":
                        descricao_fin = f"Sangria: {obs}"
                        if obs == "Sangria Preventiva - Excesso de Caixa":
                            descricao_fin = obs
                        cursor.execute(
                            "INSERT INTO sangrias (valor, justificativa, caixa_operacao_id) VALUES (?, ?, ?)",
                            (valor, obs, self.caixa_id),
                        )
                        if "caixa_operacao_id" in cols_fin:
                            cursor.execute(
                                "INSERT INTO financeiro (valor, tipo, descricao, caixa_operacao_id) VALUES (?, ?, ?, ?)",
                                (valor, "Saída", descricao_fin, cx_mov),
                            )
                        else:
                            cursor.execute(
                                "INSERT INTO financeiro (valor, tipo, descricao) VALUES (?, ?, ?)",
                                (valor, "Saída", descricao_fin),
                            )
                    else:
                        if "caixa_operacao_id" in cols_fin:
                            cursor.execute(
                                "INSERT INTO financeiro (valor, tipo, descricao, caixa_operacao_id) VALUES (?, ?, ?, ?)",
                                (valor, "Entrada", f"Suprimento: {obs}", cx_mov),
                            )
                        else:
                            cursor.execute(
                                "INSERT INTO financeiro (valor, tipo, descricao) VALUES (?, ?, ?)",
                                (valor, "Entrada", f"Suprimento: {obs}"),
                            )

                valor_fmt = self._formatar_moeda_br(valor)
                self._set_status(f"{tipo.title()} registrada: {valor_fmt}", "#2ecc71")
                registrar_log(None, f"Registro de {tipo.title()}", "Sucesso", f"Valor: {valor_fmt}, Obs: {obs}")
                self._avaliar_limite_caixa()
                fechar_modal()
            except ValueError:
                self._set_status(f"Valor inválido para {tipo.lower()}.", "#ff6666")
            except Exception as e:
                self._set_status(f"Erro ao registrar {tipo.lower()}: {e}", "#ff6666")
                registrar_log(None, f"Registro de {tipo.title()}", "Falha", f"Erro: {e}")

        botoes = ctk.CTkFrame(modal, fg_color="transparent")
        botoes.pack(pady=22)
        ctk.CTkButton(botoes, text="CONFIRMAR", fg_color="#27ae60", width=140, command=confirmar_movimento).pack(side="left", padx=8)
        ctk.CTkButton(botoes, text="CANCELAR", fg_color="#7f8c8d", width=140, command=fechar_modal).pack(side="left", padx=8)

        modal.protocol("WM_DELETE_WINDOW", fechar_modal)
        self._safe_focus(ent_valor)

    def modal_suprimento(self):
        self._abrir_modal_movimento("SUPRIMENTO")

    def modal_sangria(self, preencher_excesso=False):
        valor_sugerido = 0.0
        motivo = ""

        self._avaliar_limite_caixa()
        if preencher_excesso and self.excesso_caixa_atual > 0:
            valor_sugerido = self.excesso_caixa_atual
            motivo = "Sangria Preventiva - Excesso de Caixa"
        elif self.excesso_caixa_atual > 0:
            valor_sugerido = self.excesso_caixa_atual

        self._abrir_modal_movimento("SANGRIA", valor_sugerido=valor_sugerido, motivo_padrao=motivo)

    MODALIDADES_FECHAMENTO = ("DINHEIRO", "DEBITO", "CREDITO", "VOUCHER", "PIX")

    def _abrir_modal_conferencia_fechamento(self, esperado):
        """Modal de conferência cega: somente o valor contado (informado).

        Por segurança, NÃO exibe total do sistema, diferenças ou valores
        esperados durante a digitação — o operador informa apenas quanto
        efetivamente contou por modalidade. Sistema/Diferença aparecem
        somente no resumo pós-fechamento.

        Retorna {modalidade: valor_informado} ao confirmar ou None ao cancelar.
        """
        modal = ctk.CTkToplevel(self)
        modal.title("Conferência de Fechamento")
        modal.geometry("460x440")
        modal.transient(self)
        modal.grab_set()
        modal.resizable(False, False)
        modal.configure(fg_color="#181818")

        resultado = {"informado": None}

        ctk.CTkLabel(
            modal,
            text="CONFERÊNCIA DE FECHAMENTO",
            font=("Roboto", 15, "bold"),
            text_color="#f1c40f",
        ).pack(pady=(14, 4))
        ctk.CTkLabel(
            modal,
            text="Informe o valor contado de cada modalidade.",
            font=("Roboto", 11),
            text_color="#9aa0a6",
        ).pack(pady=(0, 10))

        grid = ctk.CTkFrame(modal, fg_color="#202020")
        grid.pack(fill="both", expand=True, padx=16)

        for col, titulo in enumerate(("MODALIDADE", "VALOR CONTADO (R$)")):
            ctk.CTkLabel(grid, text=titulo, font=("Roboto", 12, "bold"), text_color="#4aa3ff").grid(
                row=0, column=col, padx=10, pady=(10, 6), sticky="w"
            )

        entradas = {}

        for linha, modalidade in enumerate(self.MODALIDADES_FECHAMENTO, start=1):
            ctk.CTkLabel(grid, text=modalidade, font=("Roboto", 12, "bold")).grid(
                row=linha, column=0, padx=10, pady=4, sticky="w"
            )
            entrada = ctk.CTkEntry(grid, width=150, justify="center", font=("Roboto", 12))
            entrada.grid(row=linha, column=1, padx=10, pady=4)
            aplicar_padrao_entrada_numerica(entrada, inteiro=False, casas_decimais=2)
            entradas[modalidade] = entrada

        def _parse_valor(txt):
            try:
                return float(str(txt).strip().replace("R$", "").replace(".", "").replace(",", ".") or "0")
            except ValueError:
                return 0.0

        botoes = ctk.CTkFrame(modal, fg_color="transparent")
        botoes.pack(fill="x", padx=16, pady=(6, 14))
        ctk.CTkButton(
            botoes, text="CANCELAR", fg_color="#6c757d", width=140,
            command=lambda: (resultado.__setitem__("informado", None), modal.destroy()),
        ).pack(side="right", padx=6)

        def _confirmar():
            informado = {}
            for mod in self.MODALIDADES_FECHAMENTO:
                informado[mod] = _parse_valor(entradas[mod].get())
            resultado["informado"] = informado
            modal.destroy()

        ctk.CTkButton(
            botoes, text="CONFIRMAR FECHAMENTO", fg_color="#27ae60", width=220,
            command=_confirmar,
        ).pack(side="right", padx=6)

        modal.protocol("WM_DELETE_WINDOW", lambda: (resultado.__setitem__("informado", None), modal.destroy()))
        modal.wait_window()
        return resultado["informado"]

    def processar_fechamento_inteligente(self, exibir_conferencia=True):
        """Fechamento analítico por modalidade, sem compensação cruzada.

        Compara Valor Informado x Valor Calculado x Diferença individual para
        DINHEIRO, DEBITO, CREDITO, VOUCHER e PIX. Falta em uma modalidade jamais
        abate sobra em outra: cada divergência é gravada individualmente na tabela
        caixa_conferencia e mantida visível no resumo final.
        """
        MODALIDADES = self.MODALIDADES_FECHAMENTO
        try:
            if self.caixa_id is None:
                self._set_status("Nenhum caixa aberto para fechar.", "#ff6666")
                return

            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT saldo_inicial FROM caixa_operacao WHERE id = ?", (self.caixa_id,))
                linha_saldo = cursor.fetchone()
                saldo_inicial = float(linha_saldo[0] or 0.0) if linha_saldo else 0.0
            # Movimentações (sangrias/reforços) lidas FORA da conexão acima:
            # a rotina abre a própria conexão e, se aninhada, recebia
            # "database is locked" — o que zerava sangrias/reforços na
            # composição do DINHEIRO do ciclo.
            movs = modulo_financeiro.obter_movimentacoes_caixa(self.caixa_id)
            total_sangrias = float(movs.get("total_sangrias", 0.0) or 0.0)
            total_reforcos = float(movs.get("total_reforcos", 0.0) or 0.0)
            lista_sangrias = list(movs.get("sangrias", []) or [])
            lista_reforcos = list(movs.get("reforcos", []) or [])

            vendas_por_forma = modulo_financeiro.obter_vendas_dia_por_forma(caixa_id=self.caixa_id)
            esperado = {}
            for modalidade in MODALIDADES:
                if modalidade == "DINHEIRO":
                    esperado[modalidade] = round(
                        saldo_inicial + vendas_por_forma.get("DINHEIRO", 0.0) + total_reforcos - total_sangrias, 2
                    )
                else:
                    esperado[modalidade] = round(vendas_por_forma.get(modalidade, 0.0), 2)

            informado = {m: 0.0 for m in MODALIDADES}
            if exibir_conferencia:
                try:
                    informado_modal = self._abrir_modal_conferencia_fechamento(esperado)
                    if informado_modal is None:
                        self._set_status("Fechamento cancelado pelo operador.", "#f39c12")
                        return
                    informado = {m: round(float((informado_modal or {}).get(m, 0.0) or 0.0), 2)
                                 for m in MODALIDADES}
                except Exception as e_modal:
                    # Rotinas automáticas/sem UI (ex.: checklist): mantém conferência
                    # zerada (operador não informou) em vez de espelhar o sistema.
                    # REGRA 1.0.20: INFORMADO = exatamente o valor informado;
                    # ABERTURA NÃO participa do informado nem da diferença.
                    registrar_log(None, "Fechamento de Caixa", "Aviso", f"Conferência visual indisponível ({e_modal}); informado zerado.")
                    informado = {m: 0.0 for m in MODALIDADES}

            # REGRA 1.0.20:
            # - SISTEMA (esperado) inclui a ABERTURA no DINHEIRO (TOTAL DO SISTEMA).
            # - CONFERENCIA usa o SISTEMA DO CICLO (calculado = esperado_mov,
            #   sem abertura) e o INFORMADO puro do operador (modal/0.00).
            # - DIFERENCA = INFORMADO - SISTEMA DO CICLO.
            esperado_mov = {m: (round(float(esperado.get(m, 0.0) or 0.0)
                                      - (saldo_inicial if m == "DINHEIRO" else 0.0), 2))
                            for m in MODALIDADES}
            with get_db_connection() as conn_fechamento:
                for modalidade in MODALIDADES:
                    diferenca = round(informado[modalidade] - esperado_mov[modalidade], 2)
                    conn_fechamento.execute(
                        """
                        INSERT INTO caixa_conferencia
                            (caixa_operacao_id, modalidade, valor_calculado, valor_sistema, valor_informado, diferenca)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (self.caixa_id, modalidade, esperado_mov[modalidade], esperado_mov[modalidade], informado[modalidade], diferenca),
                    )
                conn_fechamento.execute(
                    "UPDATE caixa_operacao SET status = 'FECHADO', data_fechamento = CURRENT_TIMESTAMP WHERE id = ?",
                    (self.caixa_id,),
                )

            sucesso, msg_fechamento = modulo_financeiro.fechar_caixa(caixa_id=self.caixa_id)
            # REGRA 1.0.20 (resumo inferior):
            # - TOTAL DO SISTEMA segue com ABERTURA (total_sistema).
            # - DIFERENCA da conferencia usa INFORMADO x SISTEMA DO CICLO
            #   (total_sistema_ciclo, sem abertura).
            total_sistema = round(sum(esperado.values()), 2)
            total_sistema_ciclo = round(sum(esperado_mov.values()), 2)
            total_informado = round(sum(informado.values()), 2)
            diferenca_geral = round(total_informado - total_sistema_ciclo, 2)
            caixa_fechado_id = self.caixa_id
            # TOTAL DE VALE DO CICLO — SOMENTE INFORMATIVO.
            # Leitura isolada dos Vales GERADOS (criados) neste ciclo de caixa.
            # NAO entra em DINHEIRO, TOTAL DO SISTEMA, VALOR INFORMADO,
            # DIFERENCA nem em qualquer outra composicao financeira do
            # fechamento: nenhum calculo acima foi alterado por causa dela.
            total_vales_gerados = 0.0
            try:
                total_vales_gerados = float(
                    modulo_financeiro.obter_total_vales_caixa(caixa_fechado_id) or 0.0
                )
            except Exception as e_vales:
                registrar_log(None, "Fechamento de Caixa (Vales)", "Aviso", f"Total informativo de vales indisponível: {e_vales}")
                total_vales_gerados = 0.0
            if sucesso:
                self._set_status("Caixa fechado com sucesso.", "#2ecc71")
                # Indicador compacto volta ao estado real (🔴 CAIXA FECHADO).
                self._atualizar_indicadores_caixa(False)
                divergencias = ", ".join(f"{m} {informado[m] - esperado_mov[m]:+.2f}" for m in MODALIDADES)
                registrar_log(
                    None,
                    "Fechamento de Caixa",
                    "Sucesso",
                    f"Caixa {caixa_fechado_id} fechado. Sistema {total_sistema:.2f} | Informado {total_informado:.2f} "
                    f"| Diferença geral {diferenca_geral:+.2f} | Divergências: {divergencias}",
                )
                # PARTES A/B (BLOCO 2) — pergunta de impressão e resumo visual.
                # Reutiliza os dados já calculados/persistidos (esperado, informado,
                # totais, caixa encerrado). O resumo aparece nos dois casos (SIM/NÃO).
                dados_resumo = {
                    "caixa_id": caixa_fechado_id,
                    "data_hora": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "operador": getattr(getattr(self.master, "usuario_atual", None), "get", lambda _k, _d=None: None)("nome", None)
                    if getattr(self, "master", None) is not None and getattr(self.master, "usuario_atual", None)
                    else None,
                    "modalidades": [
                        {
                            "modalidade": m,
                            "calculado": round(esperado_mov[m], 2),
                            "informado": round(informado[m], 2),
                            "diferenca": round(informado[m] - esperado_mov[m], 2),
                        }
                        for m in MODALIDADES
                    ],
                    "total_calculado": total_sistema,
                    "total_informado": total_informado,
                    "diferenca_geral": diferenca_geral,
                    "saldo_abertura": saldo_inicial,
                    "total_sangrias": round(total_sangrias, 2),
                    "total_reforcos": round(total_reforcos, 2),
                    "sangrias": [{"hora": h, "valor": v} for h, v in lista_sangrias],
                    "reforcos": [{"hora": h, "valor": v} for h, v in lista_reforcos],
                    # INFORMATIVO: total dos Vales gerados no ciclo. Nao compoe
                    # nenhum total financeiro do fechamento (nem DINHEIRO, nem
                    # TOTAL DO SISTEMA, VALOR INFORMADO ou DIFERENCA).
                    "total_vales_gerados": round(total_vales_gerados, 2),
                }
                try:
                    imprimir_resumo = self._perguntar_impressao_fechamento()
                except Exception:
                    imprimir_resumo = False
                if imprimir_resumo:
                    try:
                        self._imprimir_resumo_fechamento(dados_resumo)
                    except Exception as e_imp:
                        registrar_log(None, "Fechamento de Caixa", "Falha", f"Erro impressão resumo: {e_imp}")
                try:
                    self._exibir_resumo_fechamento(dados_resumo, ao_concluir=self._perguntar_abertura_novo_caixa)
                except Exception as e_res:
                    registrar_log(None, "Fechamento de Caixa", "Aviso", f"Resumo visual indisponível ({e_res}).")
                    self._perguntar_abertura_novo_caixa()
            else:
                self._set_status(msg_fechamento, "#ff6666")
                registrar_log(None, "Fechamento de Caixa", "Falha", msg_fechamento)
        except Exception as e:
            self._set_status(f"Falha no fechamento: {e}", "#ff6666")
            registrar_log(None, "Fechamento de Caixa", "Falha", f"Erro: {e}")

    def _perguntar_impressao_fechamento(self):
        """Pergunta SIM/NÃO sobre imprimir o resumo do fechamento (BLOCO 2, Parte B).

        Retorna True (IMPRIMIR) ou False (NÃO IMPRIMIR). Em ambiente sem UI
        (checklist/testes), retorna False — nunca bloqueia o fluxo.
        """
        from tkinter import messagebox

        try:
            return bool(
                messagebox.askyesno(
                    "Imprimir fechamento",
                    "Deseja imprimir o resumo do fechamento de caixa?",
                    parent=self,
                )
            )
        except Exception:
            return False

    def _montar_texto_resumo_fechamento(self, dados):
        """Monta o texto do resumo do fechamento (reutilizado na tela e na impressão)."""
        largura = 42
        sep = "-" * largura
        linhas = [
            "FECHAMENTO DE CAIXA",
            sep,
            f"Data/Hora: {dados.get('data_hora', '')}",
            f"Caixa: {dados.get('caixa_id', '')}",
        ]
        operador = dados.get("operador")
        if operador:
            linhas.append(f"Operador: {operador}")
        linhas.append(sep)
        linhas.append(f"{'MODALIDADE':<12} {'SISTEMA':>9} {'INFORM.':>9} {'DIFER.':>9}")
        def _fmt_dif_col(_v):
            # Visual apenas: sem "+" em zero; "+" somente em diferenca positiva.
            try:
                _v = round(float(_v or 0.0), 2)
            except Exception:
                _v = 0.0
            if _v > 0.0004:
                # Sinal "+" SOMENTE no sinal do numero (sem usar "+" como
                # caractere de preenchimento, que poluia a coluna).
                return f"{_v:>+9.2f}"
            return f"{_v:>9.2f}"
        # REGRA 1.0.20 — tabela superior de conferencia (sem abertura):
        # SISTEMA = SISTEMA DO CICLO (calculado ja vem sem abertura);
        # INFORMADO = exatamente o valor informado pelo operador (0.00 se
        # nao informado); DIFERENCA = INFORMADO - SISTEMA.
        _abertura = float(dados.get("saldo_abertura", 0.0) or 0.0)
        _total_sis = float(dados.get("total_calculado", 0.0) or 0.0)
        _total_inf = float(dados.get("total_informado", 0.0) or 0.0)
        _dif = float(dados.get("diferenca_geral", 0.0) or 0.0)
        _mods = {str(i.get("modalidade", "")).upper(): i for i in dados.get("modalidades", [])}
        _din = _mods.get("DINHEIRO", {})
        _din_sis = round(float(_din.get("calculado", 0.0) or 0.0), 2)
        _din_inf = round(float(_din.get("informado", 0.0) or 0.0), 2)
        _din_dif = round(_din_inf - _din_sis, 2)
        for item in dados.get("modalidades", []):
            if str(item.get("modalidade", "")).strip().upper() == "DINHEIRO":
                _sis, _inf, _df = _din_sis, _din_inf, _din_dif
            else:
                _sis = float(item.get("calculado", 0.0) or 0.0)
                _inf = float(item.get("informado", 0.0) or 0.0)
                _df = float(item.get("diferenca", 0.0) or 0.0)
            linhas.append(
                f"{str(item.get('modalidade', '')):<12} "
                f"{_sis:>9.2f} "
                f"{_inf:>9.2f} "
                f"{_fmt_dif_col(_df)}"
            )
        linhas.append(sep)
        linhas.append(f"VALOR DE ABERTURA DO CAIXA: R$ {_abertura:>9.2f}")
        linhas.append(sep)
        # REGRA 1.0.20 — resumo inferior: abertura separada, DINHEIRO somente
        # com a movimentacao do ciclo (sem abertura), TOTAL DO SISTEMA com
        # abertura + movimentacao, e DIFERENCA = INFORMADO x SISTEMA DO CICLO.
        linhas.append(f"DINHEIRO:                  R$ {_din_sis:>9.2f}")
        for _m in ("DEBITO", "CREDITO", "VOUCHER", "PIX"):
            _v = float((_mods.get(_m) or {}).get("calculado", 0.0) or 0.0)
            linhas.append(f"{_m:<12}                  R$ {_v:>9.2f}")
        linhas.append(sep)
        _sang = list(dados.get("sangrias", []) or [])
        _ref = list(dados.get("reforcos", []) or [])
        if _sang:
            linhas.append("SANGRIAS")
            for _s in _sang:
                try:
                    _sv = float(_s.get("valor", 0.0) or 0.0)
                except Exception:
                    _sv = 0.0
                linhas.append(f"{str(_s.get('hora', '--:--'))} - R$ {_sv:>8.2f}")
        if _ref:
            linhas.append("REFORCOS")
            for _r in _ref:
                try:
                    _rv = float(_r.get("valor", 0.0) or 0.0)
                except Exception:
                    _rv = 0.0
                linhas.append(f"{str(_r.get('hora', '--:--'))} - R$ {_rv:>8.2f}")
        if _sang or _ref:
            linhas.append(sep)
        _dif_fmt = _fmt_dif_col(_dif)
        if _dif_fmt.strip().startswith("+"):
            _dif_linha = f"DIFERENCA:                   +R$ {_dif:>8.2f}"
        elif _dif < -0.0004:
            _dif_linha = f"DIFERENCA:                   -R$ {abs(_dif):>8.2f}"
        else:
            _dif_linha = f"DIFERENCA:                    R$ {_dif:>8.2f}"
        linhas.append(f"TOTAL DO SISTEMA:            R$ {_total_sis:>9.2f}")
        linhas.append(f"VALOR INFORMADO:             R$ {_total_inf:>9.2f}")
        linhas.append(_dif_linha)
        linhas.append(sep)
        # ================================ LINHA SOMENTE INFORMATIVA (pos-totais)
        # TOTAL DE VALE GERADO DURANTE O DIA: informa o total dos Vales GERADOS
        # (criados) neste ciclo de caixa. NAO entra em DINHEIRO, TOTAL DO
        # SISTEMA, VALOR INFORMADO, DIFERENCA nem em qualquer outra composicao
        # financeira acima — nenhuma linha de calculo do fechamento foi
        # alterada por causa desta informacao.
        try:
            _vales = round(float(dados.get("total_vales_gerados", 0.0) or 0.0), 2)
        except Exception:
            _vales = 0.0
        _vales_txt = f"{_vales:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
        linhas.append(f"TOTAL DE VALE GERADO DURANTE O DIA: R$ {_vales_txt}")
        linhas.append(sep)
        return "\n".join(linhas)

    def _imprimir_resumo_fechamento(self, dados):
        """Imprime o resumo do fechamento via rotina ESC/POS existente (BLOCO 2, Parte B).

        Reutiliza ``_enviar_raw_impressora_padrao``; não altera impressão de
        vendas, NFC-e ou ACBr.
        """
        texto = self._montar_texto_resumo_fechamento(dados)
        payload = (texto + "\n\n\n").encode("cp850", errors="replace")
        self._enviar_raw_impressora_padrao(payload)
        registrar_log(None, "Fechamento de Caixa", "Sucesso", "Resumo do fechamento impresso.")

    def _exibir_resumo_fechamento(self, dados, ao_concluir=None):
        """Exibe o modal de resumo visual pós-fechamento (BLOCO 2, Parte A).

        Permanece visível para conferência/foto; aparece tanto no SIM quanto
        no NÃO da impressão ("não imprimir" ≠ "não mostrar").
        Ao fechar (botão FECHAR ou X), executa ``ao_concluir`` — pergunta
        "ABRIR NOVO CAIXA?" e só abre a tela de contagem no SIM (no NÃO o
        caixa permanece fechado e o programa pode ser encerrado).
        """
        resumo = ctk.CTkToplevel(self)
        resumo.title("FECHAMENTO DE CAIXA — RESUMO")
        resumo.geometry("560x620")
        try:
            resumo.transient(self)
            resumo.grab_set()
        except Exception:
            pass

        concluido = {"ok": False}

        def _concluir_resumo():
            if concluido["ok"]:
                return
            concluido["ok"] = True
            try:
                if hasattr(resumo, "grab_release"):
                    resumo.grab_release()
            except Exception:
                pass
            try:
                resumo.destroy()
            except Exception:
                pass
            if callable(ao_concluir):
                try:
                    ao_concluir()
                except Exception as e_cb:
                    registrar_log(None, "Fechamento de Caixa", "Aviso", f"Pós-resumo indisponível ({e_cb}).")

        ctk.CTkLabel(
            resumo,
            text="FECHAMENTO DE CAIXA",
            font=("Roboto", 18, "bold"),
        ).pack(pady=(14, 2))
        ctk.CTkLabel(
            resumo,
            text=f"{dados.get('data_hora', '')}   |   Caixa {dados.get('caixa_id', '')}"
            + (f"   |   Operador: {dados.get('operador')}" if dados.get("operador") else ""),
            font=("Roboto", 12),
            text_color="#9aa0a6",
        ).pack(pady=(0, 8))

        texto = ctk.CTkTextbox(resumo, font=("Consolas", 12), wrap="none")
        texto.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        texto.insert("1.0", self._montar_texto_resumo_fechamento(dados))
        texto.configure(state="disabled")

        ctk.CTkButton(
            resumo,
            text="FECHAR",
            fg_color="#27ae60",
            height=40,
            font=("Roboto", 14, "bold"),
            command=_concluir_resumo,
        ).pack(padx=12, pady=(0, 14), fill="x")
        resumo.protocol("WM_DELETE_WINDOW", _concluir_resumo)

    def _perguntar_abertura_novo_caixa(self):
        """Após o resultado final do fechamento: pergunta "ABRIR NOVO CAIXA?".

        SIM → abre a tela de CONTAGEM DE ABERTURA (ciclo existente).
        NÃO → o caixa permanece FECHADO (``caixa_id = None``, indicador
        🔴 CAIXA FECHADO), a janela do PDV é encerrada e o foco volta
        automaticamente ao MENU PRINCIPAL. A tela de contagem NUNCA abre
        automaticamente depois de um fechamento.
        """
        abrir = False
        try:
            abrir = bool(
                messagebox.askyesno(
                    "Novo caixa",
                    "ABRIR NOVO CAIXA?",
                    parent=self,
                )
            )
        except Exception:
            abrir = False

        if abrir:
            self._encerrar_ciclo_e_abrir_novo_caixa()
            return

        # NÃO: estado de caixa fechado preservado, sem modal de abertura.
        self.caixa_id = None
        self._atualizar_indicadores_caixa(False)
        self._set_status("Caixa fechado. Nenhum novo caixa aberto.", "#f1c40f")
        # NÃO REABRIR CAIXA: encerra o PDV e devolve o MENU PRINCIPAL.
        self._fechar_pdv_e_retornar_menu()

    def _fechar_pdv_e_retornar_menu(self):
        """NÃO REABRIR CAIXA: fecha a janela do PDV e retorna ao MENU PRINCIPAL.

        Executado SOMENTE no caminho "NÃO REABRIR" do fim do fechamento de
        caixa. Não interfere no fluxo de "REABRIR CAIXA", que continua usando
        ``_encerrar_ciclo_e_abrir_novo_caixa`` (abre a contagem de abertura).
        """
        master = getattr(self, "master", None)
        try:
            if getattr(master, "_janela_pdv", None) is self:
                master._janela_pdv = None
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        try:
            if master is not None and master.winfo_exists():
                resgatar = getattr(master, "_resgatar_janela", None)
                if callable(resgatar):
                    resgatar()
                master.lift()
                master.focus_force()
        except Exception:
            pass

    def _encerrar_ciclo_e_abrir_novo_caixa(self):
        """Encerra o ciclo do caixa fechado e inicia abertura do próximo.

        Executado SOMENTE após o operador dispensar a tela de resumo
        (botão FECHAR ou X). Não fecha o programa, não destrói o PDV e
        não volta ao menu: apenas libera ``caixa_id`` e reabre o modal
        de abertura para o novo ciclo.
        """
        self.caixa_id = None
        self._atualizar_indicadores_caixa(False)
        self._set_status("Caixa anterior encerrado. Abra o novo caixa.", "#f1c40f")
        try:
            self.abrir_caixa_modal()
        except Exception as e:
            registrar_log(None, "Abertura de Caixa", "Falha", f"Erro ao iniciar novo ciclo: {e}")
            self._set_status(f"Falha ao abrir novo caixa: {e}", "#ff6666")

    def _minimizar_pdv(self):
        """Minimiza somente a janela, preservando todos os estados internos."""
        try:
            self.iconify()
        except Exception:
            pass

    def _caixa_esta_aberto(self):
        try:
            with get_db_connection() as conn:
                if self.caixa_id:
                    row = conn.execute(
                        "SELECT status FROM caixa_operacao WHERE id = ?",
                        (self.caixa_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT status FROM caixa_operacao WHERE status = 'ABERTO' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                if row:
                    return str(row[0] or "").strip().upper() == "ABERTO"
                return self.caixa_id is not None
        except Exception:
            return self.caixa_id is not None

    def _ao_fechar_janela(self):
        if self._caixa_esta_aberto():
            messagebox.showwarning(
                "Caixa aberto",
                "Caixa aberto. Favor fechar o caixa primeiro.",
                parent=self,
            )
            return "break"
        try:
            self.destroy()
        except Exception:
            pass
        return None

    def reentrar_apos_menu(self):
        """Reexibe o PDV e revalida o ciclo de caixa antes de operar."""
        try:
            if self._id_after_verificacao_caixa is not None:
                self.after_cancel(self._id_after_verificacao_caixa)
            self._id_after_verificacao_caixa = None
            self.deiconify()
            self.lift()
            self.focus_force()
            self.verificar_caixa_aberto()
        except Exception as e:
            try:
                registrar_log(None, "Reentrada do PDV", "Falha", f"Erro ao reexibir/verificar caixa: {e}")
            except Exception:
                pass
            try:
                self._set_status(f"Falha ao verificar caixa: {e}", "#ff6666")
            except Exception:
                pass

    def _ao_reexibir_pdv(self, event=None):
        """Ao restaurar o PDV minimizado, mantém o modal de abertura pendente."""
        try:
            if event is not None and str(event.widget) != str(self):
                return None
            modal = self.modal_abertura
            if modal is not None and modal.winfo_exists() and modal.state() == "withdrawn":
                self.after(30, self._restaurar_modal_abertura)
        except Exception:
            pass
        return None

    def _restaurar_modal_abertura(self):
        try:
            modal = self.modal_abertura
            if modal is not None and modal.winfo_exists():
                modal.deiconify()
                modal.lift()
                modal.grab_set()
        except Exception:
            pass

    def _ligar_atalho_end_gaveta(self):
        """Atalho <End> -> SOMENTE abrir a gaveta.

        Não executa nenhum outro fluxo: não finaliza venda, não inicia
        pagamento, não imprime, não fecha/reabre caixa, não cancela item e
        não altera carrinho, estoque ou financeiro. Reutiliza a rotina segura
        já existente (`abrir_gaveta`, ESC/POS), sem duplicar lógica.
        """
        try:
            self.bind("<End>", self._ao_pressionar_end_abrir_gaveta)
        except Exception as e:
            registrar_log(None, "PDV Gaveta", "Falha", f"Falha ao ligar atalho <End>: {e}")

    def _ao_pressionar_end_abrir_gaveta(self, event=None):
        """<End> -> uma única ação: ACIONAR A GAVETA.

        Forma de pagamento NÃO é informada de propósito: a trava de segurança
        de `abrir_gaveta` só restringe quando a forma é informada, e aqui a
        abertura é um comando explícito do operador, independente do fluxo de
        venda em andamento.
        """
        try:
            # Não sequestra a tecla quando o foco está em campo de edição
            # (mantém o comportamento nativo do widget, sem acionar a gaveta).
            try:
                foco = self.focus_get()
                if foco is not None:
                    classe = type(foco).__name__.lower()
                    if "entry" in classe or "text" in classe or "spinbox" in classe or "combobox" in classe:
                        return None
            except Exception:
                pass
            self.abrir_gaveta()
        except Exception as e:
            registrar_log(None, "PDV Gaveta", "Falha", f"Erro no atalho <End>: {e}")
            return None
        return "break"


if __name__ == "__main__":
    app = ctk.CTk()

    def abrir_pdv():
        ModuloPDV()

    ctk.CTkButton(app, text="Entrar no PDV", command=abrir_pdv).pack(pady=50, padx=50)
    app.mainloop()
