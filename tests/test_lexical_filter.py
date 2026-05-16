import unittest
from collections import defaultdict

from src.lexical_filter import (
    doc_name_matches_lexical_query,
    docs_from_lexical_query_parts,
    parse_boolean_query,
)
from src.positional_index import search, tokenize


def build_index(chunks):
    index = defaultdict(lambda: defaultdict(list))

    for chunk_id, text in chunks.items():
        for pos, token in enumerate(tokenize(text)):
            index[token][chunk_id].append(pos)

    return {
        token: dict(postings)
        for token, postings in index.items()
    }


class LexicalFilterTests(unittest.TestCase):
    def setUp(self):
        self.index = build_index({
            "docA.pdf::p1::c0": "plano de abandono com custo estimado",
            "docB.pdf::p1::c0": "plano diretor de tecnologia",
            "docC.pdf::p1::c0": "custo operacional previsto",
            "docD.pdf::p1::c0": "abandono seguro do poco",
            "docE.pdf::p1::c0": "plano preliminar custo anual",
        })

    def test_positional_search_supports_boolean_modes(self):
        self.assertEqual(
            {c.split("::")[0] for c in search("plano AND custo", self.index)},
            {"docA.pdf", "docE.pdf"},
        )
        self.assertEqual(
            {c.split("::")[0] for c in search("plano OR operacional", self.index)},
            {"docA.pdf", "docB.pdf", "docC.pdf", "docE.pdf"},
        )
        self.assertEqual(
            {c.split("::")[0] for c in search("plano NOT diretor", self.index)},
            {"docA.pdf", "docE.pdf"},
        )

    def test_positional_search_supports_phrases_and_near(self):
        self.assertEqual(
            {c.split("::")[0] for c in search('"plano de abandono"', self.index)},
            {"docA.pdf"},
        )
        self.assertEqual(
            {c.split("::")[0] for c in search("plano near/2 custo", self.index)},
            {"docE.pdf"},
        )

    def test_filter_parser_preserves_phrases_and_near(self):
        terms, operator, exclude = parse_boolean_query(
            '"plano de abandono" AND custo NOT diretor'
        )

        self.assertEqual(terms, ['"plano de abandono"', "custo"])
        self.assertEqual(operator, "AND")
        self.assertEqual(exclude, ["diretor"])

        terms, operator, exclude = parse_boolean_query("plano near/2 custo")
        self.assertEqual(terms, ["plano near/2 custo"])
        self.assertEqual(operator, "OR")
        self.assertEqual(exclude, [])

    def test_docs_from_lexical_query_parts_combines_content_terms(self):
        terms, operator, exclude = parse_boolean_query(
            '"plano de abandono" AND custo NOT diretor'
        )

        self.assertEqual(
            docs_from_lexical_query_parts(self.index, terms, operator, exclude),
            {"docA.pdf"},
        )

    def test_docs_from_lexical_query_parts_supports_not_only(self):
        terms, operator, exclude = parse_boolean_query("NOT diretor")

        self.assertEqual(
            docs_from_lexical_query_parts(self.index, terms, operator, exclude),
            {"docA.pdf", "docC.pdf", "docD.pdf", "docE.pdf"},
        )

    def test_docs_from_lexical_query_parts_excludes_whole_document(self):
        index = build_index({
            "docA.pdf::p1::c0": "conteudo sem a sigla",
            "docA.pdf::p2::c0": "este trecho cita pdid",
            "docB.pdf::p1::c0": "conteudo sem a sigla",
        })
        terms, operator, exclude = parse_boolean_query('not "pdid"')

        self.assertEqual(
            docs_from_lexical_query_parts(index, terms, operator, exclude),
            {"docB.pdf"},
        )

    def test_doc_name_filter_supports_boolean_modes(self):
        terms, operator, exclude = parse_boolean_query("PDI AND custo NOT minuta")

        self.assertTrue(
            doc_name_matches_lexical_query(
                "Relatorio PDI custo final.pdf",
                "PDI AND custo NOT minuta",
                terms,
                operator,
                exclude,
            )
        )

    def test_doc_name_filter_treats_multiple_quoted_doc_names_as_selection(self):
        query = (
            '"_PROTOCOLO_RJ_Carta_Cronograma_EJD_e_PDI_Conceitual_BJSA.pdf" '
            'and "08_2020_SSO_Carta_Encaminhamento_PDI_Conceitual_BJSA"'
        )
        terms, operator, exclude = parse_boolean_query(query)

        self.assertEqual(operator, "AND")
        self.assertTrue(
            doc_name_matches_lexical_query(
                "_PROTOCOLO_RJ_Carta_Cronograma_EJD_e_PDI_Conceitual_BJSA.pdf",
                query,
                terms,
                operator,
                exclude,
            )
        )
        self.assertTrue(
            doc_name_matches_lexical_query(
                "08_2020_SSO_Carta_Encaminhamento_PDI_Conceitual_BJSA.pdf",
                query,
                terms,
                operator,
                exclude,
            )
        )
        self.assertFalse(
            doc_name_matches_lexical_query(
                "Outro_documento_PDI.pdf",
                query,
                terms,
                operator,
                exclude,
            )
        )
        self.assertFalse(
            doc_name_matches_lexical_query(
                "Minuta PDI custo.pdf",
                "PDI AND custo NOT minuta",
                terms,
                operator,
                exclude,
            )
        )


if __name__ == "__main__":
    unittest.main()
