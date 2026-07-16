import os
import json
import time
import subprocess
import threading
from pathlib import Path
import xml.etree.ElementTree as ET
from datetime import datetime
import configparser
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


class FiscalManager:
    """
    Gerencia a comunicacao por arquivos com ACBrMonitor (ENTREGA.TXT/RETORNO.TXT).
    """

    def __init__(self, timeout_segundos=30, intervalo_poll=0.25):
        self.timeout_segundos = timeout_segundos
        self.intervalo_poll = intervalo_poll

        self.raiz_projeto = Path(__file__).resolve().parent
        self.pasta_instala = self.raiz_projeto / "instala"
        self.pasta_fiscal_in = self.raiz_projeto / "fiscal_in"
        self.pasta_fiscal_out = self.raiz_projeto / "fiscal_out"

        self.arquivo_entrega = self.pasta_fiscal_in / "ENTREGA.TXT"
        self.arquivo_retorno = self.pasta_fiscal_out / "RETORNO.TXT"
        self.arquivo_ini = self.pasta_instala / "ACBrMonitor.ini"

        self._garantir_pastas()
        self._configurar_acbr_ini()

    def _garantir_pastas(self):
        self.pasta_instala.mkdir(parents=True, exist_ok=True)
        self.pasta_fiscal_in.mkdir(parents=True, exist_ok=True)
        self.pasta_fiscal_out.mkdir(parents=True, exist_ok=True)

    def _configurar_acbr_ini(self):
        cfg = configparser.ConfigParser()
        if self.arquivo_ini.exists():
            try:
                cfg.read(self.arquivo_ini, encoding="utf-8")
            except Exception:
                cfg = configparser.ConfigParser()

        # Alguns ambientes usam nomes diferentes de secao/chaves.
        # Atualizamos as convencoes mais comuns para forcar a troca por arquivos.
        secoes = ["ACBrMonitor", "Monitor", "MONITOR"]
        executavel_acbr = self._localizar_executavel_acbr()
        for secao in secoes:
            if secao not in cfg:
                cfg[secao] = {}

            cfg[secao]["PastaEntrada"] = str(self.pasta_fiscal_in)
            cfg[secao]["PastaSaida"] = str(self.pasta_fiscal_out)
            cfg[secao]["ArquivoEntrada"] = str(self.arquivo_entrega)
            cfg[secao]["ArquivoSaida"] = str(self.arquivo_retorno)
            cfg[secao]["ArqEntrada"] = str(self.arquivo_entrega)
            cfg[secao]["ArqSaida"] = str(self.arquivo_retorno)
            if executavel_acbr:
                cfg[secao]["Executavel"] = executavel_acbr

        with open(self.arquivo_ini, "w", encoding="utf-8") as f:
            cfg.write(f)

    def _localizar_executavel_acbr(self):
        candidatos = [
            self.pasta_instala / "ACBrMonitorPLUS.exe",
            self.pasta_instala / "ACBrMonitor.exe",
        ]
        for candidato in candidatos:
            if candidato.exists():
                return str(candidato)

        for arq in self.pasta_instala.glob("*ACBrMonitor*.exe"):
            return str(arq)
        return ""

    def _to_float(self, valor, default=0.0):
        try:
            return float(valor)
        except Exception:
            return float(default)

    def _fmt(self, valor, casas=2):
        return f"{self._to_float(valor):.{casas}f}"

    def _normalizar_item_nfce(self, item, indice):
        codigo = str(item.get("barcode") or item.get("id") or f"ITEM{indice}").strip() or f"ITEM{indice}"
        descricao = str(item.get("nome") or f"Item {indice}").strip() or f"Item {indice}"
        ean = str(item.get("ean") or item.get("barcode") or "SEM GTIN").strip() or "SEM GTIN"
        ncm = str(item.get("ncm") or "00000000").strip() or "00000000"
        cfop = str(item.get("cfop") or "5102").strip() or "5102"
        unidade = str(item.get("unidade") or "UN").strip() or "UN"

        quantidade = self._to_float(item.get("quantidade"), 1.0)
        if quantidade <= 0:
            quantidade = 1.0

        valor_unitario = self._to_float(item.get("preco"), 0.0)
        valor_total_item = round(quantidade * valor_unitario, 2)

        desconto_item = self._to_float(item.get("desconto"), 0.0)
        if desconto_item < 0:
            desconto_item = 0.0
        if desconto_item > valor_total_item:
            desconto_item = valor_total_item

        base_calculo = round(max(valor_total_item - desconto_item, 0.0), 2)

        p_icms = self._to_float(item.get("icms_aliquota", item.get("aliquota_imposto", 0.0)), 0.0)
        p_pis = self._to_float(item.get("pis_aliquota", 0.0), 0.0)
        p_cofins = self._to_float(item.get("cofins_aliquota", 0.0), 0.0)

        v_icms = round(base_calculo * (p_icms / 100.0), 2)
        v_pis = round(base_calculo * (p_pis / 100.0), 2)
        v_cofins = round(base_calculo * (p_cofins / 100.0), 2)

        return {
            "codigo": codigo,
            "descricao": descricao,
            "ean": ean,
            "ncm": ncm,
            "cfop": cfop,
            "unidade": unidade,
            "quantidade": quantidade,
            "valor_unitario": valor_unitario,
            "valor_total_item": valor_total_item,
            "desconto_item": desconto_item,
            "base_calculo": base_calculo,
            "p_icms": p_icms,
            "p_pis": p_pis,
            "p_cofins": p_cofins,
            "v_icms": v_icms,
            "v_pis": v_pis,
            "v_cofins": v_cofins,
        }

    def _calcular_totais_nfce(self, itens_norm):
        total_produtos = round(sum(i["valor_total_item"] for i in itens_norm), 2)
        total_descontos = round(sum(i["desconto_item"] for i in itens_norm), 2)
        total_base_calculo = round(sum(i["base_calculo"] for i in itens_norm), 2)
        total_icms = round(sum(i["v_icms"] for i in itens_norm), 2)
        total_pis = round(sum(i["v_pis"] for i in itens_norm), 2)
        total_cofins = round(sum(i["v_cofins"] for i in itens_norm), 2)
        total_nf = round(max(total_produtos - total_descontos, 0.0), 2)

        return {
            "total_produtos": total_produtos,
            "total_descontos": total_descontos,
            "total_base_calculo": total_base_calculo,
            "total_icms": total_icms,
            "total_pis": total_pis,
            "total_cofins": total_cofins,
            "total_nf": total_nf,
        }

    def gerar_comando_nfce(self, venda_id, forma_pgto, itens):
        mapa_pagamento = {
            "DINHEIRO": "01",
            "CREDITO": "03",
            "DEBITO": "04",
            "PIX": "17",
        }
        codigo_pagto = mapa_pagamento.get(str(forma_pgto or "").upper(), "99")

        itens = list(itens or [])
        if not itens:
            raise ValueError("Nao ha itens para emissao NFC-e.")

        itens_norm = [self._normalizar_item_nfce(item, idx) for idx, item in enumerate(itens, start=1)]
        totais = self._calcular_totais_nfce(itens_norm)

        linhas = [
            "NFE.LimparLista",
            f'NFE.CriarNFe("{venda_id}")',
            'NFE.SetCampo("NFe.infNFe.ide.mod=65")',
            'NFE.SetCampo("NFe.infNFe.ide.tpNF=1")',
            'NFE.SetCampo("NFe.infNFe.ide.indFinal=1")',
            'NFE.SetCampo("NFe.infNFe.ide.indPres=1")',
            'NFE.SetCampo("NFe.infNFe.ide.natOp=VENDA NFCe")',
        ]

        for idx, item in enumerate(itens_norm, start=1):
            det = f"{idx:03d}"
            linhas.extend(
                [
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.cProd={item["codigo"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.cEAN={item["ean"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.xProd={item["descricao"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.NCM={item["ncm"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.CFOP={item["cfop"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.uCom={item["unidade"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.qCom={self._fmt(item["quantidade"], 4)}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.vUnCom={self._fmt(item["valor_unitario"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.vProd={self._fmt(item["valor_total_item"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.cEANTrib={item["ean"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.uTrib={item["unidade"]}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.qTrib={self._fmt(item["quantidade"], 4)}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.prod.vUnTrib={self._fmt(item["valor_unitario"])}")',
                    'NFE.SetCampo("NFe.infNFe.det{det}.prod.indTot=1")'.replace("{det}", det),
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.ICMS.ICMS00.orig=0")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.ICMS.ICMS00.CST=00")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.ICMS.ICMS00.modBC=3")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.ICMS.ICMS00.vBC={self._fmt(item["base_calculo"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.ICMS.ICMS00.pICMS={self._fmt(item["p_icms"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.ICMS.ICMS00.vICMS={self._fmt(item["v_icms"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.PIS.PISAliq.CST=01")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.PIS.PISAliq.vBC={self._fmt(item["base_calculo"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.PIS.PISAliq.pPIS={self._fmt(item["p_pis"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.PIS.PISAliq.vPIS={self._fmt(item["v_pis"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.COFINS.COFINSAliq.CST=01")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.COFINS.COFINSAliq.vBC={self._fmt(item["base_calculo"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.COFINS.COFINSAliq.pCOFINS={self._fmt(item["p_cofins"])}")',
                    f'NFE.SetCampo("NFe.infNFe.det{det}.imposto.COFINS.COFINSAliq.vCOFINS={self._fmt(item["v_cofins"])}")',
                ]
            )

        linhas.extend(
            [
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vBC={self._fmt(totais["total_base_calculo"])}")',
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vICMS={self._fmt(totais["total_icms"])}")',
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vProd={self._fmt(totais["total_produtos"])}")',
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vDesc={self._fmt(totais["total_descontos"])}")',
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vPIS={self._fmt(totais["total_pis"])}")',
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vCOFINS={self._fmt(totais["total_cofins"])}")',
                f'NFE.SetCampo("NFe.infNFe.total.ICMSTot.vNF={self._fmt(totais["total_nf"])}")',
                'NFE.SetCampo("NFe.infNFe.transp.modFrete=9")',
                f'NFE.SetCampo("NFe.infNFe.pag.detPag001.tPag={codigo_pagto}")',
                f'NFE.SetCampo("NFe.infNFe.pag.detPag001.vPag={self._fmt(totais["total_nf"])}")',
                'NFE.EnviarNFe("1","1","")',
            ]
        )

        return "\n".join(linhas)

    def interpretar_retorno(self, retorno_txt):
        retorno = str(retorno_txt or "").strip()
        if not retorno:
            return {"sucesso": False, "mensagem": "Sem retorno do ACBrMonitor.", "retorno": retorno}

        lower = retorno.lower()
        sucesso_tokens = ["autorizado o uso", "autorizada", "ok", "100"]
        erro_tokens = ["erro", "rejeicao", "falha", "exception", "deneg", "nao autorizado"]

        encontrou_erro = any(token in lower for token in erro_tokens)
        encontrou_sucesso = any(token in lower for token in sucesso_tokens)

        if encontrou_erro and not encontrou_sucesso:
            primeira_linha = retorno.splitlines()[0].strip() if retorno.splitlines() else retorno
            return {"sucesso": False, "mensagem": primeira_linha, "retorno": retorno}

        return {"sucesso": True, "mensagem": "Emissao processada pelo ACBr.", "retorno": retorno}

    def iniciar_acbr(self):
        """Verifica se o ACBrMonitor esta ativo nos processos do Windows."""
        try:
            resultado = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            saida = (resultado.stdout or "").lower()
            return "acbrmonitor" in saida
        except Exception:
            return False

    def enviar_comando(self, comando):
        """
        Escreve comando em fiscal_in/ENTREGA.TXT e aguarda resposta em fiscal_out/RETORNO.TXT.
        Retorna o conteudo de RETORNO.TXT.
        """
        if not comando or not str(comando).strip():
            raise ValueError("Comando fiscal vazio.")

        self._garantir_pastas()
        self._configurar_acbr_ini()

        comando_txt = str(comando).strip() + "\n"

        retorno_existente = ""
        retorno_mtime = 0.0
        if self.arquivo_retorno.exists():
            try:
                retorno_existente = self.arquivo_retorno.read_text(encoding="utf-8", errors="ignore")
                retorno_mtime = self.arquivo_retorno.stat().st_mtime
            except Exception:
                retorno_existente = ""
                retorno_mtime = 0.0

        self.arquivo_entrega.write_text(comando_txt, encoding="utf-8")

        inicio = time.time()
        while time.time() - inicio <= self.timeout_segundos:
            if self.arquivo_retorno.exists():
                try:
                    mtime_atual = self.arquivo_retorno.stat().st_mtime
                    conteudo = self.arquivo_retorno.read_text(encoding="utf-8", errors="ignore")
                    if conteudo.strip() and (mtime_atual > retorno_mtime or conteudo != retorno_existente):
                        return conteudo.strip()
                except Exception:
                    pass

            time.sleep(self.intervalo_poll)

        raise TimeoutError("Timeout aguardando resposta fiscal em RETORNO.TXT.")

    def processar_xml_entrada(self, caminho_xml):
        """
        Le XML e extrai EAN, NCM, preco e impostos por item.
        Retorna dicionario pronto para persistencia.
        """
        caminho = Path(caminho_xml)
        if not caminho.exists():
            raise FileNotFoundError(f"XML fiscal nao encontrado: {caminho}")

        raiz = ET.parse(caminho).getroot()

        def _tag_local(tag):
            if not isinstance(tag, str):
                return ""
            if "}" in tag:
                return tag.split("}", 1)[1]
            return tag

        def _buscar_texto(node, nome_tags):
            for filho in node.iter():
                if _tag_local(filho.tag) in nome_tags and filho.text is not None:
                    valor = str(filho.text).strip()
                    if valor:
                        return valor
            return ""

        itens = []
        total_impostos = 0.0

        for det in raiz.iter():
            if _tag_local(det.tag) != "det":
                continue

            prod = None
            imposto = None
            for filho in list(det):
                nome = _tag_local(filho.tag)
                if nome == "prod":
                    prod = filho
                elif nome == "imposto":
                    imposto = filho

            if prod is None:
                continue

            descricao = _buscar_texto(prod, {"xProd"})
            ean = _buscar_texto(prod, {"cEAN", "cEANTrib"})
            ncm = _buscar_texto(prod, {"NCM"})

            preco_txt = _buscar_texto(prod, {"vUnCom", "vProd"})
            try:
                preco = float((preco_txt or "0").replace(",", "."))
            except Exception:
                preco = 0.0

            impostos_item = {
                "vICMS": 0.0,
                "vIPI": 0.0,
                "vPIS": 0.0,
                "vCOFINS": 0.0,
                "vII": 0.0,
                "vTotTrib": 0.0,
            }

            if imposto is not None:
                for filho in imposto.iter():
                    nome = _tag_local(filho.tag)
                    if nome in impostos_item and filho.text is not None:
                        try:
                            impostos_item[nome] = float(str(filho.text).replace(",", "."))
                        except Exception:
                            impostos_item[nome] = 0.0

            total_item_imposto = round(sum(impostos_item.values()), 2)
            total_impostos += total_item_imposto

            itens.append(
                {
                    "descricao": descricao,
                    "ean": ean,
                    "ncm": ncm,
                    "preco": round(preco, 2),
                    "impostos": impostos_item,
                    "total_impostos_item": total_item_imposto,
                }
            )

        chave_nfe = ""
        for node in raiz.iter():
            if _tag_local(node.tag) == "infNFe":
                chave_nfe = str(node.attrib.get("Id", "")).replace("NFe", "")
                break

        return {
            "arquivo_origem": str(caminho),
            "chave_nfe": chave_nfe,
            "itens": itens,
            "total_itens": len(itens),
            "total_impostos": round(total_impostos, 2),
        }