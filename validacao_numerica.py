def _limpar_texto_numerico(texto):
    bruto = "" if texto is None else str(texto)
    return bruto.strip().replace("R$", "").replace(" ", "")


def _normalizar_para_float_str(texto):
    texto_limpo = _limpar_texto_numerico(texto)
    if not texto_limpo:
        return ""

    permitido = "0123456789-.,"
    filtrado = "".join(ch for ch in texto_limpo if ch in permitido)
    if not filtrado:
        return ""

    negativo = filtrado.startswith("-")
    sem_sinal = filtrado[1:] if negativo else filtrado

    if "," in sem_sinal and "." in sem_sinal:
        if sem_sinal.rfind(",") > sem_sinal.rfind("."):
            sem_sinal = sem_sinal.replace(".", "").replace(",", ".")
        else:
            sem_sinal = sem_sinal.replace(",", "")
    elif "," in sem_sinal:
        sem_sinal = sem_sinal.replace(".", "").replace(",", ".")
    elif sem_sinal.count(".") > 1:
        partes = sem_sinal.split(".")
        sem_sinal = "".join(partes[:-1]) + "." + partes[-1]

    return ("-" if negativo else "") + sem_sinal


def parse_numero(
    texto,
    nome_campo,
    permitir_vazio=False,
    default=0.0,
    inteiro=False,
    minimo=0,
    maximo=None,
):
    valor_str = _normalizar_para_float_str(texto)
    if not valor_str or valor_str in {"-", ".", "-."}:
        if permitir_vazio:
            return int(default) if inteiro else float(default)
        raise ValueError(f"{nome_campo} inválido, por favor verifique os campos numéricos.")

    try:
        valor = float(valor_str)
    except (TypeError, ValueError):
        raise ValueError(f"{nome_campo} inválido, por favor verifique os campos numéricos.")

    if minimo is not None and valor < minimo:
        raise ValueError(f"{nome_campo} inválido, por favor verifique os campos numéricos.")

    if maximo is not None and valor > maximo:
        raise ValueError(f"{nome_campo} inválido, por favor verifique os campos numéricos.")

    if inteiro:
        if not valor.is_integer():
            raise ValueError(f"{nome_campo} inválido, por favor verifique os campos numéricos.")
        return int(valor)

    return valor


def _normalizar_texto_entrada(texto, inteiro=False, casas_decimais=2):
    bruto = _limpar_texto_numerico(texto)
    if not bruto:
        return ""

    resultado = []
    separador_usado = False

    for ch in bruto:
        if ch.isdigit():
            resultado.append(ch)
            continue

        if not inteiro and ch in ",." and not separador_usado:
            if not resultado:
                resultado.append("0")
            resultado.append(",")
            separador_usado = True

    texto_final = "".join(resultado)

    if not inteiro and "," in texto_final and casas_decimais is not None:
        parte_int, parte_dec = texto_final.split(",", 1)
        texto_final = f"{parte_int},{parte_dec[:casas_decimais]}"

    return texto_final


def aplicar_padrao_entrada_numerica(entry, inteiro=False, casas_decimais=2):
    def _set_texto(valor):
        entry.delete(0, "end")
        entry.insert(0, valor)

    def _ao_digitar(_event=None):
        atual = entry.get()
        normalizado = _normalizar_texto_entrada(atual, inteiro=inteiro, casas_decimais=casas_decimais)
        if atual != normalizado:
            _set_texto(normalizado)

    def _ao_sair(_event=None):
        atual = entry.get().strip()
        if not atual:
            return

        try:
            valor = parse_numero(
                atual,
                "Valor",
                permitir_vazio=True,
                default=0,
                inteiro=inteiro,
                minimo=0,
            )
        except ValueError:
            _set_texto(_normalizar_texto_entrada(atual, inteiro=inteiro, casas_decimais=casas_decimais))
            return

        if inteiro:
            _set_texto(str(int(valor)))
            return

        casas = 2 if casas_decimais is None else casas_decimais
        formatado = f"{float(valor):.{casas}f}".replace(".", ",")
        _set_texto(formatado)

    entry.bind("<KeyRelease>", _ao_digitar, add="+")
    entry.bind("<FocusOut>", _ao_sair, add="+")
