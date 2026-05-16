# Importacao de bases ANM do dados.gov.br

O script `tools/download_anm_dados_gov_to_sqlite.py` busca conjuntos de dados no
catalogo `dados.gov.br`, baixa os recursos e transforma arquivos tabulares em
tabelas SQLite.

## Token

Em 2026-05-16, os endpoints do portal retornaram `401` sem token `Bearer`.
Gere um token de consumidor no perfil do dados.gov.br e informe por variavel de
ambiente:

```powershell
$env:DADOS_GOV_BR_TOKEN = "SEU_TOKEN"
```

ou por parametro:

```powershell
python tools\download_anm_dados_gov_to_sqlite.py --token "SEU_TOKEN"
```

## Uso principal

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
python tools\download_anm_dados_gov_to_sqlite.py --max-pages 1 --max-rows 1000
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
