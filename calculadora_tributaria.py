from datetime import date, datetime

from database_manager import get_db_connection


class CalculadoraTributaria:
    """
    Motor tributario com suporte ao regime atual e ao IVA Dual (IBS/CBS).
    Regra de transicao: a partir de 2027-01-01 prioriza IBS/CBS.
    """

    DATA_CORTE_IVA_DUAL = date(2027, 1, 1)

    def _normalizar_data(self, data_operacao):
        if isinstance(data_operacao, datetime):
            return data_operacao.date()
        if isinstance(data_operacao, date):
            return data_operacao
        if isinstance(data_operacao, str) and data_operacao.strip():
            txt = data_operacao.strip()
            for formato in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(txt, formato).date()
                except ValueError:
                    continue
        return date.today()

    def _to_float(self, valor, default=0.0):
        try:
            return float(valor)
        except Exception:
            return float(default)

    def _normalizar_ncm(self, ncm):
        return "".join(ch for ch in str(ncm or "") if ch.isdigit())

    def _obter_aliquotas_produto(self, aliquotas_produto):
        base = {
            "icms": 0.0,
            "pis": 0.0,
            "cofins": 0.0,
            "ibs": 0.0,
            "cbs": 0.0,
        }
        if not isinstance(aliquotas_produto, dict):
            return base

        base["icms"] = self._to_float(aliquotas_produto.get("icms", aliquotas_produto.get("aliquota_icms", 0.0)))
        base["pis"] = self._to_float(aliquotas_produto.get("pis", aliquotas_produto.get("aliquota_pis", 0.0)))
        base["cofins"] = self._to_float(aliquotas_produto.get("cofins", aliquotas_produto.get("aliquota_cofins", 0.0)))
        base["ibs"] = self._to_float(aliquotas_produto.get("ibs", aliquotas_produto.get("aliquota_ibs", 0.0)))
        base["cbs"] = self._to_float(aliquotas_produto.get("cbs", aliquotas_produto.get("aliquota_cbs", 0.0)))
        return base

    def _buscar_aliquotas_por_ncm(self, ncm):
        ncm_norm = self._normalizar_ncm(ncm)
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        ncm_prefixo,
                        aliquota_icms,
                        aliquota_pis,
                        aliquota_cofins,
                        aliquota_ibs,
                        aliquota_cbs
                    FROM config_aliquotas_fiscais_ncm
                    WHERE ativo = 1
                    ORDER BY CASE WHEN ncm_prefixo = '*' THEN 0 ELSE LENGTH(ncm_prefixo) END DESC
                    """
                )
                regras = cursor.fetchall()
        except Exception:
            regras = []

        fallback_novo = None
        for prefixo, icms, pis, cofins, ibs, cbs in regras:
            prefixo_txt = str(prefixo or "").strip()
            dados = {
                "icms": self._to_float(icms),
                "pis": self._to_float(pis),
                "cofins": self._to_float(cofins),
                "ibs": self._to_float(ibs),
                "cbs": self._to_float(cbs),
            }
            if prefixo_txt == "*":
                fallback_novo = dados
                continue
            if ncm_norm.startswith(prefixo_txt):
                return dados

        if fallback_novo and any(self._to_float(v) > 0 for v in fallback_novo.values()):
            return fallback_novo

        # Fallback para regra legada (aliquota_percentual) para manter compatibilidade.
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT ncm_prefixo, aliquota_percentual
                    FROM config_aliquotas_ncm
                    WHERE ativo = 1
                    ORDER BY CASE WHEN ncm_prefixo = '*' THEN 0 ELSE LENGTH(ncm_prefixo) END DESC
                    """
                )
                regras_legadas = cursor.fetchall()
        except Exception:
            regras_legadas = []

        fallback_legacy = None
        for prefixo, aliquota in regras_legadas:
            prefixo_txt = str(prefixo or "").strip()
            dados_legacy = {
                "icms": self._to_float(aliquota),
                "pis": 0.0,
                "cofins": 0.0,
                "ibs": 0.0,
                "cbs": 0.0,
            }
            if prefixo_txt == "*":
                fallback_legacy = dados_legacy
                continue
            if ncm_norm.startswith(prefixo_txt):
                return dados_legacy

        if fallback_legacy:
            return fallback_legacy

        if fallback_novo:
            return fallback_novo

        return {
            "icms": 0.0,
            "pis": 0.0,
            "cofins": 0.0,
            "ibs": 0.0,
            "cbs": 0.0,
        }

    def calcular_impostos(self, valor_produto, data_operacao, ncm="", aliquotas_produto=None):
        base_calculo = max(self._to_float(valor_produto), 0.0)
        data_ref = self._normalizar_data(data_operacao)

        aliquotas_ncm = self._buscar_aliquotas_por_ncm(ncm)
        aliquotas_item = self._obter_aliquotas_produto(aliquotas_produto)

        aliquotas = {
            "icms": aliquotas_item["icms"] if aliquotas_item["icms"] > 0 else aliquotas_ncm["icms"],
            "pis": aliquotas_item["pis"] if aliquotas_item["pis"] > 0 else aliquotas_ncm["pis"],
            "cofins": aliquotas_item["cofins"] if aliquotas_item["cofins"] > 0 else aliquotas_ncm["cofins"],
            "ibs": aliquotas_item["ibs"] if aliquotas_item["ibs"] > 0 else aliquotas_ncm["ibs"],
            "cbs": aliquotas_item["cbs"] if aliquotas_item["cbs"] > 0 else aliquotas_ncm["cbs"],
        }

        usar_iva_dual = data_ref >= self.DATA_CORTE_IVA_DUAL
        regime = "IVA_DUAL" if usar_iva_dual else "ATUAL"

        valores = {
            "icms": 0.0,
            "pis": 0.0,
            "cofins": 0.0,
            "ibs": 0.0,
            "cbs": 0.0,
        }

        if usar_iva_dual:
            valores["ibs"] = round(base_calculo * (aliquotas["ibs"] / 100.0), 2)
            valores["cbs"] = round(base_calculo * (aliquotas["cbs"] / 100.0), 2)
        else:
            valores["icms"] = round(base_calculo * (aliquotas["icms"] / 100.0), 2)
            valores["pis"] = round(base_calculo * (aliquotas["pis"] / 100.0), 2)
            valores["cofins"] = round(base_calculo * (aliquotas["cofins"] / 100.0), 2)

        imposto_total = round(sum(valores.values()), 2)
        valor_liquido = round(max(base_calculo - imposto_total, 0.0), 2)
        aliquota_total = round(sum(aliquotas.values()), 4)

        return {
            "regime": regime,
            "base_calculo": base_calculo,
            "aliquotas": aliquotas,
            "valores": valores,
            "aliquota_total": aliquota_total,
            "imposto_total": imposto_total,
            "valor_liquido": valor_liquido,
            # Compatibilidade com chamadas antigas do PDV
            "aliquota": aliquota_total,
            "valor_imposto": imposto_total,
        }
