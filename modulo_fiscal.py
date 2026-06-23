import os
import json
import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from modulo_config import carregar_configuracoes
from database_manager import obter_caminho_dados

class ModuloExportacaoFiscal:
    def __init__(self):
        self.config = carregar_configuracoes()
        # Pastas sempre em APPDATA para evitar WinError 5 em Program Files.
        self.pasta_exportacao = self.config.get("pasta_exportacao_fiscal") or obter_caminho_dados("exportacao_fiscal")
        self.pasta_entrada_integrador = self.config.get("pasta_entrada_fiscal") or obter_caminho_dados("fiscal_in")
        self.pasta_retorno_integrador = self.config.get("pasta_retorno_fiscal") or obter_caminho_dados("fiscal_out")
        
        if not os.path.exists(self.pasta_exportacao):
            os.makedirs(self.pasta_exportacao, exist_ok=True)
        
        for p in [self.pasta_entrada_integrador, self.pasta_retorno_integrador]:
            if not os.path.exists(p): os.makedirs(p, exist_ok=True)

    def monitorar_retorno(self, venda_id, callback_status):
        """Inicia uma thread para monitorar o retorno de uma venda específica."""
        def check():
            # Tempo máximo de espera: 60 segundos
            tentativas = 0
            while tentativas < 120: 
                # Padrão de arquivo de retorno (ex: retorno_venda_123.json)
                arquivo_retorno = os.path.join(self.pasta_retorno_integrador, f"retorno_venda_{venda_id}.json")
                
                if os.path.exists(arquivo_retorno):
                    try:
                        with open(arquivo_retorno, 'r', encoding='utf-8') as f:
                            dados = json.load(f)
                            status = dados.get("status", "ERRO")
                            motivo = dados.get("motivo", "Erro desconhecido")
                            try:
                                callback_status(status, motivo)
                            except Exception as cb_err:
                                print(f"Erro no callback fiscal: {cb_err}")
                            # Remove o arquivo após processar para não poluir a pasta
                            os.remove(arquivo_retorno)
                            return
                    except Exception as e:
                        try:
                            callback_status("ERRO", f"Erro leitura: {e}")
                        except Exception as cb_err:
                            print(f"Erro no callback fiscal: {cb_err}")
                        return
                
                time.sleep(0.5)
                tentativas += 1
            
            try:
                callback_status("TIMEOUT", "O integrador fiscal não respondeu a tempo.")
            except Exception as cb_err:
                print(f"Erro no callback fiscal: {cb_err}")

        thread = threading.Thread(target=check, daemon=True)
        thread.start()

    def exportar_venda(self, venda_id, itens, forma_pagamento, valor_total, dados_cliente="Consumidor Final"):
        """
        Gera um XML simplificado apenas com os dados brutos da venda.
        Livre de assinaturas digitais ou vínculos com hardware fiscal legado.
        """
        root = ET.Element("VendaExportacao")
        ET.SubElement(root, "ID_Venda").text = str(venda_id)
        ET.SubElement(root, "DataHora").text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ET.SubElement(root, "Cliente").text = dados_cliente
        
        # Cabeçalho do Mercado (Emitente)
        emit = ET.SubElement(root, "Emitente")
        ET.SubElement(emit, "RazaoSocial").text = self.config.get("razao_social", "MERCADO FRS")
        ET.SubElement(emit, "CNPJ").text = self.config.get("cnpj", "00.000.000/0000-00")

        # Lista de Produtos
        prod_list = ET.SubElement(root, "Produtos")
        for i, item in enumerate(itens):
            p = ET.SubElement(prod_list, "Item", nItem=str(i + 1))
            ET.SubElement(p, "Descricao").text = item.get('nome')
            ET.SubElement(p, "Qtd").text = str(item.get('quantidade'))
            ET.SubElement(p, "PrecoUn").text = f"{item.get('preco'):.2f}"
            ET.SubElement(p, "Subtotal").text = f"{(item.get('quantidade') * item.get('preco')):.2f}"

        # Totais e Pagamento
        fin = ET.SubElement(root, "Financeiro")
        ET.SubElement(fin, "TotalGeral").text = f"{valor_total:.2f}"
        ET.SubElement(fin, "FormaPagamento").text = forma_pagamento

        # Gravação do arquivo
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_arquivo = f"export_venda_{venda_id}_{timestamp}.xml"
        caminho_final = os.path.join(self.pasta_exportacao, nome_arquivo)
        
        tree = ET.ElementTree(root)
        tree.write(caminho_final, encoding="utf-8", xml_declaration=True)
        
        return True, caminho_final