import inspect
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


_OLD_APPDATA = os.environ.get("APPDATA")
_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-pdv-tests-")
os.environ["APPDATA"] = _TEST_APPDATA.name

import app_paths
import modulo_config
import modulo_pdv
from modulo_pdv import ModuloPDV, _avaliar_troco_pagamento


class _Entry:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def delete(self, _start, _end):
        self.value = ""


class _Label:
    def __init__(self):
        self.text = "0.00"

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


class _Calculator:
    def calcular_impostos(self, *_args, **_kwargs):
        return {
            "aliquota": 0.0,
            "valor_imposto": 0.0,
            "valor_liquido": 50.0,
            "regime": "ATUAL",
            "aliquotas": {},
            "valores": {},
        }


class _FakePDV:
    def __init__(self, total=50.0):
        self.itens_carrinho = [{"total": float(total), "quantidade": 1, "preco": float(total)}]
        self.valor_pago_acumulado = 0.0
        self.pagamentos_parciais = []
        self.forma_pagamento_selecionada = "DINHEIRO"
        self._gaveta_solicitada_por_pagamento = False
        self.ent_valor_pago = _Entry()
        self.lbl_troco_venda = _Label()
        self.lbl_pago_venda = _Label()
        self.lbl_restante_venda = _Label()
        self.btn_acrescentar_pago = object()
        self.statuses = []
        self.gaveta_calls = []
        self.print_calls = []
        self.raw_prints = []
        self.delivery_calls = []
        self.config = {}
        self.formas_resolvidas = []
        self.calculadora_tributaria = _Calculator()

    def _ler_valor_pago_digitado(self):
        return ModuloPDV._ler_valor_pago_digitado(self)

    def _formatar_moeda_br(self, valor):
        return f"{float(valor):.2f}"

    def _set_status(self, mensagem, _cor="#3498db"):
        self.statuses.append(mensagem)

    def _safe_focus(self, _widget):
        return None

    def atualizar_troco_display(self):
        return ModuloPDV.atualizar_troco_display(self)

    def _resolver_forma_pagamento_registro(self, forma_explicita=None):
        self.formas_resolvidas.append(forma_explicita)
        return ModuloPDV._resolver_forma_pagamento_registro(self, forma_explicita)

    def _forma_para_gaveta_pos_venda(self, forma_pagamento):
        return ModuloPDV._forma_para_gaveta_pos_venda(self, forma_pagamento)

    def _acionar_gaveta_pos_venda(self, forma_pagamento):
        return ModuloPDV._acionar_gaveta_pos_venda(self, forma_pagamento)

    def abrir_gaveta(self, forma_pagamento=None):
        self.gaveta_calls.append(forma_pagamento)
        return True

    def imprimir_cupom(self, dados):
        self.print_calls.append(dados)

    def _largura_cupom_chars(self):
        return 42

    def _formatar_linha_item_cupom(self, nome, _qtd, _total, _largura, _unidade=None):
        return [str(nome)]

    def _enviar_raw_impressora_padrao(self, payload):
        self.raw_prints.append(payload)

    def _normalizar_bool(self, value):
        return bool(value)

    def _normalizar_origem_venda(self, _payload):
        return "DELIVERY", "Delivery"

    def _alertar_novo_pedido(self, _origem):
        return None

    def _resolver_item_delivery(self, _item, _index):
        return {"id": 1, "nome": "Produto", "quantidade": 1, "preco": 50.0, "total": 50.0}

    def finalizar_venda_pdv(self, *args, **kwargs):
        self.delivery_calls.append((args, kwargs))


class _ImpressaoStub:
    """Executa o imprimir_cupom REAL sem abrir janela nem tocar em impressora.

    Não substitui imprimir_cupom (ao contrário de _FakePDV): delega os helpers
    ao ModuloPDV e apenas intercepta o transporte, capturando os bytes crus.
    """

    def __init__(self, config):
        self.config = config
        self.raw_prints = []
        self.statuses = []
        self.gaveta_calls = []

    # Helpers delegadas ao ModuloPDV (implementação real).
    def _largura_cupom_chars(self):
        return ModuloPDV._largura_cupom_chars(self)

    def _split_texto_largura(self, texto, largura):
        return ModuloPDV._split_texto_largura(self, texto, largura)

    def _formatar_quantidade_cupom(self, qtd, unidade="UN"):
        return ModuloPDV._formatar_quantidade_cupom(self, qtd, unidade)

    def _formatar_linha_item_cupom(self, nome, qtd, total, largura, unidade="UN"):
        return ModuloPDV._formatar_linha_item_cupom(self, nome, qtd, total, largura, unidade)

    def _formatar_moeda_br(self, valor):
        return f"R$ {float(valor):.2f}"

    def _montar_texto_resumo_fechamento(self, dados):
        return ModuloPDV._montar_texto_resumo_fechamento(self, dados)

    # Transporte interceptado: nada é enviado a impressora real.
    def _enviar_raw_impressora_padrao(self, payload):
        self.raw_prints.append(payload)

    def _set_status(self, mensagem, _cor="#3498db"):
        self.statuses.append(mensagem)

    def winfo_exists(self):
        return True

    def abrir_gaveta(self, forma_pagamento=None):
        self.gaveta_calls.append(forma_pagamento)
        return True

    def imprimir_cupom(self, dados_venda):
        """Delega ao ModuloPDV real (usado também por imprimir_cupom_vale)."""
        ModuloPDV.imprimir_cupom(self, dados_venda)
        return self.texto()

    def imprimir(self, itens=None, **extra):
        dados = {
            "itens": itens
            or [{"nome": "PRODUTO", "quantidade": 1, "total": 10.0, "preco": 10.0}],
            "total": 10.0,
            "forma_pagamento": "DINHEIRO",
        }
        dados.update(extra)
        return self.imprimir_cupom(dados)

    def texto(self):
        """Último payload decodificado, para as asserções."""
        return self.raw_prints[-1].decode("cp850", errors="replace")


