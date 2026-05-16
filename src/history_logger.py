from pathlib import Path
import re
from datetime import datetime


# =====================================================
# CONFIGURAÇÃO DE DIRETÓRIO
# =====================================================
PROJECT_NAME = Path(__file__).resolve().parents[1].name
USER_ROOT = Path.home() / PROJECT_NAME
LOG_DIR = USER_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "historico_respostas.txt"


# =====================================================
# EXTRAI SOMENTE BLOCO DA RESPOSTA (SEM EVIDÊNCIAS)
# =====================================================
def extrair_resposta_completa(markdown):

    inicio = markdown.find("# 📘 Resposta")
    fim = markdown.find("# 📎 Evidência")

    if inicio == -1:
        return markdown.strip()

    if fim == -1:
        bloco = markdown[inicio:]
    else:
        bloco = markdown[inicio:fim]

    # Remove o título "# 📘 Resposta"
    bloco = bloco.replace("# 📘 Resposta", "").strip()

    return bloco


# =====================================================
# DESCOBRE PRÓXIMO NÚMERO
# =====================================================
def obter_proximo_numero():

    if not LOG_FILE.exists():
        return 1

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        conteudo = f.read()
        numeros = re.findall(r"Pergunta (\d+):", conteudo)

        if not numeros:
            return 1

        return int(numeros[-1]) + 1


# =====================================================
# SALVA RESPOSTA COMPLETA
# =====================================================
def salvar_resposta(pergunta, resposta_markdown):

    numero = obter_proximo_numero()
    resposta_limpa = extrair_resposta_completa(resposta_markdown)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 100 + "\n\n")
        f.write(f"Pergunta {numero}:\n")
        f.write(pergunta + "\n\n")
        f.write("Resposta:\n\n")
        f.write(resposta_limpa + "\n\n")
        f.write(f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")