import json
import os
import queue
import sqlite3
import ctypes
from ctypes import wintypes
from datetime import datetime
from tkinter import ttk, messagebox

import customtkinter as ctk
from PIL import Image

import modulo_financeiro
from database_manager import get_db_connection, obter_caminho_dados, registrar_log
from modulo_config import carregar_configuracoes, obter_limite_sangria_preventiva
from modulo_fiscal import ModuloExportacaoFiscal
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero
from webhook_delivery import iniciar_servidor_webhook


class ModuloPDV(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Caixa PDV - Mercado FRS")
        self.geometry("1100x750")

        self.caixa_id = None
        self.fiscal = ModuloExportacaoFiscal()
        self.config = carregar_configuracoes()
        self.itens_carrinho = []
        self.item_selecionado_idx = None
        self.multiplicador_atual = 1
        self.limite_caixa_atual = obter_limite_sangria_preventiva()
        self.excesso_caixa_atual = 0.0
        self.fila_pedidos_delivery = queue.Queue()
        self.modal_abertura = None
        self._id_after_verificacao_caixa = None
        self.forma_pagamento_selecionada = "DINHEIRO"

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
                cursor.execute("SELECT id FROM caixa_operacao WHERE status = 'ABERTO' ORDER BY id DESC LIMIT 1")
                res = cursor.fetchone()
        except Exception as e:
            registrar_log(None, "Verificação de Caixa", "Falha", f"Erro: {e}")
            self._set_status(f"Falha ao verificar caixa: {e}", "#ff6666")
            return

        if res:
            self.caixa_id = res[0]
            registrar_log(None, "Verificação de Caixa", "Sucesso", f"Caixa {res[0]} já aberto.")
            self._set_status(f"Caixa {res[0]} aberto. PDV pronto para operação.", "#2ecc71")
            self._safe_focus(self.ent_quantidade)
        else:
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

        ctk.CTkLabel(self.painel_lateral, text="OPERAÇÕES", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=10)

        ctk.CTkButton(self.painel_lateral, text="SANGRIA", fg_color="#c0392b", command=self.modal_sangria).pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(
            self.painel_lateral,
            text="SUPRIMENTO",
            fg_color="#2980b9",
            command=self.modal_suprimento,
        ).pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(self.painel_lateral, text="CANCELAR ITEM", fg_color="#d35400", command=self.cancelar_item).pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(self.painel_lateral, text="VALOR PAGO", font=("Roboto", 11, "bold"), text_color="gray").pack(pady=(8, 2))
        self.ent_valor_pago = ctk.CTkEntry(self.painel_lateral, width=180, placeholder_text="0,00")
        self.ent_valor_pago.pack(padx=10, pady=(0, 8))
        self.ent_valor_pago.bind("<KeyRelease>", lambda _e: self.atualizar_troco_display())
        aplicar_padrao_entrada_numerica(self.ent_valor_pago, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(self.painel_lateral, text="PAGAMENTO RÁPIDO", font=("Roboto", 12, "bold"), text_color="gray").pack(pady=(12, 10))
        ctk.CTkButton(self.painel_lateral, text="DINHEIRO (F1)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("DINHEIRO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.painel_lateral, text="PIX (F2)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("PIX")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.painel_lateral, text="CARTÃO DÉBITO (F3)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("DEBITO")).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(self.painel_lateral, text="CARTÃO CRÉDITO (F4)", fg_color="#2c3e50", command=lambda: self.selecionar_forma_pagamento("CREDITO")).pack(fill="x", padx=10, pady=2)

        ctk.CTkButton(
            self.painel_lateral,
            text="FINALIZAR VENDA",
            fg_color="#27ae60",
            height=60,
            font=("Roboto", 14, "bold"),
            command=self.finalizar_venda_com_confirmacoes,
        ).pack(side="bottom", fill="x", padx=10, pady=20)

        ctk.CTkButton(self.painel_lateral, text="VOLTAR AO MENU", fg_color="#4a4a4a", command=self.voltar_ao_menu).pack(side="bottom", fill="x", padx=10, pady=(0, 8))

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

    def _aplicar_pedido_delivery(self, payload):
        if not isinstance(payload, dict):
            self._set_status("Webhook delivery recebido em formato inválido.", "#ff6666")
            return

        itens = payload.get("itens")
        cliente = str(payload.get("cliente", "Cliente Delivery")).strip() or "Cliente Delivery"
        valor_total_informado = payload.get("valor", None)

        if not isinstance(itens, list) or not itens:
            self._set_status("Pedido delivery ignorado: sem itens.", "#ff6666")
            return

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
            produto = self.buscar_produto_venda(entrada)
            if not produto:
                produtos = self.buscar_produtos_para_selecao(entrada)
                if not produtos:
                    self._set_status(f"Produto {entrada} não encontrado.", "#ff6666")
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
        img_folders = [os.path.join(os.getcwd(), "assets", "produtos"), obter_caminho_dados("assets", "produtos")]
        caminho_img = None
        for pasta in img_folders:
            for ext in [".jpg", ".png", ".jpeg"]:
                p = os.path.join(pasta, f"{barcode}{ext}")
                if os.path.exists(p):
                    caminho_img = p
                    break
            if caminho_img:
                break

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
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao
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

    def buscar_produtos_por_nome(self, termo):
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao
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
                    SELECT id, codigo_barras, nome, preco_venda, preco_base, inicio_promocao, fim_promocao
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

    def finalizar_venda_pdv(self, forma_pgto, valor_pago=None, imprimir_cupom=False):
        if not self.itens_carrinho:
            return

        valor_bruto = sum(i["quantidade"] * i["preco"] for i in self.itens_carrinho)
        taxas = modulo_financeiro.obter_taxas()
        valor_liquido = valor_bruto
        taxa_aplicada = 0.0

        if forma_pgto in ["DEBITO", "CREDITO"]:
            taxa_aplicada = taxas.get(forma_pgto, 0.0)
            valor_liquido = round(valor_bruto - (valor_bruto * (taxa_aplicada / 100)), 2)

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO vendas_dia (valor_total, forma_pagamento) VALUES (?, ?)", (valor_bruto, forma_pgto))
                venda_id = cursor.lastrowid

                desc = f"Venda PDV #{venda_id} ({forma_pgto})"
                cursor.execute(
                    """
                    INSERT INTO financeiro (valor, tipo, valor_bruto, taxa_aplicada, descricao)
                    VALUES (?, 'Entrada', ?, ?, ?)
                    """,
                    (valor_liquido, valor_bruto, taxa_aplicada, desc),
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

        sucesso, _caminho = self.fiscal.exportar_venda(venda_id, self.itens_carrinho, forma_pgto, valor_bruto)

        dados_json = {"id": venda_id, "total": valor_bruto, "pagamento": forma_pgto, "itens": self.itens_carrinho}
        self.exportar_venda_fiscal(dados_json)
        if imprimir_cupom:
            self._executar_automacao_pos_venda(
                {
                    "id": venda_id,
                    "itens": self.itens_carrinho,
                    "total": valor_bruto,
                    "forma_pagamento": forma_pgto,
                }
            )

        if sucesso:
            recebido_txt = f" | Recebido: {self._formatar_moeda_br(valor_pago)}" if valor_pago is not None else ""
            self._set_status(
                f"Venda {forma_pgto} concluída | Bruto: {self._formatar_moeda_br(valor_bruto)} | Líquido: {self._formatar_moeda_br(valor_liquido)}{recebido_txt}",
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
                "SELECT SUM(valor_total) FROM vendas_dia WHERE forma_pagamento = 'DINHEIRO'"
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
