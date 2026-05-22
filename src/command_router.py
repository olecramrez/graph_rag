import re


COMMAND_ALIASES = {
    "cnpj": "cnpj",
    "cnpjs": "cnpj",
    "anm": "anm",
    "minera": "anm",
    "mineracao": "anm",
    "mineração": "anm",
    "sqlite": "sqlite",
    "sql": "sqlite",
    "base": "sqlite",
    "rag": "rag",
}


def parse_route_command(query):
    text = str(query or "").lstrip()
    match = re.match(r"^@([A-Za-z0-9_\-çÇãÃõÕáÁéÉíÍóÓúÚ]+)\b[:\s-]*(.*)$", text, flags=re.DOTALL)
    if not match:
        return None, str(query or "").strip()

    command = match.group(1).strip().lower()
    route = COMMAND_ALIASES.get(command)
    if not route:
        return None, str(query or "").strip()

    cleaned_query = (match.group(2) or "").strip()
    return route, cleaned_query
