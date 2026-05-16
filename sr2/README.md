# Rotina ANMlegis

Baixa atos normativos da ANMlegis, salva o HTML original do ato, gera o PDF pelo
endpoint de PDF do próprio site e grava metadados para enriquecimento dos chunks.

## Teste rápido

```powershell
python .\sr2\anm_normativos_downloader.py --menu-contains Resolu --year-from 2025 --year-to 2025 --limit 3
```

## Baixar tudo que for descoberto

```powershell
python .\sr2\anm_normativos_downloader.py
```

Saídas padrão:

- `sr2\data\pdf`: PDFs gerados pelo site, com nome extraído do título no HTML.
- `sr2\data\html`: HTML original de cada ato.
- `sr2\data\metadados.csv`: metadados principais para enriquecer chunks.
- `sr2\data\metadados.jsonl`: mesma informação em JSONL.
- `sr2\data\rastreamento_dispositivos.csv`: eventos por dispositivo, com caminho
  hierárquico como `Artigo Art. 10 > Inciso XII`.
- `sr2\data\rastreamento_dispositivos.jsonl`: mesma árvore/eventos em JSONL.
- `sr2\data\atos_descobertos.csv`: inventário dos atos encontrados antes do download.

O campo `rastreamento_dispositivos_json`, também presente em `metadados.csv`,
guarda os eventos detectados dentro do HTML, incluindo revogação parcial,
alteração e inclusão quando aparecem em notas como `Revogado pela`,
`Alterado pela` ou `Redação dada pela`.

## Opções úteis

```powershell
python .\sr2\anm_normativos_downloader.py --print-menus --discover-only
python .\sr2\anm_normativos_downloader.py --menu-contains Portarias --year-from 2024 --year-to 2026
python .\sr2\anm_normativos_downloader.py --menu-url "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_modulo=566&cod_menu=6675"
```

O parâmetro `--sleep` controla a pausa entre requisições. Para rodadas grandes,
use um valor conservador, por exemplo `--sleep 1`.
