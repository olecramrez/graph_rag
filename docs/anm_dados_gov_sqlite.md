# Importacao de bases ANM do dados.gov.br

O script `tools/download_anm_dados_gov_to_sqlite.py` busca conjuntos de dados no
catalogo `dados.gov.br`, baixa os recursos e transforma arquivos tabulares em
tabelas SQLite.

## Token

Em 2026-05-16, os endpoints JSON do `dados.gov.br` retornaram `401` sem token
`Bearer`. O script funciona sem token usando a fonte oficial direta da ANM
(`https://dadosabertos.anm.gov.br`). Com token, ele tenta primeiro o catalogo do
`dados.gov.br`.

Para usar o catalogo do `dados.gov.br`, gere um token de consumidor no perfil do
portal e informe por variavel de ambiente:

```powershell
$env:DADOS_GOV_BR_TOKEN = "SEU_TOKEN"
```

ou por parametro:

```powershell
python tools\download_anm_dados_gov_to_sqlite.py --token "SEU_TOKEN"
```

## Uso principal

Sem token:

```powershell
python tools\download_anm_dados_gov_to_sqlite.py --source anm-direct
```

Modo automatico: tenta `dados.gov.br`; se receber `401`, cai para
`dadosabertos.anm.gov.br`.

```powershell
python tools\download_anm_dados_gov_to_sqlite.py --fetch-details
```

Por padrao, a saida vai para a base persistente `anm_dados_gov` configurada pelo
projeto:

- SQLite: `data/anm_dados_gov/anm_dados_gov.sqlite` dentro da raiz compartilhada.
- Arquivos baixados: `data/anm_dados_gov/files/`.

Tambem e possivel escolher caminhos explicitamente:

```powershell
python tools\download_anm_dados_gov_to_sqlite.py `
  --output-dir C:\dados\anm_dados_gov `
  --db-path C:\dados\anm_dados_gov\anm.sqlite `
  --fetch-details
```

## Teste rapido

```powershell
python tools\download_anm_dados_gov_to_sqlite.py --metadata-only --max-pages 1
```

Para testar importacao sem carregar bases inteiras:

```powershell
python tools\download_anm_dados_gov_to_sqlite.py --source anm-direct --max-dirs 3 --max-rows 1000
```

## Estrutura do SQLite

Metadados fixos:

- `import_runs`: historico de execucoes.
- `datasets`: conjuntos encontrados no catalogo.
- `resources`: recursos de cada conjunto, URL e tabela importada.
- `import_errors`: erros por recurso, sem interromper a carga toda.

Dados importados:

- Cada CSV/XLSX/XLS/JSON/GeoJSON/Parquet vira uma tabela `anm_*`.
- ZIPs sao extraidos e cada arquivo tabular interno vira uma tabela propria.

Formatos nao tabulares ficam baixados e registrados em `resources`, mas nao viram
tabela de dados.

## Consultas no app

O chat aceita prefixos explicitos para evitar erro de roteamento:

```text
@cnpj 00.788.023/0001-02 socios
@anm quais recursos de CFEM foram importados?
@anm amostra da tabela CFEM
@rag quais normas tratam de barragens?
```

`@anm` consulta o SQLite criado por este importador. Se o arquivo estiver fora do
caminho padrao, defina:

```powershell
$env:ANM_SQLITE_PATH = "Z:\Graph_rag\anm_sqlite\anm_dados_gov.sqlite"
```

## SQL Agent

O comando `@anm` tambem possui uma camada inicial de SQL Agent:

- descobre automaticamente tabelas e colunas do SQLite;
- detecta entidades como CNPJ, CPF, processo, barragem, municipio, substancia e CFEM;
- classifica consultas estruturadas como `sql_only`;
- gera SQL SQLite via LLM;
- aceita apenas `SELECT` ou `WITH`;
- bloqueia `DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `ATTACH`, `PRAGMA` e comandos semelhantes;
- aplica `LIMIT`, timeout e autorizador SQLite somente-leitura;
- consolida o resultado com LLM quando disponivel.

Exemplos:

```text
@anm esquema da base
@anm quais tabelas foram importadas?
@anm total de CFEM por municipio
@anm quais barragens existem por UF?
```

Se a base tiver apenas metadados, rode o importador sem `--metadata-only` para
criar tabelas de dados consultaveis pelo SQL Agent.