class TestCabecalhoImpressao(unittest.TestCase):
    """BLOCO 3 / FASE 2 — identidade empresarial completa no cabeçalho."""

    def setUp(self):
        # imprimir_cupom chama registrar_log, que abriria/criaria o banco real
        # do APPDATA de teste. Neutralizado para manter o teste isolado.
        from unittest.mock import patch

        neutro_log = patch.object(modulo_pdv, "registrar_log", lambda *a, **k: None)
        neutro_log.start()
        self.addCleanup(neutro_log.stop)

    def _config(self, **overrides):
        base = {
            "nome_fantasia": "MERCADO BOM PRECO",
            "razao_social": "MERCADO BOM PRECO COMERCIO DE ALIMENTOS LTDA",
            "cnpj": "12.345.678/0001-90",
            "logradouro": "Rua das Flores",
            "numero": "123",
            "bairro": "Centro",
            "cidade": "Sao Paulo",
            "uf": "SP",
            "cep": "01000-000",
            "largura_cupom_mm": 80,
        }
        base.update(overrides)
        return base

    def test_nome_fantasia_e_razao_social_diferentes_aparecem(self):
        """A) Ambos os nomes aparecem quando sao diferentes."""
        texto = _ImpressaoStub(self._config()).imprimir()

        self.assertIn("MERCADO BOM PRECO", texto)
        self.assertIn("MERCADO BOM PRECO COMERCIO DE ALIMENTOS LTDA", texto)

    def test_razao_social_igual_nao_duplica(self):
        """B) Nomes iguais nao geram linha duplicada."""
        igual = "MERCADO BOM PRECO"
        texto_igual = _ImpressaoStub(
            self._config(nome_fantasia=igual, razao_social=igual)
        ).imprimir()
        texto_diferente = _ImpressaoStub(
            self._config(nome_fantasia=igual, razao_social="OUTRA RAZAO SOCIAL LTDA")
        ).imprimir()

        # Nos dois casos o nome aparece 2x: bloco de destaque + linha do cabeçalho.
        # Havendo linha extra, a contagem seria 3.
        self.assertEqual(texto_igual.count(igual), 2)
        self.assertEqual(texto_igual.count(igual), texto_diferente.count(igual))
        self.assertNotIn("OUTRA RAZAO SOCIAL", texto_igual)

    def test_sem_nome_fantasia_razao_social_vira_identidade(self):
        """C) Sem Nome Fantasia, a Razao Social continua aparecendo."""
        texto = _ImpressaoStub(self._config(nome_fantasia="")).imprimir()

        self.assertIn("MERCADO BOM PRECO COMERCIO DE ALIMENTOS LTDA", texto)

    def test_cnpj_e_endereco_preservados(self):
        """D) CNPJ e endereco continuam impressos exatamente como antes."""
        texto = _ImpressaoStub(self._config()).imprimir()

        for esperado in (
            "CNPJ: 12.345.678/0001-90",
            "Rua das Flores",
            "123",
            "Centro",
            "Sao Paulo/SP",
            "CEP: 01000-000",
        ):
            self.assertIn(esperado, texto)

    def test_imprimir_cupom_nao_abre_gaveta(self):
        """E) A impressao do cupom nao aciona a gaveta."""
        stub = _ImpressaoStub(self._config())
        stub.imprimir()

        self.assertEqual(stub.gaveta_calls, [])

    def test_vale_usa_o_mesmo_cabecalho_corrigido(self):
        """F) imprimir_cupom_vale compoe o mesmo cabecalho, sem banco real."""
        from unittest.mock import patch

        class _Cursor:
            def __init__(self, respostas):
                self.respostas = respostas
                self.indice = 0

            def fetchone(self):
                valor = self.respostas[self.indice]
                self.indice += 1
                return valor

            def fetchall(self):
                return self.respostas[self.indice]

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        class _Conn:
            def __init__(self):
                self.n = 0

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def execute(self, _sql, _params=None):
                self.n += 1
                if self.n == 1:
                    return _Cursor([(9, 77, "2026-01-01", "PENDENTE", 30.0, "Maria")])
                return _Cursor([[("Prod", 2.0, "UN", 15.0, 30.0)]])

        stub = _ImpressaoStub(self._config())
        with patch.object(modulo_pdv, "get_db_connection", lambda: _Conn()):
            ModuloPDV.imprimir_cupom_vale(stub, 9)

        texto = stub.texto()
        self.assertIn("MERCADO BOM PRECO", texto)
        self.assertIn("MERCADO BOM PRECO COMERCIO DE ALIMENTOS LTDA", texto)
        self.assertIn("VALE", texto)

    def test_reimpressao_reconstroi_com_o_cabecalho_corrigido(self):
        """G) Reimpressao monta o mesmo documento, sem regressao."""
        texto = _ImpressaoStub(self._config()).imprimir(
            itens=[{"nome": "REIMPRESSO", "quantidade": 1, "total": 10.0, "preco": 10.0}]
        )

        self.assertIn("REIMPRESSO", texto)
        self.assertIn("MERCADO BOM PRECO COMERCIO DE ALIMENTOS LTDA", texto)


