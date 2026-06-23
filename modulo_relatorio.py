import customtkinter as ctk
from tkinter import messagebox
import sqlite3
from datetime import datetime
import os
import shutil
from pathlib import Path

# ReportLab para PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Google Drive API
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

from database_manager import (
    get_db_connection,
    get_db_path,
    registrar_log,
    GOOGLE_CREDS,
    obter_caminho_dados,
)
from modulo_config import carregar_configuracoes

class ModuloRelatorio(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("BI & Relatórios Estratégicos")
        self.geometry("1100x750")
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar relatórios.")
            self.destroy()
            return

        # Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # --- Filtros Temporais ---
        self.frame_filtros = ctk.CTkFrame(self)
        self.frame_filtros.grid(row=0, column=0, padx=20, pady=20, sticky="ew")

        ctk.CTkLabel(self.frame_filtros, text="Data Inicial (AAAA-MM-DD):").pack(side="left", padx=10)
        self.data_ini = ctk.CTkEntry(self.frame_filtros, width=120)
        self.data_ini.insert(0, datetime.now().strftime('%Y-%m-01'))
        self.data_ini.pack(side="left", padx=5)

        ctk.CTkLabel(self.frame_filtros, text="Data Final (AAAA-MM-DD):").pack(side="left", padx=10)
        self.data_fim = ctk.CTkEntry(self.frame_filtros, width=120)
        self.data_fim.insert(0, datetime.now().strftime('%Y-%m-%d'))
        self.data_fim.pack(side="left", padx=5)

        self.btn_filtrar = ctk.CTkButton(self.frame_filtros, text="Atualizar BI", command=self.atualizar_dados)
        self.btn_filtrar.pack(side="left", padx=20)

        self.btn_pdf = ctk.CTkButton(self.frame_filtros, text="Exportar PDF & Drive", fg_color="#2c3e50", command=self.gerar_e_subir_pdf)
        self.btn_pdf.pack(side="right", padx=10)

        # --- Dashboard de Cartões ---
        self.frame_cards = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_cards.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.frame_cards.grid_columnconfigure((0, 1, 2), weight=1)

        self.card_vendas = self.criar_card(self.frame_cards, "Total Vendido", "R$ 0,00", "#27ae60", 0)
        self.card_despesas = self.criar_card(self.frame_cards, "Total Despesas", "R$ 0,00", "#c0392b", 1)
        self.card_lucro = self.criar_card(self.frame_cards, "Lucro Líquido", "R$ 0,00", "#2980b9", 2)

        # --- Visualização de Tabelas (Produtos) ---
        self.scroll_tabelas = ctk.CTkScrollableFrame(self)
        self.scroll_tabelas.grid(row=2, column=0, padx=20, pady=20, sticky="nsew")
        self.grid_rowconfigure(2, weight=2)

        self.atualizar_dados()

    def criar_card(self, master, titulo, valor, cor, col):
        f = ctk.CTkFrame(master, corner_radius=15, border_width=2, border_color=cor)
        f.grid(row=0, column=col, padx=10, pady=10, sticky="nsew")
        ctk.CTkLabel(f, text=titulo, font=("Arial", 14)).pack(pady=(15, 0))
        lbl_valor = ctk.CTkLabel(f, text=valor, font=("Arial", 28, "bold"), text_color=cor)
        lbl_valor.pack(pady=(5, 15))
        return lbl_valor

    def atualizar_dados(self):
        """Busca dados no banco e atualiza a interface."""
        ini = self.data_ini.get()
        fim = self.data_fim.get()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                
                # Financeiro
                cursor.execute("SELECT SUM(valor_total) FROM vendas WHERE data_venda BETWEEN ? AND ?", (ini, fim))
                vendas = cursor.fetchone()[0] or 0.0
                
                cursor.execute("SELECT SUM(valor) FROM financeiro WHERE tipo = 'Saída' AND data_registro BETWEEN ? AND ?", (ini, fim))
                despesas = cursor.fetchone()[0] or 0.0

                lucro = vendas - despesas

                self.card_vendas.configure(text=f"R$ {vendas:,.2f}")
                self.card_despesas.configure(text=f"R$ {despesas:,.2f}")
                self.card_lucro.configure(text=f"R$ {lucro:,.2f}")

        except Exception as e:
            messagebox.showerror("Erro BI", f"Erro ao processar dados: {e}")

    def gerar_e_subir_pdf(self):
        """Gera o PDF profissional e envia ao Drive."""
        ini, fim = self.data_ini.get(), self.data_fim.get()
        nome_arquivo = f"Relatorio_Vendas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        caminho_pdf = Path(obter_caminho_dados("relatorios", nome_arquivo)).resolve()

        try:
            doc = SimpleDocTemplate(str(caminho_pdf), pagesize=A4)
            styles = getSampleStyleSheet()
            elements = []

            # Cabeçalho
            config = carregar_configuracoes()
            elements.append(Paragraph(f"<b>{config.get('razao_social', 'MERCADO FRS')}</b>", styles['Title']))
            elements.append(Paragraph(f"Relatório de Gestão: {ini} até {fim}", styles['Normal']))
            elements.append(Spacer(1, 20))

            # Sessão Financeira
            elements.append(Paragraph("1. RESUMO FINANCEIRO", styles['Heading2']))
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT SUM(valor_total) FROM vendas WHERE data_venda BETWEEN ? AND ?", (ini, fim))
                v_total = cursor.fetchone()[0] or 0.0
                
                data_fin = [["Descrição", "Valor"], ["Total de Vendas", f"R$ {v_total:.2f}"]]
                t_fin = Table(data_fin, colWidths=[300, 150])
                t_fin.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.grey), ('TEXTCOLOR',(0,0),(-1,0),colors.whitesmoke)]))
                elements.append(t_fin)

            # Sessão Estoque Crítico
            elements.append(Spacer(1, 20))
            elements.append(Paragraph("2. PRODUTOS CRÍTICOS (Estoque Baixo)", styles['Heading2']))
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT nome, quantidade_atual FROM produtos WHERE quantidade_atual < 5")
                produtos = cursor.fetchall()
                data_prod = [["Produto", "Qtd Atual"]] + list(produtos)
                t_prod = Table(data_prod, colWidths=[350, 100])
                t_prod.setStyle(TableStyle([('GRID', (0,0), (-1,-1), 1, colors.black)]))
                elements.append(t_prod)

            doc.build(elements)
            
            # Upload para o Drive
            self.upload_para_drive(str(caminho_pdf), config.get("drive_backup_folder_id"))
            
            messagebox.showinfo("Sucesso", f"Relatório gerado e enviado ao Google Drive!\nArquivo: {nome_arquivo}")
            registrar_log(None, "Relatório BI", "Sucesso", f"PDF gerado e enviado ao Drive: {nome_arquivo}")

        except Exception as e:
            messagebox.showerror("Erro PDF/Drive", f"Falha na exportação: {e}")
            registrar_log(None, "Relatório BI", "Falha", str(e))

    @staticmethod
    def _obter_drive_service():
        """Autentica via OAuth2, reaproveitando token local para evitar novo login."""
        if not GOOGLE_CREDS["credentials"]:
            raise Exception("Credenciais do Google (credentials.json) não encontradas.")

        SCOPES = ['https://www.googleapis.com/auth/drive.file']
        creds = None
        token_path = obter_caminho_dados("token.pickle")
        
        # Gerenciamento de token para evitar login repetitivo
        if os.path.exists(token_path):
            with open(token_path, 'rb') as token:
                creds = pickle.load(token)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS["credentials"], SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)

        return build('drive', 'v3', credentials=creds)

    @staticmethod
    def upload_para_drive(file_path, folder_id):
        """Realiza o upload usando as credenciais OAuth2 configuradas."""
        service = ModuloRelatorio._obter_drive_service()
        
        file_metadata = {
            'name': os.path.basename(file_path),
            'parents': [folder_id] if folder_id else []
        }
        media = MediaFileUpload(file_path, mimetype='application/pdf')
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

    @staticmethod
    def provisionar_novo_cliente(email_cliente):
        """
        Provisiona estrutura inicial no Drive do cliente autenticado:
        - Solicita OAuth2 (se necessário)
        - Garante pasta raiz FRS_Solution
        - Cria/copia base inicial template e envia para a pasta
        """
        service = ModuloRelatorio._obter_drive_service()

        email_drive = ""
        try:
            about = service.about().get(fields="user(emailAddress)").execute()
            email_drive = str(about.get("user", {}).get("emailAddress", "")).strip()
        except Exception:
            email_drive = ""

        if email_drive and email_cliente and email_drive.lower() != str(email_cliente).strip().lower():
            registrar_log(
                None,
                "Provisionamento Drive",
                "Aviso",
                f"Email OAuth2 divergente. Esperado: {email_cliente}; autenticado: {email_drive}",
            )

        query_pasta = (
            "name = 'FRS_Solution' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        resposta = service.files().list(q=query_pasta, spaces='drive', fields='files(id,name)').execute()
        pastas = resposta.get("files", [])

        if pastas:
            pasta_id = pastas[0]["id"]
        else:
            metadata_pasta = {
                "name": "FRS_Solution",
                "mimeType": "application/vnd.google-apps.folder",
            }
            pasta_criada = service.files().create(body=metadata_pasta, fields='id').execute()
            pasta_id = pasta_criada["id"]

        # Garante base local existente e cria snapshot template para provisionamento.
        origem_db = Path(get_db_path())
        if not origem_db.exists():
            with get_db_connection():
                pass

        pasta_templates = Path(obter_caminho_dados("templates"))
        pasta_templates.mkdir(parents=True, exist_ok=True)
        template_db = pasta_templates / "base_inicial_template.db"
        shutil.copy2(str(origem_db), str(template_db))

        nome_arquivo = template_db.name
        query_arquivo = (
            f"name = '{nome_arquivo}' and "
            f"'{pasta_id}' in parents and trashed = false"
        )
        arquivos = service.files().list(q=query_arquivo, spaces='drive', fields='files(id,name)').execute().get("files", [])

        media = MediaFileUpload(str(template_db), mimetype='application/octet-stream', resumable=False)
        if arquivos:
            service.files().update(fileId=arquivos[0]["id"], media_body=media).execute()
            acao_arquivo = "atualizado"
        else:
            service.files().create(
                body={"name": nome_arquivo, "parents": [pasta_id]},
                media_body=media,
                fields='id',
            ).execute()
            acao_arquivo = "criado"

        registrar_log(
            None,
            "Provisionamento Drive",
            "Sucesso",
            f"Pasta FRS_Solution pronta e template {acao_arquivo}. Email OAuth: {email_drive or 'desconhecido'}",
        )

        return {
            "email_oauth": email_drive,
            "pasta_raiz_id": pasta_id,
            "arquivo_template": nome_arquivo,
            "status": "ok",
        }

if __name__ == "__main__":
    app = ctk.CTk()
    def abrir(): ModuloRelatorio()
    ctk.CTkButton(app, text="Abrir BI", command=abrir).pack(pady=50)
    app.mainloop()