import sqlite3
from datetime import datetime, timedelta
from database_manager import get_db_connection, registrar_log

def verificar_alertas():
    """
    Analisa o banco de dados em busca de produtos com estoque baixo ou validade próxima.
    Retorna uma lista de dicionários com os alertas encontrados.
    """
    alertas = []
    db_path = 'mercado.db'
    conn = None
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Verificar Estoque Crítico (Ex: menos de 5 unidades)
            cursor.execute("SELECT nome, quantidade_atual FROM produtos WHERE quantidade_atual < 5")
            estoque_baixo = cursor.fetchall()
            for item in estoque_baixo:
                alertas.append({"tipo": "Estoque Baixo", "produto": item[0], "detalhe": f"{item[1]} unid."})
                
            # 2. Verificar Validade Próxima (Ex: próximos 15 dias)
            data_limite = (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d')
            cursor.execute("SELECT nome, validade FROM produtos WHERE validade <= ? AND validade != ''", (data_limite,))
            vencendo = cursor.fetchall()
            for item in vencendo:
                alertas.append({"tipo": "Validade Próxima", "produto": item[0], "detalhe": item[1]})
    except Exception as e:
        registrar_log(None, "Verificação de Alertas IA", "Falha", f"Erro: {e}")
        print(f"Erro na IA de Gestão: {e}")
        
    return alertas

def aplicar_promocao(id_produto, novo_preco, dias_duracao):
    """Aplica um preço promocional salvando o original."""
    hoje = datetime.now()
    data_fim = (hoje + timedelta(days=dias_duracao)).strftime('%Y-%m-%d')
    data_inicio = hoje.strftime('%Y-%m-%d')
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Só move para preco_base se for a primeira promoção (evita perder o preço real)
            cursor.execute("""
                UPDATE produtos 
                SET preco_base = COALESCE(preco_base, preco_venda),
                    preco_venda = ?,
                    inicio_promocao = ?,
                    fim_promocao = ?
                WHERE id = ?
            """, (novo_preco, data_inicio, data_fim, id_produto))
            registrar_log(None, "Promoção Dinâmica", "Sucesso", f"Promoção ativada para ID {id_produto} até {data_fim}")
    except Exception as e:
        registrar_log(None, "Promoção Dinâmica", "Falha", f"Erro ao aplicar: {e}")

def restaurar_precos_originais():
    """Restaura preços de produtos cujas promoções expiraram."""
    hoje = datetime.now().strftime('%Y-%m-%d')
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE produtos 
                SET preco_venda = preco_base, preco_base = NULL, inicio_promocao = NULL, fim_promocao = NULL
                WHERE fim_promocao < ? AND preco_base IS NOT NULL
            """, (hoje,))
    except Exception as e:
        print(f"Erro ao restaurar preços: {e}")

def analisar_performance_15_dias():
    """
    Realiza análise profunda de performance e gera relatório consultivo.
    Calcula margens, perdas e gargalos.
    """
    hoje = datetime.now()
    quinze_dias_atras = (hoje - timedelta(days=15)).strftime('%Y-%m-%d')
    trinta_dias_atras = (hoje - timedelta(days=30)).strftime('%Y-%m-%d')
    
    conselho = "### 📊 RELATÓRIO DE MENTORIA FRS ###\n\n"
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Margem de Lucro Bruta e Líquida (Últimos 15 dias)
            cursor.execute("""
                SELECT 
                    SUM(v.valor_total) as receita,
                    SUM(iv.quantidade * p.preco_custo) as custo_mercadoria
                FROM vendas v
                JOIN itens_venda iv ON v.id = iv.venda_id
                JOIN produtos p ON iv.produto_id = p.id
                WHERE v.data_venda >= ?
            """, (quinze_dias_atras,))
            res_vendas = cursor.fetchone()
            receita = res_vendas[0] or 0.0
            cmv = res_vendas[1] or 0.0
            
            cursor.execute("SELECT SUM(valor) FROM financeiro WHERE tipo = 'Saída' AND data_registro >= ?", (quinze_dias_atras,))
            despesas = cursor.fetchone()[0] or 0.0
            
            lucro_bruto = receita - cmv
            lucro_liquido = receita - cmv - despesas
            margem_liq = (lucro_liquido / receita * 100) if receita > 0 else 0
            
            conselho += f"Sr. Dono, nos últimos 15 dias sua Margem Líquida foi de {margem_liq:.1f}%.\n"
            if margem_liq < 15:
                conselho += "⚠️ Cuidado: Sua margem está apertada. Recomendo revisar as despesas fixas ou ajustar preços de itens curva C.\n\n"
            else:
                conselho += "✅ Excelente performance! Sua operação está saudável.\n\n"

            # 2. Cálculo de Perda (Produtos Vencidos)
            cursor.execute("SELECT COUNT(*), SUM(quantidade_atual * preco_custo) FROM produtos WHERE validade < DATE('now')")
            perda_res = cursor.fetchone()
            if perda_res[0] > 0:
                conselho += f"❗ Alerta de Perda: Detectei {perda_res[0]} itens vencidos, gerando um prejuízo de R$ {perda_res[1]:.2f}. "
                conselho += "Vamos treinar a equipe no método PVPS (Primeiro que Vence, Primeiro que Sai)?\n\n"

            # 3. Identificação de Gargalos (Estoque parado > 30 dias)
            cursor.execute("""
                SELECT nome, quantidade_atual FROM produtos 
                WHERE quantidade_atual > 0 
                AND id NOT IN (
                    SELECT iv.produto_id FROM itens_venda iv 
                    JOIN vendas v ON iv.venda_id = v.id 
                    WHERE v.data_venda >= ?
                ) LIMIT 3
            """, (trinta_dias_atras,))
            gargalos = cursor.fetchall()
            
            if gargalos:
                conselho += "📉 Gargalos Detectados:\n"
                for g in gargalos:
                    conselho += f"- O produto '{g[0]}' está sem saída há mais de 30 dias ({g[1]} em estoque). "
                    conselho += "Que tal uma promoção relâmpago de 15% para girar esse capital?\n"
                conselho += "\n"

            # 4. Salvar histórico
            cursor.execute("INSERT INTO logs_mentoria (conselho) VALUES (?)", (conselho,))
            
        return conselho

    except Exception as e:
        registrar_log(None, "IA Mentora", "Falha", str(e))
        return "Sr. Dono, tive uma falha ao processar os dados. Verifique os logs de sistema."

def verificar_ultimo_conselho_15_dias():
    """Verifica se já se passaram 15 dias desde o último conselho."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM logs_mentoria ORDER BY id DESC LIMIT 1")
            res = cursor.fetchone()
            if not res: return True
            
            ultimo_data = datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S')
            return (datetime.now() - ultimo_data).days >= 15
    except: return True