class TestLarguraCupomConfiguravel(unittest.TestCase):
    """BLOCO 3 / FASE 5 — largura da bobina configurável (58/80)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-f5-largura-")
        self.addCleanup(temp.cleanup)
        self.dest = Path(temp.name)
        # imprimir_cupom chama registrar_log, que criaria banco de verdade.
        neutro = patch.object(modulo_pdv, "registrar_log", lambda *a, **k: None)
        neutro.start()
        self.addCleanup(neutro.stop)

    def _escrever(self, payload):
        caminho = self.dest / "config.json"
        caminho.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return caminho

    def _carregar(self, payload):
        caminho = self._escrever(payload)
        with patch.object(modulo_config, "CONFIG_FILE", str(caminho)), patch.object(
            modulo_config, "_garantir_pastas_usuario", lambda: None
        ):
            return modulo_config.carregar_configuracoes()

    def _base(self, **extra):
        dados = {
            "nome_fantasia": "MERCADO TESTE",
            "razao_social": "MERCADO TESTE LTDA",
            "cnpj": "11.222.333/0001-44",
        }
        dados.update(extra)
        return dados

    def test_config_padrao_contem_largura_80(self):
        """A) O default oficial é 80 mm."""
        self.assertEqual(modulo_config._config_padrao()["largura_cupom_mm"], 80)

    def test_config_sem_largura_carrega_com_default_80(self):
        """B) Instalação antiga (sem a chave) recebe 80."""
        self.assertEqual(self._carregar(self._base())["largura_cupom_mm"], 80)

    def test_config_com_58_preservado(self):
        """C) Valor 58 do cliente é preservado."""
        self.assertEqual(self._carregar(self._base(largura_cupom_mm=58))["largura_cupom_mm"], 58)

    def test_config_com_80_preservado(self):
        """D) Valor 80 do cliente é preservado."""
        self.assertEqual(self._carregar(self._base(largura_cupom_mm=80))["largura_cupom_mm"], 80)

    def test_largura_58_conta_como_estado_do_cliente(self):
        """E) _config_tem_dados_usuario reconhece 58 como escolha do cliente."""
        # Sem identidade para isolar EXCLUSIVAMENTE a regra da largura.
        self.assertTrue(app_paths._config_tem_dados_usuario({"largura_cupom_mm": 58}))

    def test_largura_padrao_ou_ausente_nao_conta_como_estado(self):
        """Ausente ou 80 mm não podem reclassificar um config antigo."""
        self.assertFalse(app_paths._config_tem_dados_usuario({}))
        self.assertFalse(app_paths._config_tem_dados_usuario({"largura_cupom_mm": 80}))
        self.assertFalse(app_paths._config_tem_dados_usuario({"largura_cupom_mm": ""}))

    def _imprimir(self, mm):
        import test_pdv_payment_flow as pdv_tests

        cfg = self._base(largura_cupom_mm=mm, cnpj="11.222.333/0001-44")
        cfg.update(
            {
                "logradouro": "Rua A", "numero": "10", "bairro": "Centro",
                "cidade": "Sao Paulo", "uf": "SP", "cep": "01000-000",
            }
        )
        stub = pdv_tests._ImpressaoStub(cfg)
        with patch.object(modulo_pdv, "registrar_log", lambda *a, **k: None):
            texto = stub.imprimir()
        return stub, texto

    def _larguras_separador(self, texto):
        """Comprimentos reais das linhas feitas só de hífens."""
        return {
            len(linha)
            for linha in texto.split("\n")
            if linha and set(linha) == {"-"}
        }

    def test_cabecalho_58mm_usa_32_caracteres(self):
        """F) 58 mm -> 32 caracteres."""
        _stub, texto = self._imprimir(58)
        self.assertEqual(self._larguras_separador(texto), {32})

    def test_cabecalho_80mm_usa_48_caracteres(self):
        """G) 80 mm -> 48 caracteres."""
        _stub, texto = self._imprimir(80)
        self.assertEqual(self._larguras_separador(texto), {48})

    def test_nomes_presentes_nas_duas_larguras(self):
        """H) Nome Fantasia e Razão Social presentes em 58 mm e 80 mm."""
        for mm in (58, 80):
            with self.subTest(mm=mm):
                _stub, texto = self._imprimir(mm)
                self.assertIn("MERCADO TESTE", texto)
                self.assertIn("MERCADO TESTE LTDA", texto)

    def test_impressao_nao_abre_gaveta(self):
        """I) Imprimir o cupom nunca aciona a gaveta."""
        for mm in (58, 80):
            with self.subTest(mm=mm):
                stub, _texto = self._imprimir(mm)
                self.assertEqual(stub.gaveta_calls, [])

    def test_normalizar_nao_perde_chaves_existentes(self):
        """J) Nenhuma chave existente é perdida ao normalizar/salvar."""
        original = self._base(
            cnpj="11.222.333/0001-44",
            logradouro="Rua A", numero="10", bairro="Centro",
            cidade="Sao Paulo", uf="SP", cep="01000-000",
            fiscal_ativo=True, emissor_fiscal_path=r"C:\ACBrMonitorPLUS\ACBrMonitor.exe",
            email_cliente="cliente@exemplo.com", largura_cupom_mm=58,
        )
        normalizado = modulo_config._normalizar_config(original)

        for chave, valor in original.items():
            if chave in ("pasta_entrada_fiscal", "pasta_retorno_fiscal",
                         "pasta_exportacao_fiscal"):
                continue  # normalizadas de propósito para APPDATA
            self.assertEqual(normalizado[chave], valor, f"chave alterada: {chave}")
        self.assertEqual(normalizado["largura_cupom_mm"], 58)

    def test_config_json_existente_nao_perde_chaves_ao_carregar(self):
        """J) Carregar e regravar mantém todas as chaves do cliente."""
        caminho = self._escrever(self._base(cnpj="11.222.333/0001-44", largura_cupom_mm=58))
        with patch.object(modulo_config, "CONFIG_FILE", str(caminho)), patch.object(
            modulo_config, "_garantir_pastas_usuario", lambda: None
        ):
            modulo_config.carregar_configuracoes()

        relido = json.loads(caminho.read_text(encoding="utf-8"))
        self.assertEqual(relido["cnpj"], "11.222.333/0001-44")
        self.assertEqual(relido["razao_social"], "MERCADO TESTE LTDA")
        self.assertEqual(relido["largura_cupom_mm"], 58)


class TestFechamentoRespeitaLargura(unittest.TestCase):
    """BLOCO 3 / FASE 7 — fechamento de caixa passa a respeitar a bobina."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-f7-fechamento-")
        self.addCleanup(temp.cleanup)
        self.dest = Path(temp.name)
        # registrar_log criaria log real em disco.
        neutro = patch.object(modulo_pdv, "registrar_log", lambda *a, **k: None)
        neutro.start()
        self.addCleanup(neutro.stop)

    def _dados(self, **ajustes):
        dados = {
            "data_hora": "01/10/2026 18:00:00",
            "caixa_id": 1,
            "operador": "ADMIN",
            "saldo_abertura": 100.0,
            "total_calculado": 1500.0,
            "total_informado": 1500.0,
            "diferenca_geral": 0.0,
            "modalidades": [
                {"modalidade": "DINHEIRO", "calculado": 1000.0, "informado": 1000.0, "diferenca": 0.0},
                {"modalidade": "PIX", "calculado": 500.0, "informado": 500.0, "diferenca": 0.0},
            ],
            "sangrias": [{"hora": "17:00", "valor": 50.0}],
            "reforcos": [],
            "total_vales_gerados": 250.0,
        }
        dados.update(ajustes)
        return dados

    def _stub(self, mm):
        return _ImpressaoStub({"largura_cupom_mm": mm})

    def _fechamento(self, mm, **ajustes):
        stub = self._stub(mm)
        texto = ModuloPDV._montar_texto_resumo_fechamento(stub, self._dados(**ajustes))
        return stub, texto.split("\n")

    def test_58mm_nenhuma_linha_excede_32(self):
        """A) Em 58 mm nenhuma linha passa de 32 caracteres."""
        stub, linhas = self._fechamento(58)
        self.assertEqual(stub._largura_cupom_chars(), 32)
        excedentes = [x for x in linhas if len(x) > 32]
        self.assertEqual(excedentes, [], f"linhas acima de 32: {excedentes}")

    def test_80mm_nenhuma_linha_excede_48(self):
        """B) Em 80 mm nenhuma linha passa de 48 caracteres."""
        stub, linhas = self._fechamento(80)
        self.assertEqual(stub._largura_cupom_chars(), 48)
        excedentes = [x for x in linhas if len(x) > 48]
        self.assertEqual(excedentes, [], f"linhas acima de 48: {excedentes}")

    def test_valores_do_fechamento_preservados(self):
        """C) Nenhuma cifra é truncada ou perdida nas duas larguras."""
        for mm in (58, 80):
            with self.subTest(mm=mm):
                _stub, linhas = self._fechamento(mm)
                texto = "\n".join(linhas)
                for esperado in ("1000.00", "500.00", "100.00", "1500.00", "50.00", "250,00"):
                    self.assertIn(esperado, texto, f"valor ausente: {esperado}")

    def test_estrutura_80mm_mantem_tabela_de_4_colunas(self):
        """D1) Em 80 mm a tabela de conferência continua em 4 colunas."""
        _stub, linhas = self._fechamento(80)
        cabecalho = next(x for x in linhas if x.startswith("MODALIDADE"))
        self.assertEqual(cabecalho, "MODALIDADE     SISTEMA   INFORM.    DIFER.")
        linha_dinheiro = next(x for x in linhas if x.startswith("DINHEIRO"))
        # Nome + 3 valores na mesma linha (42 caracteres, como antes).
        self.assertEqual(len(linha_dinheiro), 42)
        self.assertIn("1000.00", linha_dinheiro)

    def test_estrutura_58mm_empilha_sem_truncar(self):
        """D2) Em 58 mm cada modalidade vira um bloco legível e íntegro."""
        _stub, linhas = self._fechamento(58)
        texto = "\n".join(linhas)
        for modalidade in ("DINHEIRO", "PIX"):
            self.assertIn(f"\n{modalidade}\n", f"\n{texto}\n")
        for cabecalho in ("MODALIDADE", "SISTEMA", "INFORMADO", "DIFERENCA"):
            self.assertIn(cabecalho, texto)
        # o cabeçalho "INFORMADO" (sozinho) não conta; só as linhas com valor.
        linhas_valor = [x for x in linhas if x.startswith("INFORMADO") and len(x) > 9]
        self.assertEqual(
            linhas_valor,
            ["INFORMADO                1000.00", "INFORMADO                 500.00"],
        )
        for total in ("TOTAL DO SISTEMA", "VALOR INFORMADO", "DIFERENCA",
                      "VALOR DE ABERTURA DO CAIXA"):
            self.assertIn(total, texto)
        self.assertIn("TOTAL DE VALE GERADO DURANTE O", texto)
        self.assertIn("DIA", texto)

    def test_impressao_fechamento_nao_abre_gaveta(self):
        """E) Imprimir o fechamento não aciona a gaveta e usa o transporte RAW."""
        stub = self._stub(58)
        ModuloPDV._imprimir_resumo_fechamento(stub, self._dados())
        self.assertEqual(stub.gaveta_calls, [])
        self.assertEqual(len(stub.raw_prints), 1)
        self.assertIn(b"FECHAMENTO DE CAIXA", stub.raw_prints[0])

    def _cupom(self, mm, documento_tipo="", extras=None):
        stub = _ImpressaoStub({"largura_cupom_mm": mm})
        stub.config = {
            "largura_cupom_mm": mm,
            "nome_fantasia": "MERCEARIA DO BAIRRO",
            "razao_social": "MERCEARIA DO BAIRRO COMERCIO DE ALIMENTOS LTDA",
            "cnpj": "12.345.678/0001-99",
            "logradouro": "AVENIDA BRASIL", "numero": "1500",
            "bairro": "CENTRO", "cidade": "SAO PAULO", "uf": "SP", "cep": "01310-100",
        }
        dados = {
            "itens": [{"nome": "PRODUTO", "quantidade": 1, "total": 10.0}],
            "total": 10.0,
            "forma_pagamento": "DINHEIRO",
            "documento_tipo": documento_tipo,
        }
        dados.update(extras or {})
        stub.imprimir_cupom(dados)
        return stub, stub.raw_prints[0].decode("cp850")

    def test_cupom_nao_fiscal_inalterado(self):
        """F) O cupom não fiscal mantém cabeçalho e larguras de antes."""
        for mm, largura in ((58, 32), (80, 48)):
            with self.subTest(mm=mm):
                _stub, texto = self._cupom(mm)
                for esperado in ("MERCEARIA DO BAIRRO",
                                 "MERCEARIA DO BAIRRO COMERCIO DE ALIMENTOS LTDA",
                                 "CNPJ: 12.345.678/0001-99", "AVENIDA BRASIL, 1500",
                                 "CENTRO", "SAO PAULO/SP", "CEP: 01310-100",
                                 "CUPOM NAO FISCAL"):
                    self.assertIn(esperado, texto)
                self.assertIn("-" * largura, texto)

    def test_vale_inalterado(self):
        """G) O Vale mantém o mesmo cabeçalho do cupom."""
        for mm, largura in ((58, 32), (80, 48)):
            with self.subTest(mm=mm):
                _stub, texto = self._cupom(
                    mm,
                    documento_tipo="VALE",
                    extras={"numero_documento": "V-001", "cliente_nome": "JOAO",
                            "status_documento": "PENDENTE"},
                )
                for esperado in ("MERCEARIA DO BAIRRO",
                                 "MERCEARIA DO BAIRRO COMERCIO DE ALIMENTOS LTDA",
                                 "VALE - PENDENTE", "VALE: V-001", "CLIENTE: JOAO"):
                    self.assertIn(esperado, texto)
                self.assertIn("-" * largura, texto)

    def test_orcamento_continua_pdf(self):
        """H) Orçamento permanece PDF/A4 e não toca no transporte térmico."""
        import modulo_orcamento

        stub = self._stub(58)
        with patch.object(modulo_orcamento, "gerar_pdf_orcamento", return_value="PDF") as pdf:
            retorno = ModuloPDV.imprimir_cupom_orcamento(stub, 7)
        self.assertEqual(retorno, "PDF")
        pdf.assert_called_once()
        self.assertEqual(pdf.call_args.args[0], 7)
        self.assertEqual(stub.raw_prints, [], "orçamento não pode imprimir em térmica")


