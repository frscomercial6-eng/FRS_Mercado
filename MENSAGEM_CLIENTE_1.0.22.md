# Mensagem ao cliente — FRS Mercado 1.0.22 (pronta para envio)

Assunto: FRS Mercado 1.0.22 disponível para download manual

Olá,

A versão **1.0.22** do FRS Mercado já está publicada e pronta para uso. Como esta versão é distribuída de forma **manual**, baixe pelo canal oficial abaixo:

- Instalação pelo **Setup** (recomendado): https://github.com/frscomercial6-eng/FRS_Mercado/releases/download/v1.0.22/FRS_Mercado_Setup.exe
- Instalação **portátil** (opcional): https://github.com/frscomercial6-eng/FRS_Mercado/releases/download/v1.0.22/FRS_Mercado_Portable_1.0.22.zip

Siga **apenas** o passo a passo do tipo de instalação que você já usa. Em **ambos os casos, faça o backup primeiro** e guarde o arquivo em local seguro.

---

## Opção A — Instalação pelo Setup (instalador)

1. Faça um **backup** dos seus dados (menu de backup do sistema) e guarde o arquivo em local seguro.
2. Execute o **Setup oficial da versão 1.0.22** (`FRS_Mercado_Setup.exe`).
3. **Mantenha a instalação anterior** no lugar. Após instalar, **abra o programa** e confirme que **licença, CNPJ, cadastros e dados** estão presentes antes de encerrar ou apagar o backup antigo.

---

## Opção B — Instalação portátil (Portable)

Atenção: no modo portátil os dados ficam **dentro da própria pasta**, na pasta `data`. Uma extração mal feita pode sobrescrever seus dados. Siga exatamente:

1. Faça um **backup completo** dos seus dados antes de qualquer coisa e guarde o arquivo em local seguro.
2. **Extraia o ZIP da versão 1.0.22 em uma pasta NOVA**, sem apagar a pasta antiga.
3. **Nunca extraia por cima da instalação antiga aceitando a substituição da pasta `data`.**
4. **Nunca substituir nem excluir** `data\mercado.db`, os arquivos de licença ou as configurações persistentes.
5. Abra a **nova** versão (pasta nova) e confirme que **licença, CNPJ, cadastros e dados** estão presentes antes de considerar a atualização concluída.
6. Se os dados **não aparecerem**: **feche o programa** e **preserve as duas pastas** (a antiga e a nova). **Não** inicialize uma base vazia nem sobrescreva arquivos. Chame o suporte.

---

## Depois de atualizar (nas duas opções)

Confirme que está tudo certo:
- O **licenciamento/ativação** continua válido.
- O **CNPJ** e as **configurações** permanecem corretos.
- Os **cadastros, produtos, vendas e estoque** estão presentes.
- O **PDV e o fechamento de caixa** funcionam normalmente.

Se algo aparecer diferente após a atualização, **não continue usando** e nos avise com uma imagem da tela — manteremos o backup anterior até você confirmar que está tudo certo.

Atenciosamente,
Suporte FRS Solutions

---
Observação interna (não enviar ao cliente): links verificados em 11/10/2026 (HTTP 200); `releases/latest` aponta para v1.0.22; SHA-256 do Setup `D2661ECE...9797`, Portable `91AA6524...54343`. Preservação: no Setup os dados ficam em `%APPDATA%\FRS_Mercado\data` (fora da pasta do programa, que o instalador não toca — `Excludes: data\*`); no Portable os dados ficam na pasta `data` junto ao executável, por isso a extração deve ser em pasta NOVA. **Não há backup nem rollback automático comprovado** — o backup é manual, pelo menu do sistema. O ZIP portátil embute um `data\mercado.db` vazio; substituí-lo por cima do existente apaga dados e licença.
