import re


def detect_query_intent(query):

    q = query.lower()

    intents = []

    if re.search(r"quantas vezes|frequência|ocorrências", q):
        intents.append("term_frequency")

    if re.search(r"em quais documentos|quais documentos|onde aparece", q):
        intents.append("term_documents")

    if re.search(r"o que é|defina|explique|qual o", q):
        intents.append("rag_query")

    if not intents:
        intents.append("rag_query")

    return intents