class _LabelFiscal:
    """Label falsa que registra o texto exibido ao operador."""

    def __init__(self, existe=True):
        self.existe = existe
        self.textos = []

    def winfo_exists(self):
        return self.existe

    def configure(self, **kwargs):
        self.textos.append(kwargs)


class _PDVFiscalMinimo:
    """Hospeda apenas o que _atualizar_status_fiscal_ui utiliza."""

    def __init__(self, label=None):
        self.lbl_status_fiscal = label or _LabelFiscal()
        self.statuses = []
        self.after_agendados = []
        self.existe = True

    def winfo_exists(self):
        return self.existe

    def _set_status(self, mensagem, _cor="#3498db"):
        self.statuses.append(mensagem)

    def _safe_after(self, _ms, func):
        # Executa síncrono: o objetivo é observar a decisão, não a agendação.
        self.after_agendados.append(func)
        func()


class TestMonitoramentoLegadoNaoSobrescreveStatusAcBr(unittest.TestCase):
    """R1: o TIMEOUT órfão não pode apagar o status real confirmado pelo ACBr."""

    @classmethod
    def setUpClass(cls):
        import modulo_fiscal

        cls.modulo_fiscal = modulo_fiscal
        cls.TIMEOUT_ANTES = 121  # tentativas do laço legado (120) + folga

    def _rodar_monitoramento_ate_timeout(self, venda_id):
        """Executa monitorar_retorno com sleep neutralizado e espera a thread."""
        mf = self.modulo_fiscal
        exportador = object.__new__(mf.ModuloExportacaoFiscal)
        exportador.pasta_retorno_integrador = self._pasta_vazia

        recebidos = []
        with patch.object(mf.time, "sleep", lambda _s: None):
            exportador.monitorar_retorno(venda_id, lambda s, m: recebidos.append((s, m)))
            # A thread é daemon e faz 120 iterações instantâneas sem sleep.
            for _ in range(200):
                if len(recebidos) >= 1:
                    break
                threading.Event().wait(0.01)
        return recebidos

    def setUp(self):
        import tempfile

        self._temp = tempfile.TemporaryDirectory(prefix="frs-monitor-legado-")
        self.addCleanup(self._temp.cleanup)
        self._pasta_vazia = self._temp.name

    def test_timeout_legado_nao_chega_ao_callback_de_status(self):
        """O TIMEOUT legado não é reportado como se fosse o status fiscal."""
        recebidos = self._rodar_monitoramento_ate_timeout(4242)
        self.assertNotIn(
            "TIMEOUT",
            [status for status, _ in recebidos],
            "o fluxo legado não deve emitir TIMEOUT para a UI",
        )

    def test_status_confirmado_pelo_acbr_permanece_exibido_apos_timeout(self):
        """O texto 'Venda Autorizada!' do ACBr não é sobrescrito pelo TIMEOUT."""
        pdv = _PDVFiscalMinimo()

        # 1) ACBr confirma a venda: a UI registra a autorização.
        pdv.lbl_status_fiscal.configure(
            text="Fiscal: Venda Autorizada!", text_color="green"
        )
        confirmados = list(pdv.lbl_status_fiscal.textos)

        # 2) O monitoramento legado roda até o fim sem encontrar resposta.
        recebidos = self._rodar_monitoramento_ate_timeout(4242)

        # 3) O fluxo legado não pode tocar na UI (nenhum callback de status).
        self.assertEqual(
            [s for s, _ in recebidos],
            [],
            "o TIMEOUT legado não pode chegar ao callback de status",
        )
        self.assertEqual(
            pdv.lbl_status_fiscal.textos,
            confirmados,
            "a confirmação do ACBr não pode ser sobrescrita pelo fluxo legado",
        )
        self.assertEqual(pdv.statuses, [], "nenhum erro fiscal pode ser exibido")

    def test_timeout_legado_nao_altera_o_status_ja_confirmado(self):
        """Mesmo reaproveitando o callback, o status exibido é o do ACBr."""
        import modulo_pdv

        pdv = _PDVFiscalMinimo()
        pdv.lbl_status_fiscal.configure(
            text="Fiscal: Venda Autorizada!", text_color="green"
        )

        # O PDV continua aceitando o callback legado; quem decide o que
        # exibir é o status emitido. Com a correção, nenhum "TIMEOUT" é emitido.
        recebidos = self._rodar_monitoramento_ate_timeout(7)
        for status, mensagem in recebidos:
            modulo_pdv.ModuloPDV._atualizar_status_fiscal_ui(pdv, status, mensagem)

        textos = [t.get("text") for t in pdv.lbl_status_fiscal.textos]
        self.assertEqual(textos, ["Fiscal: Venda Autorizada!"])

    def test_callback_legado_ainda_reporta_sucesso_e_erro_reais(self):
        """Sucesso/Erro do arquivo legado continuam chegando (comportamento mantido)."""
        import json

        mf = self.modulo_fiscal
        exportador = object.__new__(mf.ModuloExportacaoFiscal)
        exportador.pasta_retorno_integrador = self._pasta_vazia

        for status, motivo in (("SUCESSO", "ok"), ("ERRO", "falha")):
            with self.subTest(status=status):
                destino = Path(self._pasta_vazia) / f"retorno_venda_{status}.json"
                destino.write_text(
                    json.dumps({"status": status, "motivo": motivo}), encoding="utf-8"
                )
                recebidos = []
                with patch.object(mf.time, "sleep", lambda _s: None):
                    exportador.monitorar_retorno(
                        status, lambda s, m: recebidos.append((s, m))
                    )
                    for _ in range(300):
                        if recebidos:
                            break
                        threading.Event().wait(0.01)

                self.assertTrue(
                    recebidos, f"callback do fluxo legado não disparou para {status}"
                )
                self.assertEqual(recebidos[0][0], status)
                self.assertEqual(recebidos[0][1], motivo)
                # NOTA: o arquivo NÃO é removido no Windows porque o
                # os.remove() do código legado roda dentro do `with open(...)`.
                # É um defeito pré-existente e fora do escopo da correção R1
                # (TIMEOUT órfão); por isso não é afirmado aqui.


