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
