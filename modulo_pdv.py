import json
import os
import queue
import sqlite3
import ctypes
import shutil
from ctypes import wintypes
from datetime import datetime
from tkinter import ttk, messagebox, filedialog

import customtkinter as ctk
from PIL import Image

import modulo_financeiro
from calculadora_tributaria import CalculadoraTributaria
from database_manager import get_db_connection, obter_caminho_dados, registrar_log
from modulo_config import carregar_configuracoes, obter_limite_sangria_preventiva
from modulo_fiscal import ModuloExportacaoFiscal, FiscalManager
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero
from webhook_delivery import iniciar_servidor_webhook


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


class ModuloPDV(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Caixa PDV - Mercado FRS")
        self.geometry("1100x750")

        self.caixa_id = None
        self.fiscal = ModuloExportacaoFiscal()
        self.fiscal_manager = FiscalManager()
        self.config = carregar_configuracoes()
        self.itens_carrinho = []
        self.item_selecionado_idx = None
        self.multiplicador_atual = 1
        self.limite_caixa_atual = obter_limite_sangria_preventiva()
        self.excesso_caixa_atual = 0.0
        self.fila_pedidos_delivery = queue.Queue()
        self.calculadora_tributaria = CalculadoraTributaria()
        self.modal_abertura = None
        self._id_after_verificacao_caixa = None
        self.forma_pagamento_selecionada = "DINHEIRO"
        self.clientes_orcamento_map = {}
        self._cache_xml_por_ean = {}

        if master is not None and not getattr(master, "usuario_atual", None):
            self.destroy()
            return

        self.bind("<F1>", lambda e: self.selecionar_forma_pagamento("DINHEIRO"))
        self.bind("<F2>", lambda e: self.selecionar_forma_pagamento("PIX"))
        self.bind("<F3>", lambda e: self.selecionar_forma_pagamento("DEBITO"))
        self.bind("<F4>", lambda e: self.selecionar_forma_pagamento("CREDITO"))
        self.bind("<F12>", lambda e: self.finalizar_venda_com_confirmacoes())

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
                self._safe_focus(self.ent_quantidade)
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

        primeira_entry = next(iter(entries_contagem.values()), None)

        def confirmar_abertura():
            total_inicial = 0.0
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
                    total_inicial += float(valor) * qtd

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
                self._set_status(f"Caixa aberto com {total_fmt}", "#2ecc71")
                registrar_log(None, "Abertura de Caixa", "Sucesso", f"Caixa {self.caixa_id} aberto com {total_fmt}")
                self._safe_focus(self.ent_quantidade)
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
            # Não permite seguir no PDV sem abertura de caixa.
            self._set_status("Abertura de caixa é obrigatória para operar o PDV.", "#ff6666")
            _focar_modal_abertura()

        self.modal_abertura.protocol("WM_DELETE_WINDOW", _on_close_modal_abertura)
        self.after(10, _focar_modal_abertura)

    def configurar_interface_pdv(self):
        for widget in self.winfo_children():
            widget.destroy()

        self.main_pdv = ctk.CTkFrame(self, fg_color="black", corner_radius=0)
        self.main_pdv.pack(fill="both", expand=True)

        self.top_bar = ctk.CTkFrame(self.main_pdv, height=50, fg_color="#1a1a1a", corner_radius=0)
        self.top_bar.pack(side="top", fill="x")
        ctk.CTkLabel(self.top_bar, text="CAIXA LIVRE", font=("Roboto", 16, "bold"), text_color="#2ecc71").pack(side="left", padx=20)

        self.centro_container = ctk.CTkFrame(self.main_pdv, fg_color="black")
        self.centro_container.pack(fill="both", expand=True, padx=10, pady=10)

        self.grid_container = ctk.CTkFrame(self.centro_container, fg_color="#121212")
        self.grid_container.pack(side="left", fill="both", expand=True)

        self.input_topo = ctk.CTkFrame(self.grid_container, fg_color="#1a1a1a", height=70)
        self.input_topo.pack(fill="x", padx=8, pady=(8, 6))

        self.ent_quantidade = ctk.CTkEntry(self.input_topo, width=140, height=50, font=("Roboto", 20, "bold"), placeholder_text="Qtd")
        self.ent_quantidade.pack(side="left", padx=(10, 8), pady=10)
        self.ent_quantidade.bind("<Return>", lambda _e: self._safe_focus(self.ent_cod_barras))
        self.ent_quantidade.bind("<Tab>", self._focar_produto_pelo_tab)
        aplicar_padrao_entrada_numerica(self.ent_quantidade, inteiro=True)

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

        ctk.CTkLabel(self.scroll_operacoes, text="OPERACOES", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=10)

        ctk.CTkButton(self.scroll_operacoes, text="SANGRIA", fg_color="#c0392b", command=self.modal_sangria).pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(
            self.scroll_operacoes,
            text="SUPRIMENTO",
            fg_color="#2980b9",
            command=self.modal_suprimento,
        ).pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(self.scroll_operacoes, text="CANCELAR ITEM", fg_color="#d35400", command=self.cancelar_item).pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(self.scroll_operacoes, text="CLIENTE (ORCAMENTO)", font=("Roboto", 11, "bold"), text_color="gray").pack(pady=(10, 2))
        self.combo_cliente_orcamento = ctk.CTkOptionMenu(self.scroll_operacoes, values=["Sem clientes cadastrados"], width=180)
        self.combo_cliente_orcamento.pack(padx=10, pady=(0, 8))
        self._carregar_clientes_orcamento()

        ctk.CTkButton(
            self.scroll_operacoes,
            text="SALVAR ORÇAMENTO",
            fg_color="#1565c0",
            command=self.salvar_orcamento_atual,
        ).pack(fill="x", padx=10, pady=4)
        ctk.CTkButton(
            self.scroll_operacoes,
            text="ABRIR ORÇAMENTOS",
            fg_color="#5d4037",
            command=self.abrir_tela_orcamentos,
        ).pack(fill="x", padx=10, pady=4)

        ctk.CTkLabel(self.scroll_operacoes, text="VALOR PAGO", font=("Roboto", 11, "bold"), text_color="gray").pack(pady=(8, 2))
        self.ent_valor_pago = ctk.CTkEntry(self.scroll_operacoes, width=180, placeholder_text="0,00")
        self.ent_valor_pago.pack(padx=10, pady=(0, 8))
        self.ent_valor_pago.bind("<KeyRelease>", lambda _e: self.atualizar_troco_display())
        aplicar_padrao_entrada_numerica(self.ent_valor_pago, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(self.scroll_operacoes, text="PAGAMENTO RAPIDO", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=(12, 10))
        ctk.CTkButton(self.scroll_operacoes, text="DINHEIRO (F1)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("DINHEIRO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="PIX (F2)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("PIX")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="CARTAO DEBITO (F3)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("DEBITO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.scroll_operacoes, text="CARTAO CREDITO (F4)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("CREDITO")).pack(fill="x", padx=10, pady=2)

        ctk.CTkButton(
            self.scroll_operacoes,
            text="FINALIZAR VENDA",
            fg_color="#27ae60",
            height=60,
            font=("Roboto", 14, "bold"),
            command=self.finalizar_venda_com_confirmacoes,
        ).pack(fill="x", padx=10, pady=(16, 8))

        ctk.CTkButton(self.scroll_operacoes, text="VOLTAR AO MENU", fg_color="#4a4a4a", command=self.voltar_ao_menu).pack(fill="x", padx=10, pady=(0, 12))

        self.footer = ctk.CTkFrame(self.main_pdv, height=110, fg_color="#1a1a1a", corner_radius=0)
        self.footer.pack(side="bottom", fill="x")

        self.footer_content = ctk.CTkFrame(self.footer, fg_color="transparent")
        self.footer_content.pack(expand=True, pady=6)

        self.troco_frame = ctk.CTkFrame(self.footer_content, fg_color="transparent")
        self.troco_frame.pack(side="left", padx=(0, 42))

        self.lbl_troco_venda = ctk.CTkLabel(
            self.troco_frame,
            text="TROCO R$ 0,00",
            font=("Roboto", 36, "bold"),
            text_color="#e74c3c",
        )
        self.lbl_troco_venda.pack(anchor="w", pady=(18, 8))

        self.total_frame = ctk.CTkFrame(self.footer_content, fg_color="transparent")
        self.total_frame.pack(side="left")

        ctk.CTkLabel(self.total_frame, text="TOTAL DA VENDA", font=("Roboto", 12, "bold"), text_color="gray").pack(anchor="e")
        self.lbl_total_venda = ctk.CTkLabel(self.total_frame, text="R$ 0,00", font=("Roboto", 64, "bold"), text_color="#d8ff3f")
        self.lbl_total_venda.pack(anchor="e")

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

        self._safe_focus(self.ent_quantidade)
        self._safe_after(300, self._processar_fila_delivery)

    def _focar_produto_pelo_tab(self, _event=None):
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

    def _carregar_clientes_orcamento(self):
        try:
            with get_db_connection() as conn:
                clientes = conn.execute(
                    "SELECT id, nome FROM clientes ORDER BY nome COLLATE NOCASE ASC"
                ).fetchall()
        except Exception:
            clientes = []

        self.clientes_orcamento_map = {}
        labels = []
        for cliente_id, nome in clientes:
            label = f"{cliente_id} - {nome}"
            self.clientes_orcamento_map[label] = int(cliente_id)
            labels.append(label)

        if not labels:
            labels = ["Sem clientes cadastrados"]

        self.combo_cliente_orcamento.configure(values=labels)
        self.combo_cliente_orcamento.set(labels[0])

    def salvar_orcamento_atual(self):
        if not self.itens_carrinho:
            self._set_status("Adicione itens antes de salvar orçamento.", "#ff6666")
            return

        cliente_label = self.combo_cliente_orcamento.get() if hasattr(self, "combo_cliente_orcamento") else ""
        cliente_id = self.clientes_orcamento_map.get(cliente_label)
        if not cliente_id:
            self._set_status("Selecione um cliente válido para o orçamento.", "#ff6666")
            return

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

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO orcamentos (cliente_id, status, valor_total, valor_impostos_retidos, valor_liquido)
                    VALUES (?, 'ORCAMENTO', ?, ?, ?)
                    """,
                    (cliente_id, valor_total, valor_impostos, valor_liquido),
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
                            quantidade, valor_unitario, subtotal
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            orcamento_id,
                            produto_id,
                            str(item.get("barcode", "") or ""),
                            str(item.get("nome", "Item")),
                            str(item.get("ncm", "") or ""),
                            int(item.get("quantidade", 0) or 0),
                            float(item.get("preco", 0.0) or 0.0),
                            float(item.get("total", 0.0) or 0.0),
                        ),
                    )

            self._set_status(
                f"Orçamento #{orcamento_id} salvo | Total: {self._formatar_moeda_br(valor_total)}",
                "#4aa3ff",
            )
            registrar_log(None, "PDV Orçamento", "Sucesso", f"Orçamento {orcamento_id} salvo para cliente {cliente_id}")
            self.itens_carrinho = []
            self._renderizar_carrinho()
            self.atualizar_total_display()
            self.ent_valor_pago.delete(0, "end")
            self.lbl_troco_venda.configure(text="TROCO R$ 0,00")
        except Exception as e:
            self._set_status(f"Falha ao salvar orçamento: {e}", "#ff6666")
            registrar_log(None, "PDV Orçamento", "Falha", f"Erro ao salvar orçamento: {e}")

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
        entrada = self.ent_cod_barras.get().strip()
        self.ent_cod_barras.delete(0, "end")
        if not entrada:
            return

        qtd_digitada = 1
        qtd_texto = self.ent_quantidade.get().strip() if hasattr(self, "ent_quantidade") else ""
        if qtd_texto:
            try:
                qtd_digitada = parse_numero(qtd_texto, "Quantidade", inteiro=True, minimo=1)
            except ValueError:
                self._set_status("Quantidade inválida. Informe número inteiro.", "#ff6666")
                return
        elif entrada.isdigit() and len(entrada) <= 3:
            self.multiplicador_atual = int(entrada)
            self.ent_cod_barras.configure(placeholder_text=f"Qtd: {self.multiplicador_atual} x ...")
            self._set_status(f"Quantidade definida: {self.multiplicador_atual}", "#f1c40f")
            return

        qtd_item = qtd_digitada if qtd_texto else self.multiplicador_atual

        if entrada.isdigit():
            produto = self.buscar_produto_por_ean(entrada)
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
        self.processar_entrada_produto()
        return "break"

    def _adicionar_item_produto(self, produto, qtd_item):
        try:
            preco_unitario = parse_numero(produto[3], "Preço", permitir_vazio=True, default=0.0, minimo=0)
        except ValueError:
            self._set_status(f"Preço inválido para o produto {produto[2]}.", "#ff6666")
            return

        item = {
            "id": produto[0],
            "barcode": produto[1] or "",
            "nome": produto[2],
            "preco": preco_unitario,
            "quantidade": qtd_item,
            "total": preco_unitario * qtd_item,
            "ncm": produto[7] if len(produto) > 7 else "",
            "origem": "BALCAO",
        }
        self.itens_carrinho.append(item)
        self._renderizar_carrinho()
        self.atualizar_total_display()
        self._set_status(f"Item adicionado: {item['nome']}", "#2ecc71")

        self.multiplicador_atual = 1
        self.ent_cod_barras.configure(placeholder_text="Código ou Nome do Produto (Enter para adicionar)")
        self.ent_quantidade.delete(0, "end")
        self._safe_focus(self.ent_quantidade)

    def _abrir_modal_selecao_produtos(self, termo, produtos, qtd_item):
        modal = ctk.CTkToplevel(self)
        modal.title("Selecionar Produto")
        modal.geometry("780x520")
        modal.grab_set()

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
            self._safe_focus(self.ent_quantidade)
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
            self._safe_focus(self.ent_quantidade)

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
        arvore.focus_set()

    def _renderizar_carrinho(self):
        for widget in self.scroll_vendas.winfo_children():
            widget.destroy()

        self.item_selecionado_idx = None
        for idx, item in enumerate(self.itens_carrinho):
            self._adicionar_linha_grid(item, idx)

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

        widgets = [
            ctk.CTkLabel(row, text=str(item["id"]), width=130),
            ctk.CTkLabel(row, text=f"[{origem}] {item['nome']}", width=540, anchor="w"),
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
        valor_pago = self._ler_valor_pago_digitado()

        if valor_pago is None or valor_pago <= total:
            troco = 0.0
        else:
            troco = valor_pago - total

        self.lbl_troco_venda.configure(text=f"TROCO {self._formatar_moeda_br(troco)}")

    def buscar_produto_venda(self, codigo_barras):
        hoje = datetime.now().strftime("%Y-%m-%d")
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao, ncm
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

    def buscar_imagem_produto(self, ean):
        ean_txt = str(ean or "").strip()
        if not ean_txt:
            return None

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

        pasta_destino = os.path.join(os.getcwd(), "assets", "produtos")
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
        entry_qtd.insert(0, "0")
        aplicar_padrao_entrada_numerica(entry_qtd, inteiro=True)

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

                ncm = entry_ncm.get().strip()
                imagem_final = self._salvar_imagem_produto_por_ean(img_state.get("path", ""), ean_salvar)

                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO produtos (
                            codigo_barras, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda,
                            quantidade_atual, quantidade_minima, validade, imagem_path
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ean_salvar,
                            nome,
                            "UN",
                            ncm,
                            float(preco_custo),
                            float(margem),
                            float(preco_venda),
                            int(qtd),
                            0,
                            "",
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
                messagebox.showwarning("Cadastro", "Este EAN já está cadastrado no sistema.")
            except Exception as e:
                messagebox.showerror("Cadastro", f"Falha ao cadastrar produto: {e}")

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
            messagebox.showwarning("Produto não cadastrado", f"EAN {ean} não cadastrado. Abra o cadastro rápido.")
        except Exception:
            pass
        self._abrir_modal_cadastro_rapido_produto(ean)

    def buscar_produtos_por_nome(self, termo):
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao, ncm
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
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao, ncm
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
        self.forma_pagamento_selecionada = forma_pgto
        self._set_status(f"Forma de pagamento selecionada: {forma_pgto}", "#4aa3ff")

    def _validar_e_obter_valor_pagamento(self, forma_pgto):
        if not self.itens_carrinho:
            self._set_status("Adicione itens antes de processar pagamento.", "#ff6666")
            return None

        valor_total = sum(i["quantidade"] * i["preco"] for i in self.itens_carrinho)
        valor_pago = valor_total
        valor_pago_lido = self._ler_valor_pago_digitado()
        if valor_pago_lido is None:
            self._set_status("Valor pago inválido.", "#ff6666")
            return None
        if valor_pago_lido > 0:
            valor_pago = valor_pago_lido

        if forma_pgto == "DINHEIRO":
            if valor_pago < valor_total:
                self._set_status("Valor pago menor que o total da venda.", "#ff6666")
                return None

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

        imprimir = messagebox.askyesno("Finalizar Venda", "Deseja imprimir o cupom? (Sim/Não)")

        self.finalizar_venda_pdv(forma_pgto, valor_pago, imprimir_cupom=imprimir)

    def iniciar_pagamento(self, modo):
        self.processar_pagamento(modo)

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

    def _formatar_linha_item_cupom(self, nome, qtd, total, largura):
        col_qtd = 4
        col_total = 10
        col_nome = max(8, largura - (col_qtd + col_total + 2))

        linhas_nome = self._split_texto_largura(nome, col_nome)
        primeira_linha = f"{linhas_nome[0]:<{col_nome}} {int(qtd):>{col_qtd}} {float(total):>{col_total}.2f}"
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

    def abrir_gaveta(self):
        """Aciona pulso de abertura de gaveta via ESC/POS na impressora padrão."""
        comando_gaveta = b"\x1bp\x00\x19\xfa"
        self._enviar_raw_impressora_padrao(comando_gaveta)

    def imprimir_cupom(self, dados_venda):
        """Imprime cupom não fiscal em impressora térmica (58mm/80mm) via ESC/POS."""
        itens = list(dados_venda.get("itens") or [])
        total = float(dados_venda.get("total") or 0.0)
        forma_pgto = str(dados_venda.get("forma_pagamento") or "N/A")
        if not itens:
            raise ValueError("Cupom não impresso: venda sem itens.")

        largura = self._largura_cupom_chars()
        separador = "-" * largura
        nome_estabelecimento = str(
            self.config.get("nome_estabelecimento")
            or self.config.get("razao_social")
            or "MERCADO FRS"
        ).strip().upper()
        rodape = str(self.config.get("mensagem_rodape_cupom", "OBRIGADO PELA PREFERENCIA!")).strip()

        linhas = [
            "CUPOM NAO FISCAL".center(largura),
            datetime.now().strftime("%d/%m/%Y %H:%M:%S").center(largura),
            separador,
            "ITEM".ljust(largura - 15) + "QTD".rjust(4) + "TOTAL".rjust(11),
            separador,
        ]

        for item in itens:
            nome = item.get("nome", "ITEM")
            qtd = item.get("quantidade", 1)
            total_item = item.get("total", 0.0)
            for linha in self._formatar_linha_item_cupom(nome, qtd, total_item, largura):
                linhas.append(linha)

        linhas.extend(
            [
                separador,
                f"FORMA PGTO: {forma_pgto}",
                f"TOTAL: {self._formatar_moeda_br(total)}",
                separador,
            ]
        )

        corpo_principal = "\n".join(linhas).encode("cp850", errors="replace")
        # Cabeçalho em destaque: centralizado, negrito e tamanho ampliado.
        cabecalho_destaque = (
            b"\x1ba\x01" +          # ESC a 1 -> alinhamento central
            b"\x1d!\x11" +          # GS ! 0x11 -> largura/altura dobradas
            b"\x1bE\x01" +          # ESC E 1 -> negrito on
            f"{nome_estabelecimento}\n".encode("cp850", errors="replace") +
            b"\x1bE\x00" +          # ESC E 0 -> negrito off
            b"\x1d!\x00" +          # GS ! 0x00 -> tamanho normal
            b"\x1ba\x00"            # ESC a 0 -> alinhamento à esquerda
        )

        rodape_principal = f"{rodape.center(largura)}\n".encode("cp850", errors="replace")
        # Rodapé discreto: centralizado com fonte B (menor).
        assinatura_rodape = (
            b"\x1ba\x01" +
            b"\x1bM\x01" +          # ESC M 1 -> fonte B (menor)
            b"Desenvolvido por FRS Mercado\n" +
            b"\x1bM\x00" +
            b"\x1ba\x00"
        )

        comando_inicial = b"\x1b@\x1ba\x00"
        comando_final = b"\n\n\n\x1dV\x00"
        payload = comando_inicial + cabecalho_destaque + corpo_principal + b"\n" + rodape_principal + assinatura_rodape + comando_final

        self._enviar_raw_impressora_padrao(payload)
        registrar_log(None, "PDV Impressão", "Sucesso", "Cupom não fiscal enviado para impressora térmica.")

    def _executar_automacao_pos_venda(self, dados_cupom):
        """Dispara impressão e abertura da gaveta em sequência obrigatória pós-venda."""
        erro_impressao = None
        erro_gaveta = None

        try:
            self.imprimir_cupom(dados_cupom)
        except Exception as e:
            registrar_log(None, "PDV Impressão", "Falha", f"Erro impressão cupom: {e}")
            erro_impressao = e

        try:
            self.abrir_gaveta()
            registrar_log(None, "PDV Gaveta", "Sucesso", "Comando de abertura de gaveta enviado.")
        except Exception as e:
            registrar_log(None, "PDV Gaveta", "Falha", f"Erro ao abrir gaveta: {e}")
            erro_gaveta = e

        if erro_impressao is None and erro_gaveta is None:
            self._set_status("Cupom não fiscal impresso e gaveta acionada.", "#2ecc71")
            return

        if erro_impressao is not None and erro_gaveta is not None:
            self._set_status(f"Falha na automação pós-venda: impressão e gaveta. ({erro_impressao})", "#ff6666")
            return

        if erro_impressao is not None:
            self._set_status(f"Cupom não impresso: {erro_impressao}. Gaveta acionada.", "#ff6666")
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
                    messagebox.showerror("Erro Fiscal", f"Erro na emissão: {mensagem_erro}")
                except Exception:
                    pass
                registrar_log(None, "PDV Fiscal", "Falha", f"Erro na emissão: {mensagem_erro}")
                return False

            registrar_log(None, "PDV Fiscal", "Sucesso", f"Comando NFC-e enviado. Retorno: {resposta[:200]}")
            return True
        except Exception as e:
            self._set_status(f"Erro na emissão: {e}", "#ff6666")
            try:
                messagebox.showerror("Erro Fiscal", f"Erro na emissão: {e}")
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
        origem_venda="LOJA_FISICA",
        status_pedido="APROVADO",
        status_pagamento="PAGO",
    ):
        if not self.itens_carrinho:
            return

        valor_bruto = sum(i["quantidade"] * i["preco"] for i in self.itens_carrinho)
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
                cursor.execute(
                    """
                    INSERT INTO financeiro (valor, tipo, valor_bruto, valor_impostos_retidos, taxa_aplicada, descricao)
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
                    try:
                        produto_id = int(item.get("id"))
                        quantidade_vendida = int(item.get("quantidade", 0))
                    except (TypeError, ValueError):
                        continue

                    if quantidade_vendida <= 0:
                        continue

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
        except Exception as e:
            self._set_status(f"Falha ao registrar venda: {e}", "#ff6666")
            registrar_log(None, "PDV", "Falha", f"Erro ao registrar venda: {e}")
            return

        sucesso = True
        if self._fiscal_habilitado():
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
                }
            )

        if sucesso:
            recebido_txt = f" | Recebido: {self._formatar_moeda_br(valor_pago)}" if valor_pago is not None else ""
            self._set_status(
                f"Venda {forma_pgto} concluída | Bruto: {self._formatar_moeda_br(valor_bruto)} | Impostos: {self._formatar_moeda_br(valor_impostos)} | Líquido: {self._formatar_moeda_br(valor_liquido)}{recebido_txt}",
                "#2ecc71",
            )
            registrar_log(None, "PDV", "Sucesso", f"Venda {venda_id} ({forma_pgto}) exportada.")
        else:
            self._set_status("Venda registrada sem exportação fiscal confirmada.", "#f1c40f")

        self.itens_carrinho = []
        self._renderizar_carrinho()
        self.atualizar_total_display()
        self.ent_valor_pago.delete(0, "end")
        self.lbl_troco_venda.configure(text="TROCO R$ 0,00")
        self._avaliar_limite_caixa()
        self._safe_focus(self.ent_quantidade)

    def _retornar_foco_pdv(self):
        self._safe_after(30, lambda: self._safe_focus(self.ent_quantidade))

    def _to_float(self, texto):
        return parse_numero(texto, "Valor", minimo=0)

    def _obter_dinheiro_atual_caixa(self):
        if not self.caixa_id:
            return 0.0

        with get_db_connection() as conn:
            saldo_row = conn.execute("SELECT saldo_inicial FROM caixa_operacao WHERE id = ?", (self.caixa_id,)).fetchone()
            saldo_inicial = float(saldo_row[0] or 0.0) if saldo_row else 0.0

            vendas_dinheiro_row = conn.execute(
                """
                SELECT SUM(
                    CASE
                        WHEN COALESCE(valor_liquido, 0) = 0 AND COALESCE(valor_impostos_retidos, 0) = 0
                            THEN valor_total
                        ELSE valor_liquido
                    END
                )
                FROM vendas_dia
                WHERE forma_pagamento = 'DINHEIRO'
                """
            ).fetchone()
            vendas_dinheiro = float(vendas_dinheiro_row[0] or 0.0) if vendas_dinheiro_row else 0.0

            try:
                sangria_row = conn.execute(
                    "SELECT SUM(valor) FROM sangrias WHERE caixa_operacao_id = ?",
                    (self.caixa_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                sangria_row = conn.execute("SELECT SUM(valor) FROM sangrias").fetchone()
            total_sangrias = float(sangria_row[0] or 0.0) if sangria_row else 0.0

            suprimento_row = conn.execute(
                "SELECT SUM(valor) FROM financeiro WHERE tipo = 'Entrada' AND descricao LIKE 'Suprimento:%'"
            ).fetchone()
            total_suprimentos = float(suprimento_row[0] or 0.0) if suprimento_row else 0.0

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
                    if tipo == "SANGRIA":
                        descricao_fin = f"Sangria: {obs}"
                        if obs == "Sangria Preventiva - Excesso de Caixa":
                            descricao_fin = obs
                        cursor.execute(
                            "INSERT INTO sangrias (valor, justificativa, caixa_operacao_id) VALUES (?, ?, ?)",
                            (valor, obs, self.caixa_id),
                        )
                        cursor.execute(
                            "INSERT INTO financeiro (valor, tipo, descricao) VALUES (?, ?, ?)",
                            (valor, "Saída", descricao_fin),
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

    def processar_fechamento_inteligente(self):
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT saldo_inicial FROM caixa_operacao WHERE id = ?", (self.caixa_id,))
                saldo_inicial = cursor.fetchone()[0]
                total_vendas = modulo_financeiro.obter_total_vendas_dia()
                try:
                    cursor.execute(
                        "SELECT SUM(valor) FROM sangrias WHERE data_sangria >= (SELECT data_abertura FROM caixa_operacao WHERE id = ?) AND caixa_operacao_id = ?",
                        (self.caixa_id, self.caixa_id),
                    )
                except sqlite3.OperationalError:
                    cursor.execute(
                        "SELECT SUM(valor) FROM sangrias WHERE data_sangria >= (SELECT data_abertura FROM caixa_operacao WHERE id = ?)",
                        (self.caixa_id,),
                    )
                total_sangrias = cursor.fetchone()[0] or 0.0

                valor_esperado = (saldo_inicial + total_vendas) - total_sangrias

            with get_db_connection() as conn_fechamento:
                conn_fechamento.execute(
                    "UPDATE caixa_operacao SET status = 'FECHADO', data_fechamento = CURRENT_TIMESTAMP WHERE id = ?",
                    (self.caixa_id,),
                )

            sucesso, msg_fechamento = modulo_financeiro.fechar_caixa()
            if sucesso:
                self._set_status("Caixa fechado com sucesso.", "#2ecc71")
                registrar_log(
                    None,
                    "Fechamento de Caixa",
                    "Sucesso",
                    f"Caixa {self.caixa_id} fechado. Valor esperado: {self._formatar_moeda_br(valor_esperado)}",
                )
            else:
                self._set_status(msg_fechamento, "#ff6666")
                registrar_log(None, "Fechamento de Caixa", "Falha", msg_fechamento)
        except Exception as e:
            self._set_status(f"Falha no fechamento: {e}", "#ff6666")
            registrar_log(None, "Fechamento de Caixa", "Falha", f"Erro: {e}")

    def voltar_ao_menu(self):
        try:
            if self.modal_abertura is not None and self.modal_abertura.winfo_exists():
                self.modal_abertura.grab_release()
                self.modal_abertura.destroy()
        except Exception:
            pass
        self.modal_abertura = None

        try:
            if self._id_after_verificacao_caixa is not None:
                self.after_cancel(self._id_after_verificacao_caixa)
        except Exception:
            pass
        self._id_after_verificacao_caixa = None

        self.destroy()


if __name__ == "__main__":
    app = ctk.CTk()

    def abrir_pdv():
        ModuloPDV()

    ctk.CTkButton(app, text="Entrar no PDV", command=abrir_pdv).pack(pady=50, padx=50)
    app.mainloop()