class TestPDVPaymentFlow(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _TEST_APPDATA.cleanup()
        if _OLD_APPDATA is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = _OLD_APPDATA

    def _confirmar_parcela(self, forma, valor, pdv=None):
        pdv = pdv or _FakePDV()
        pdv.forma_pagamento_selecionada = forma
        pdv.ent_valor_pago.value = str(valor)
        ModuloPDV.acrescentar_valor_pago(pdv)
        return pdv

    def setUp(self):
        logger_patch = patch.object(modulo_pdv, "registrar_log")
        logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def test_cash_at_total_opens_once_and_has_no_change(self):
        pdv = self._confirmar_parcela("DINHEIRO", 50)
        self.assertEqual(pdv.gaveta_calls, ["DINHEIRO"])
        self.assertEqual(pdv.lbl_troco_venda.text, "0.00")
        pdv._acionar_gaveta_pos_venda("DINHEIRO")
        self.assertEqual(pdv.gaveta_calls, ["DINHEIRO"])

    def test_cash_over_total_keeps_change_visible(self):
        pdv = self._confirmar_parcela("DINHEIRO", 60)
        self.assertEqual(pdv.lbl_troco_venda.text, "10.00")
        self.assertEqual(pdv.gaveta_calls, ["DINHEIRO"])

    def test_unconfirmed_cash_amount_cannot_skip_to_finalization(self):
        pdv = _FakePDV()
        pdv.forma_pagamento_selecionada = "DINHEIRO"
        pdv.ent_valor_pago.value = "60"

        result = ModuloPDV._validar_e_obter_valor_pagamento(pdv, "DINHEIRO")

        self.assertIsNone(result)
        self.assertEqual(pdv.valor_pago_acumulado, 0.0)
        self.assertEqual(pdv.pagamentos_parciais, [])
        self.assertEqual(pdv.ent_valor_pago.value, "60")
        self.assertEqual(pdv.gaveta_calls, [])

    def test_non_cash_overpayment_is_rejected_without_mutation(self):
        for forma in ("PIX", "DEBITO", "CREDITO", "VOUCHER"):
            with self.subTest(forma=forma):
                pdv = _FakePDV()
                pdv.forma_pagamento_selecionada = forma
                pdv.ent_valor_pago.value = "60"
                before_troco = pdv.lbl_troco_venda.text

                ModuloPDV.acrescentar_valor_pago(pdv)

                self.assertEqual(pdv.valor_pago_acumulado, 0.0)
                self.assertEqual(pdv.pagamentos_parciais, [])
                self.assertEqual(pdv.ent_valor_pago.value, "60")
                self.assertEqual(pdv.lbl_troco_venda.text, before_troco)
                self.assertEqual(pdv.gaveta_calls, [])

    def test_cash_then_pix_exact_total_is_mixed(self):
        pdv = self._confirmar_parcela("DINHEIRO", 30)
        self._confirmar_parcela("PIX", 20, pdv)
        self.assertEqual(pdv._resolver_forma_pagamento_registro(), "MISTO")
        self.assertEqual(pdv.valor_pago_acumulado, 50.0)
        self.assertEqual(pdv.lbl_troco_venda.text, "0.00")
        self.assertEqual(pdv.pagamentos_parciais, [("DINHEIRO", 30.0), ("PIX", 20.0)])
        self.assertEqual(pdv.gaveta_calls, ["DINHEIRO"])

    def test_cash_then_pix_over_total_has_change_and_one_drawer_call(self):
        pdv = self._confirmar_parcela("DINHEIRO", 30)
        self._confirmar_parcela("PIX", 25, pdv)
        self.assertEqual(pdv._resolver_forma_pagamento_registro(), "MISTO")
        self.assertEqual(pdv.lbl_troco_venda.text, "5.00")
        self.assertEqual(pdv.gaveta_calls, ["DINHEIRO"])

    def test_pix_then_card_over_total_rejects_second_installment(self):
        pdv = self._confirmar_parcela("PIX", 30)
        pdv.forma_pagamento_selecionada = "CREDITO"
        pdv.ent_valor_pago.value = "25"

        ModuloPDV.acrescentar_valor_pago(pdv)

        self.assertEqual(pdv.valor_pago_acumulado, 30.0)
        self.assertEqual(pdv.pagamentos_parciais, [("PIX", 30.0)])
        self.assertEqual(pdv.lbl_troco_venda.text, "0.00")
        self.assertEqual(pdv.gaveta_calls, [])

    def test_shared_troco_rule_rejects_direct_non_cash_overpayment(self):
        self.assertEqual(_avaliar_troco_pagamento(50, 60, [("PIX", 60)]), (False, 0.0))
        self.assertEqual(_avaliar_troco_pagamento(50, 60, [("CREDITO", 60)]), (False, 0.0))
        self.assertEqual(_avaliar_troco_pagamento(50, 55, [("DINHEIRO", 30), ("PIX", 25)]), (True, 5.0))

    def test_direct_finalization_rejects_pix_overpayment_before_database(self):
        pdv = _FakePDV()
        with patch.object(modulo_pdv, "get_db_connection", side_effect=AssertionError("database access")):
            ModuloPDV.finalizar_venda_pdv(pdv, "PIX", valor_pago=60)
        self.assertEqual(pdv.gaveta_calls, [])
        self.assertTrue(any("excesso" in mensagem for mensagem in pdv.statuses))

    def test_direct_cash_finalization_validates_without_opening_drawer(self):
        pdv = _FakePDV()
        pdv.forma_pagamento_selecionada = "PIX"
        with patch.object(modulo_pdv, "get_db_connection", side_effect=RuntimeError("blocked by test")):
            with patch.object(modulo_pdv.modulo_financeiro, "obter_taxas", return_value={}):
                ModuloPDV.finalizar_venda_pdv(pdv, "DINHEIRO", valor_pago=50)
        self.assertEqual(pdv.formas_resolvidas[0], "DINHEIRO")
        self.assertEqual(pdv.gaveta_calls, [])

    def test_finalization_after_cash_confirmation_does_not_open_again(self):
        pdv = self._confirmar_parcela("DINHEIRO", 60)
        with patch.object(modulo_pdv, "get_db_connection", side_effect=RuntimeError("blocked by test")):
            with patch.object(modulo_pdv.modulo_financeiro, "obter_taxas", return_value={}):
                ModuloPDV.finalizar_venda_pdv(pdv, "DINHEIRO", valor_pago=60)
        self.assertEqual(pdv.gaveta_calls, ["DINHEIRO"])

    def test_direct_delivery_pix_keeps_explicit_form_and_does_not_open_drawer(self):
        pdv = _FakePDV()
        with patch.object(modulo_pdv, "get_db_connection", side_effect=RuntimeError("blocked by test")):
            with patch.object(modulo_pdv.modulo_financeiro, "obter_taxas", return_value={}):
                ModuloPDV.finalizar_venda_pdv(pdv, "PIX", valor_pago=50)
        self.assertEqual(pdv.formas_resolvidas[0], "PIX")
        self.assertEqual(pdv.gaveta_calls, [])

    def test_delivery_approved_pix_passes_pix_and_rejects_excess(self):
        pdv = _FakePDV()
        ModuloPDV._aplicar_pedido_delivery(
            pdv,
            {"itens": [{"nome": "Produto"}], "pagamento_aprovado": True, "valor": 50},
        )
        self.assertEqual(pdv.delivery_calls[0][0][0], "PIX")
        self.assertEqual(pdv.delivery_calls[0][1]["valor_pago"], 50.0)

        excesso = _FakePDV()
        ModuloPDV._aplicar_pedido_delivery(
            excesso,
            {"itens": [{"nome": "Produto"}], "pagamento_aprovado": True, "valor": 60},
        )
        self.assertEqual(excesso.delivery_calls, [])
        self.assertEqual(excesso.gaveta_calls, [])

    def test_delivery_missing_amount_uses_resolved_total(self):
        pdv = _FakePDV()
        ModuloPDV._aplicar_pedido_delivery(
            pdv,
            {"itens": [{"nome": "Produto"}], "pagamento_aprovado": True},
        )
        self.assertEqual(pdv.delivery_calls[0][1]["valor_pago"], 50.0)

    def test_explicit_delivery_form_is_preserved_without_installments(self):
        pdv = _FakePDV()
        self.assertEqual(pdv._resolver_forma_pagamento_registro("PIX"), "PIX")
        self.assertEqual(pdv._resolver_forma_pagamento_registro("DINHEIRO"), "DINHEIRO")

    def test_print_automation_does_not_open_drawer(self):
        pdv = _FakePDV()
        ModuloPDV._executar_automacao_pos_venda(pdv, {"forma_pagamento": "DINHEIRO"})
        self.assertEqual(len(pdv.print_calls), 1)
        self.assertEqual(pdv.gaveta_calls, [])

    def test_cupom_prints_mixed_installments_and_valid_change_without_drawer(self):
        pdv = _FakePDV()
        ModuloPDV.imprimir_cupom(
            pdv,
            {
                "itens": [{"nome": "Produto", "quantidade": 1, "total": 50}],
                "total": 50,
                "forma_pagamento": "MISTO",
                "pagamentos": [("DINHEIRO", 30), ("PIX", 25)],
                "valor_recebido": 55,
            },
        )
        payload = pdv.raw_prints[0]
        self.assertIn(b"DINHEIRO: 30.00", payload)
        self.assertIn(b"PIX: 25.00", payload)
        self.assertIn(b"TROCO: 5.00", payload)
        self.assertEqual(pdv.gaveta_calls, [])

    def test_finalizer_and_print_automation_do_not_request_drawer(self):
        source = inspect.getsource(ModuloPDV.finalizar_venda_pdv)
        self.assertEqual(source.count("_acionar_gaveta_pos_venda("), 0)
        print_call = source.index("self._executar_automacao_pos_venda(")
        cleanup_call = source.index("self.limpar_pagamentos_recebidos()", print_call)
        self.assertLess(print_call, cleanup_call)
        self.assertNotIn("_acionar_gaveta_pos_venda(", source[print_call:cleanup_call])

        print_source = inspect.getsource(ModuloPDV._executar_automacao_pos_venda)
        self.assertNotIn("_acionar_gaveta_pos_venda(", print_source)

    def test_cleanup_clears_receipt_and_change_after_sale_flow(self):
        pdv = _FakePDV()
        pdv.valor_pago_acumulado = 60.0
        pdv.pagamentos_parciais = [("DINHEIRO", 60.0)]
        pdv.ent_valor_pago.value = "60"
        pdv.lbl_troco_venda.text = "10.00"

        ModuloPDV.limpar_pagamentos_recebidos(pdv)

        self.assertEqual(pdv.valor_pago_acumulado, 0.0)
        self.assertEqual(pdv.pagamentos_parciais, [])
        self.assertEqual(pdv.ent_valor_pago.value, "")
        self.assertEqual(pdv.lbl_troco_venda.text, "0.00")

    def test_end_and_delete_are_not_rebound_globally(self):
        source = Path(modulo_pdv.__file__).read_text(encoding="utf-8")
        self.assertNotIn("bind_all(", source)
        self.assertNotIn("bind_class(", source)
        self.assertNotIn("_registrar_foco_central_pdv", source)

    def test_test_appdata_does_not_contain_a_database(self):
        from database_manager import get_db_path

        self.assertFalse(Path(get_db_path()).exists())


if __name__ == "__main__":
    unittest.main()