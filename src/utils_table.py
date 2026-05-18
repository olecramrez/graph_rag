def _md_table(headers, rows):
    if not rows:
        return "Nenhum resultado encontrado."

    header_line = "| " + " | ".join(str(h) for h in headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"

    body = []
    for row in rows:
        body.append(
            "| " + " | ".join(str(cell) if cell is not None else "" for cell in row) + " |"
        )

    return "\n".join([header_line, separator] + body)