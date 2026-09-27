import os
import json
import time
import subprocess
import sys
import threading
from pathlib import Path
import xml.etree.ElementTree as ET
from datetime import datetime
import configparser
from modulo_config import carregar_configuracoes
from database_manager import obter_caminho_dados


def _es_nome_instalador_acbr(nome: str) -> bool:
    """True se o nome corresponde a um INSTALADOR, nunca ao motor fiscal.

    Cobre os nomes em ingles (Installer/DEMO/-I) e em portugues
    (Instalador/-Instalador), que existem no projeto: o instalador oficial
    traz "DEMO"/"-I", e o pacote de distribuicao tambem traz
    "ACBrMonitorPLUS-1.4.0.497-x86-Instalador.exe". Sem "instalador" na
    lista, esse arquivo seria tomado como motor fiscal pelo
    localizar_acbr_instalado().
    """
    nome_low = str(nome or "").lower()
    return (
        "installer" in nome_low
        or "instalador" in nome_low
        or "setup" in nome_low
        or "demo" in nome_low
        or nome_low.endswith("-i.exe")
    )


def _pasta_base_aplicacao() -> Path:
    """Pasta base do aplicativo (pasta do executável quando congelado).

    No executável (PyInstaller onedir), Path(__file__) aponta para a pasta
    interna do runtime (_internal), enquanto a pasta "instala"/"acbr" oficial
    fica junto ao executável principal. Em código-fonte, é a raiz do projeto.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _pastas_instala_candidatas(pasta_instala=None):
    """Pastas candidatas de empacotamento do ACBr, preservando a ordem do mecanismo atual."""
    pastas = []
    if pasta_instala is not None:
        pastas.append(Path(pasta_instala))
    pastas.append(Path(__file__).resolve().parent / "instala")
    pastas.append(_pasta_base_aplicacao() / "instala")
    pastas.append(_pasta_base_aplicacao() / "acbr")
    pastas.append(Path(__file__).resolve().parent / "_build_support" / "acbr")

    unicas = []
    vistos = set()
    for pasta in pastas:
        try:
            chave = str(pasta).lower()
        except Exception:
            chave = str(pasta)
        if chave not in vistos:
            vistos.add(chave)
            unicas.append(pasta)
    return unicas


def _caminhos_acbr_do_registro():
    """InstallLocation das instalações do ACBrMonitor registradas no Windows."""
    caminhos = []
    try:
        import winreg
    except Exception:
        return caminhos

    raizes = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    vistas = [
        0,
        getattr(winreg, "KEY_WOW64_32KEY", 0),
        getattr(winreg, "KEY_WOW64_64KEY", 0),
    ]
    for hive, caminho_raiz in raizes:
        for vista in vistas:
            try:
                with winreg.OpenKey(hive, caminho_raiz, 0, winreg.KEY_READ | vista) as raiz:
                    indice = 0
                    while True:
                        try:
                            subchave = winreg.EnumKey(raiz, indice)
                        except OSError:
                            break
                        indice += 1
                        try:
                            with winreg.OpenKey(raiz, subchave) as item:
                                nome, _ = winreg.QueryValueEx(item, "DisplayName")
                                if "acbrmonitor" not in str(nome or "").lower():
                                    continue
                                local, _ = winreg.QueryValueEx(item, "InstallLocation")
                                texto = str(local or "").strip().strip('"')
                                if texto:
                                    caminhos.append(Path(texto))
                        except Exception:
                            continue
            except Exception:
                continue
    return caminhos


def _caminhos_padrao_acbr_demo():
    """Diretórios padrão do instalador oficial ACBrMonitorPLUS (DEMO)."""
    pastas = [Path("C:/ACBrMonitorPLUS")]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var)
        if base:
            pastas.append(Path(base) / "ACBrMonitorPLUS")
    return pastas


def localizar_acbr_instalado(pasta_instala=None):
    """Localiza o executável REAL do motor fiscal (ACBrMonitor.exe).

    Mantém primeiro os caminhos já usados pelo FRS (pastas "instala") e depois
    acrescenta os destinos reais do instalador oficial DEMO: InstallLocation no
    registro do Windows e os diretórios padrão do ACBrMonitorPLUS. O instalador
    (DEMO/-I/Installer) nunca é retornado como motor.
    """
    for pasta in _pastas_instala_candidatas(pasta_instala):
        for nome in ("ACBrMonitorPLUS.exe", "ACBrMonitor.exe"):
            candidato = pasta / nome
            if candidato.exists():
                return candidato
        try:
            for arq in pasta.glob("*ACBrMonitor*.exe"):
                if _es_nome_instalador_acbr(arq.name):
                    continue
                return arq
        except Exception:
            pass

    pastas_extras = _caminhos_acbr_do_registro() + _caminhos_padrao_acbr_demo()
    for pasta in pastas_extras:
        try:
            if not pasta.is_dir():
                continue
        except Exception:
            continue
        for nome in ("ACBrMonitorPLUS.exe", "ACBrMonitor.exe"):
            candidato = pasta / nome
            if candidato.exists():
                return candidato
        try:
            for arq in pasta.glob("*ACBrMonitor*.exe"):
                if _es_nome_instalador_acbr(arq.name):
                    continue
                return arq
        except Exception:
            pass
    return None


def localizar_instalador_acbr_empacotado(pasta_instala=None):
    """Localiza o instalador oficial DEMO empacotado com o aplicativo."""
    for pasta in _pastas_instala_candidatas(pasta_instala):
        if not pasta.is_dir():
            continue
        fixo = pasta / "ACBrMonitor_Installer.exe"
        if fixo.exists():
            return fixo
        for padrao in (
            "ACBrMonitorPLUS-DEMO-*-I.exe",
            "ACBrMonitorPLUS*DEMO*.exe",
            "*ACBrMonitor*Installer*.exe",
        ):
            try:
                for arq in pasta.glob(padrao):
                    if arq.is_file():
                        return arq
            except Exception:
                continue
    return None


def instalar_acbr_demo(timeout_segundos=600):
    """Instala o ACBr Monitor com o instalador oficial DEMO empacotado.

    Usa exatamente os parâmetros já definidos no setup Inno do FRS
    (setup_frs.iss): /VERYSILENT /NORESTART. O UAC do Windows é apresentado
    normalmente pelo próprio instalador (sem elevação forçada). Aguarda a
    conclusão e retorna o executável real localizado (ou None).
    """
    # ACBr já instalado? NÃO executar o instalador novamente.
    existente = localizar_acbr_instalado()
    if existente:
        return existente

    instalador = localizar_instalador_acbr_empacotado()
    if instalador is None:
        return None

    comando = [
        "cmd",
        "/c",
        "start",
        "",
        "/wait",
        str(instalador),
        "/VERYSILENT",
        "/NORESTART",
    ]
    try:
        subprocess.run(
            comando,
            check=False,
            timeout=max(60, int(timeout_segundos)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass

    # Após o término, localiza o motor real instalado (nome original).
    inicio = time.time()
    while time.time() - inicio < 30:
        existente = localizar_acbr_instalado()
        if existente:
            return existente
        time.sleep(1.0)
    return localizar_acbr_instalado()


def normalizar_data_iso(valor) -> str:
    """Normaliza uma data (NF-e ou digitada) para o formato AAAA-MM-DD.

    Aceita AAAA-MM-DD, AAAA/MM/DD, DD/MM/AAAA, DD-MM-AAAA, AAAAMMDD, DDMMAAAA
    e datas com horário (ex.: 2026-10-15T00:00:00). Retorna "" quando não for
    possível interpretar uma data válida.
    """
    texto = str(valor or "").strip()
    if not texto:
        return ""

    candidatos = [texto]
    if len(texto) > 10 and "T" in texto:
        candidatos.insert(0, texto.split("T", 1)[0])
    if len(texto) > 10 and " " in texto:
        candidatos.insert(0, texto.split(" ", 1)[0])

    for candidato in candidatos:
        for formato in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d", "%d%m%Y"):
            try:
                return datetime.strptime(candidato, formato).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


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
        # Pasta "instala" e somente leitura (local de instalacao do ACBrMonitor);
        # nunca deve receber gravacoes para evitar WinError 5 sob Program Files.
        # Em PyInstaller (onedir) Path(__file__) aponta para a pasta interna do
        # runtime (_internal), enquanto o executavel e a pasta "instala" ficam
        # na raiz do aplicativo; por isso usamos _pasta_base_aplicacao(), que ja
        # resolve a pasta do executavel quando congelado.
        self.pasta_instala = _pasta_base_aplicacao() / "instala"
        # Em codigo-fonte (nao congelado) mantem a raiz historica do projeto,
        # onde a pasta "instala" tambem existe.
        if not self.pasta_instala.is_dir() and self.raiz_projeto.is_dir():
            self.pasta_instala = self.raiz_projeto / "instala"
        # Arquivos gerados em runtime (ini/entrega/retorno) vao para local gravavel.
        self.pasta_fiscal_in = Path(obter_caminho_dados("fiscal_in"))
        self.pasta_fiscal_out = Path(obter_caminho_dados("fiscal_out"))
        self.pasta_config_acbr = Path(obter_caminho_dados("acbrmonitor"))

        self.arquivo_entrega = self.pasta_fiscal_in / "ENTREGA.TXT"
        self.arquivo_retorno = self.pasta_fiscal_out / "RETORNO.TXT"
        self.arquivo_ini = self.pasta_config_acbr / "ACBrMonitor.ini"

        self._garantir_pastas()
        self._configurar_acbr_ini()

    def _garantir_pastas(self):
        # A pasta de instalacao do ACBr ja existe (criada pelo instalador); apenas
        # tentamos criá-la quando ainda executando a partir do codigo-fonte.
        try:
            self.pasta_instala.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.pasta_fiscal_in.mkdir(parents=True, exist_ok=True)
        self.pasta_fiscal_out.mkdir(parents=True, exist_ok=True)
        self.pasta_config_acbr.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Configuracao real do ACBrMonitorPLUS 1.4.0.467.
    #
    # A UI (Menu MONITOR) grava em "ACBrMonitor.ini", secao [ACBrMonitor],
    # com as chaves abaixo - verificadas empiricamente no executavel real.
    # O binario NAO possui as chaves antigas (PastaEntrada/PastaSaida/
    # ArqEntrada...): o Monitor ignorava o INI e mantinha o alerta
    # "Configure a forma de Integracao TCP/IP ou TXT".
    #
    # O INI real possui ~52 secoes (WebService, Certificado, SAT, NFSe...).
    # Apenas [ACBrMonitor] e gerenciada aqui; todas as demais sao PRESERVADAS.
    # ------------------------------------------------------------------
    _SECAO_ACBR = "ACBrMonitor"

    # Chaves fixas exigidas pelo FRS (modo TXT com troca por arquivos).
    # HashSenha NAO entra aqui: e gerado pela UI e deve ser preservado.
    _CHAVES_ACBR_FIXAS = {
        "Modo_TCP": "0",
        "Modo_TXT": "1",
        "MonitorarPasta": "0",
        "TCP_Porta": "3434",
        "TCP_TimeOut": "10000",
        "Converte_TCP_Ansi": "0",
        "Converte_TXT_Entrada_Ansi": "1",
        "Converte_TXT_Saida_Ansi": "1",
        "Intervalo": "50",
        "Gravar_Log": "1",
        "Arquivo_Log": "LOG.TXT",
        "Linhas_Log": "0",
        "Comandos_Remotos": "0",
        "Uma_Instancia": "1",
        "MostraAbas": "0",
        "MostrarNaBarraDeTarefas": "0",
        "RetirarAcentosNaResposta": "0",
        "MostraLogEmRespostasEnviadas": "0",
        "TipoResposta": "0",
    }

    # Ordem usada pela UI do ACBr (mantida para o INI ficar identico ao original).
    _ORDEM_ACBR = [
        "Modo_TCP", "Modo_TXT", "MonitorarPasta", "TCP_Porta", "TCP_TimeOut",
        "Converte_TCP_Ansi", "TXT_Entrada", "TXT_Saida",
        "Converte_TXT_Entrada_Ansi", "Converte_TXT_Saida_Ansi", "Intervalo",
        "Gravar_Log", "Arquivo_Log", "Linhas_Log", "Comandos_Remotos",
        "Uma_Instancia", "MostraAbas", "MostrarNaBarraDeTarefas",
        "RetirarAcentosNaResposta", "MostraLogEmRespostasEnviadas",
        "HashSenha", "TipoResposta",
    ]

    def _ini_do_motor(self):
        """Caminho do ACBrMonitor.ini real (na pasta do executavel do ACBr)."""
        executavel = self._localizar_executavel_acbr()
        if not executavel:
            return None
        return Path(executavel).resolve().parent / "ACBrMonitor.ini"

    def _valores_acbr_desejados(self):
        """Chaves [ACBrMonitor] exigidas, com caminhos ABSOLUTOS resolvidos."""
        valores = dict(self._CHAVES_ACBR_FIXAS)
        valores["TXT_Entrada"] = str(Path(self.arquivo_entrega).resolve())
        valores["TXT_Saida"] = str(Path(self.arquivo_retorno).resolve())
        return valores

    def _ler_ini_seguro(self, caminho):
        """Le o INI como texto bruto, preservando todas as secoes do ACBr.

        O configparser normaliza e reescreve o arquivo inteiro; o INI real
        tem ~52 secoes que NAO podem ser reescritas. Lemos o texto e mexemos
        apenas no bloco [ACBrMonitor].
        """
        if not caminho or not Path(caminho).exists():
            return None
        try:
            return Path(caminho).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @staticmethod
    def _bloco_acbr_do_texto(texto):
        """(inicio, fim, linhas) do bloco [ACBrMonitor] no texto, ou None."""
        if texto is None:
            return None
        linhas = texto.splitlines()
        ini = None
        for i, linha in enumerate(linhas):
            if linha.strip().lower() == "[acbrmonitor]":
                if ini is None:
                    ini = i
        if ini is None:
            return None
        fim = ini
        for j in range(ini + 1, len(linhas)):
            if linhas[j].strip().startswith("["):
                return ini, j, linhas[ini:j]
            fim = j
        return ini, len(linhas), linhas[ini:fim + 1]

    def _montar_bloco_acbr(self, valores, hash_atual=""):
        """Bloco [ACBrMonitor] na ordem da UI, preservando HashSenha."""
        out = [f"[{self._SECAO_ACBR}]"]
        for chave in self._ORDEM_ACBR:
            if chave == "HashSenha":
                if hash_atual:
                    out.append(f"HashSenha={hash_atual}")
                continue
            if chave in valores:
                out.append(f"{chave}={valores[chave]}")
        return "\n".join(out) + "\n"

    def _atualizar_ini_acbr(self, caminho, forcar=False):
        """Aplica [ACBrMonitor] preservando TODAS as demais secoes do INI.

        Idempotente: se o bloco ja estiver correto, o arquivo nao e tocado.
        Devolve (alterado: bool, motivo: str).
        """
        ini = Path(caminho)
        texto = self._ler_ini_seguro(ini)
        valores = self._valores_acbr_desejados()

        if texto is None:
            # Arquivo inexistente: cria somente com a secao necessaria.
            try:
                ini.parent.mkdir(parents=True, exist_ok=True)
                ini.write_text(self._montar_bloco_acbr(valores), encoding="utf-8")
            except OSError as exc:
                return False, f"nao foi possivel criar {ini}: {exc}"
            return True, f"ACBrMonitor.ini criado com a secao [{self._SECAO_ACBR}]"

        bloco = self._bloco_acbr_do_texto(texto)
        hash_atual = ""
        if bloco:
            for linha in bloco[2]:
                if linha.strip().lower().startswith("hashsenha="):
                    hash_atual = linha.split("=", 1)[1].strip()
                    break

        esperado = self._montar_bloco_acbr(valores, hash_atual)

        def _sem_hash(t):
            return [
                l.rstrip() for l in (t or "").splitlines()
                if l.strip() and not l.strip().lower().startswith("hashsenha=")
            ]

        if bloco and not forcar and _sem_hash("\n".join(bloco[2])) == _sem_hash(esperado):
            return False, f"[{self._SECAO_ACBR}] ja esta correta (preservado)"

        if bloco:
            # Substitui SOMENTE o bloco [ACBrMonitor], preservando o resto.
            inicio, fim, _ = bloco
            linhas = texto.splitlines()
            novo_texto = "\n".join(
                linhas[:inicio] + esperado.rstrip("\n").split("\n") + [""] + linhas[fim:]
            )
        else:
            # Nao existe a secao: acrescenta ao final, preservando o resto.
            sep = "" if (not texto.strip() or texto.endswith("\n")) else "\n"
            novo_texto = texto + sep + esperado

        if novo_texto == texto:
            return False, f"[{self._SECAO_ACBR}] ja esta correta (preservado)"

        try:
            ini.write_text(novo_texto, encoding="utf-8")
        except OSError as exc:
            return False, f"nao foi possivel gravar {ini}: {exc}"
        return True, f"[{self._SECAO_ACBR}] atualizado em {ini}"

    def _configurar_acbr_ini(self):
        """Aplica a configuracao real do monitor no ACBrMonitor.ini do motor.

        Nao reescreve o arquivo inteiro: apenas a secao [ACBrMonitor] e
        tocada, preservando as demais (~52) secoes do ACBr.
        """
        try:
            self.pasta_config_acbr.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # Prioriza o INI real do motor; usa o espelho local como fallback
        # quando o ACBr ainda nao esta instalado.
        ini_motor = self._ini_do_motor()
        if ini_motor is not None:
            self._atualizar_ini_acbr(ini_motor)
        else:
            self._atualizar_ini_acbr(self.arquivo_ini)



    def _localizar_executavel_acbr(self):
        # Mantém primeiro os caminhos já usados pelo FRS (pasta "instala") e
        # acrescenta, como fallback, os destinos reais do instalador oficial
        # DEMO (InstallLocation no registro + diretórios padrão do ACBr).
        # Instaladores (DEMO/-I/Installer) nunca são considerados motor.
        caminho = localizar_acbr_instalado(self.pasta_instala)
        return str(caminho) if caminho else ""

    # ------------------------------------------------------------------
    # Health-check e reparo da configuracao do ACBr (motor presente, porem
    # com INI ausente/invalido/desapontado para as pastas do FRS).
    # Nao reinstala o ACBr: apenas regrava a configuracao, de forma idempotente.
    # ------------------------------------------------------------------
    def _pastas_fiscais_gravaveis(self):
        """(ok, detalhe) para leitura+escrita nas pastas fiscais do FRS."""
        for rotulo, pasta in (
            ("entrada", self.pasta_fiscal_in),
            ("saida", self.pasta_fiscal_out),
        ):
            try:
                pasta.mkdir(parents=True, exist_ok=True)
                teste = pasta / "_frs_acbr_write.tmp"
                teste.write_text("ok", encoding="utf-8")
                teste.unlink(missing_ok=True)
            except OSError as exc:
                return False, f"pasta fiscal de {rotulo} nao gravavel: {exc}"
        return True, ""

    def _chaves_do_bloco(self, bloco):
        """Converte as linhas do bloco [ACBrMonitor] em dict (minusculas)."""
        if not bloco:
            return {}
        d = {}
        for linha in bloco:
            linha = linha.strip()
            if not linha or linha.startswith("[") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            d[chave.strip().lower()] = valor.strip()
        return d

    def _bloco_real_do_motor(self, caminho):
        """Lê o bloco [ACBrMonitor] do INI real, ou None."""
        texto = self._ler_ini_seguro(caminho)
        bloco = self._bloco_acbr_do_texto(texto)
        return bloco[2] if bloco else None

    def _healthcheck_acbr(self, executavel=None, verificar_pastas=True):
        """Diagnostica o ACBr instalado, sem reinstala-lo e sem gravar nada.

        Valida as chaves REAIS do ACBrMonitorPLUS 1.4.0.467 (secao
        [ACBrMonitor]): Modo_TXT, Modo_TCP, MonitorarPasta, TXT_Entrada,
        TXT_Saida, Converte_*_Ansi, Intervalo e TipoResposta.

        Devolve (ok: bool, motivo: str).
        """
        caminho = executavel or self._localizar_executavel_acbr()
        if not caminho:
            return False, "ACBr ausente (executavel nao encontrado)"
        caminho = Path(caminho)

        # (a) executavel presente e utilizavel
        if not caminho.is_file():
            return False, f"executavel do ACBr inexistente: {caminho}"
        if _es_nome_instalador_acbr(caminho.name):
            return False, f"apenas o instalador foi encontrado, nao o motor: {caminho.name}"

        # (b) configuracao real
        ini_motor = caminho.parent / "ACBrMonitor.ini"
        bloco = self._bloco_real_do_motor(ini_motor)
        if bloco is None:
            return False, f"ACBrMonitor.ini sem secao [ACBrMonitor] em {caminho.parent}"
        chaves = self._chaves_do_bloco(bloco)

        # (c) modo de integracao: TXT ativo, TCP desligado
        if chaves.get("modo_txt", "") != "1":
            return False, f"Modo_TXT inativo (valor: {chaves.get('modo_txt', 'ausente')!r})"
        if chaves.get("modo_tcp", "") != "0":
            return False, f"Modo_TCP deveria estar 0 (valor: {chaves.get('modo_tcp', 'ausente')!r})"

        # (d) TXT_Entrada = caminho absoluto do FRS
        esperado_entrada = str(Path(self.arquivo_entrega).resolve())
        atual_entrada = str(chaves.get("txt_entrada", "")).strip().strip('"')
        if not atual_entrada:
            return False, "TXT_Entrada ausente no ACBrMonitor.ini"
        if atual_entrada.lower() != esperado_entrada.lower():
            return False, f"TXT_Entrada divergente: {atual_entrada}"
        if not Path(atual_entrada).parent.is_dir():
            return False, f"pasta de TXT_Entrada inexistente: {Path(atual_entrada).parent}"

        # (e) TXT_Saida = caminho absoluto do FRS
        esperado_saida = str(Path(self.arquivo_retorno).resolve())
        atual_saida = str(chaves.get("txt_saida", "")).strip().strip('"')
        if not atual_saida:
            return False, "TXT_Saida ausente no ACBrMonitor.ini"
        if atual_saida.lower() != esperado_saida.lower():
            return False, f"TXT_Saida divergente: {atual_saida}"
        if not Path(atual_saida).parent.is_dir():
            return False, f"pasta de TXT_Saida inexistente: {Path(atual_saida).parent}"

        # (f) gravacao das pastas fiscais
        if verificar_pastas:
            ok_pastas, detalhe = self._pastas_fiscais_gravaveis()
            if not ok_pastas:
                return False, detalhe

        return True, ""

    def _configuracao_coerente(self, cfg=None):
        """True quando [ACBrMonitor] do motor ja esta correta para o FRS.

        "Correto" = as chaves REAIS apontam para os caminhos absolutos do FRS
        (TXT_Entrada/TXT_Saida) e as chaves fixas tem os valores esperados.
        Nao exige HashSenha, para nao forcar reescrita de config valida.
        """
        caminho = self._localizar_executavel_acbr()
        if not caminho:
            return False
        ini_motor = Path(caminho).parent / "ACBrMonitor.ini"
        if cfg is None:
            bloco = self._bloco_real_do_motor(ini_motor)
            if bloco is None:
                return False
            cfg = self._chaves_do_bloco(bloco)

        esperado = self._valores_acbr_desejados()
        for chave, valor in esperado.items():
            atual = str(cfg.get(chave.lower(), "")).strip().strip('"')
            if atual.lower() != valor.lower():
                return False
        return True

    def reparar_configuracao_acbr(self, forcar=False):
        """Reescreve a configuracao do ACBr quando ela nao serve ao FRS.

        Idempotente: se ja estiver coerente e `forcar` for False, o arquivo
        NAO e tocado (preserva configuracao valida). Nao reinstala o motor e
        nao altera as rotinas de comunicacao ENTREGA.TXT/RETORNO.TXT.

        Devolve (reparado: bool, motivo: str).
        """
        caminho = self._localizar_executavel_acbr()
        if not caminho:
            return False, "ACBr ausente: nada a reparar"
        caminho = Path(caminho)
        ini_motor = caminho.resolve().parent / "ACBrMonitor.ini"

        # Garante as pastas fiscais antes de apontar o ACBr para elas.
        ok_pastas, detalhe = self._pastas_fiscais_gravaveis()
        if not ok_pastas:
            return False, detalle

        # Idempotente: so reescreve quando [ACBrMonitor] nao serve ao FRS.
        return self._atualizar_ini_acbr(ini_motor, forcar=forcar)
        return True, f"configuracao do ACBr reparada em {ini_motor}"

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
        Le XML e extrai EAN, NCM, quantidade, validade, preco e impostos por item.
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
            unidade = _buscar_texto(prod, {"uCom", "uTrib"}) or "UN"
            quantidade_txt = _buscar_texto(prod, {"qCom", "qTrib"})
            try:
                quantidade = float((quantidade_txt or "0").replace(",", "."))
            except Exception:
                quantidade = 0.0
            validade = normalizar_data_iso(_buscar_texto(det, {"dVal"}))

            lotes = []
            for no_rastro in prod.iter():
                if _tag_local(no_rastro.tag) != "rastro":
                    continue
                numero_lote = _buscar_texto(no_rastro, {"nLote"})
                d_fab = normalizar_data_iso(_buscar_texto(no_rastro, {"dFab"}))
                d_val_lote = normalizar_data_iso(_buscar_texto(no_rastro, {"dVal"}))
                try:
                    q_lote = float(str(_buscar_texto(no_rastro, {"qLote"}) or "0").replace(",", "."))
                except Exception:
                    q_lote = 0.0
                if numero_lote or d_val_lote or d_fab:
                    lotes.append(
                        {
                            "numero_lote": numero_lote,
                            "quantidade": q_lote,
                            "data_fabricacao": d_fab,
                            "data_validade": d_val_lote,
                        }
                    )

            if not validade and lotes:
                validade = lotes[0]["data_validade"]

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
                    "unidade": unidade,
                    "quantidade": quantidade,
                    "validade": validade,
                    "lotes": lotes,
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