import unittest
from datetime import date
from unittest.mock import patch

from src.normative_temporal import (
    detect_temporal_normative_intent,
    is_active_in_reference_period,
    status_in_reference_period,
)
from src.rag_pipeline import _evaluate_temporal_gate, _select_temporal_results
from src.rag_pipeline import (
    _apply_mining_domain_policy,
    _chat_completion_with_model_fallback,
    _detect_mining_domain_intent,
    _ensure_mining_fallback_coverage,
)
from src.rag_pipeline import build_evidence
from src.router import extract_metadata
from src.reranker import _parse_lia_rerank_response
from src.query_variants import _extract_json_array
from src.lia_client import LIAClientError


class TemporalIntentTests(unittest.TestCase):
    def test_detects_exact_day_reference(self):
        intent = detect_temporal_normative_intent(
            "Em 12/03/2019, qual era o normativo aplicado ao caso?",
            allow_llm=False,
        )

        self.assertTrue(intent["enabled"])
        self.assertEqual(intent["reference_precision"], "day")
        self.assertEqual(intent["reference_start"], date(2019, 3, 12))
        self.assertEqual(intent["reference_end"], date(2019, 3, 12))
        self.assertEqual(intent["year"], 2019)

    def test_detects_exact_day_reference_with_two_digit_year(self):
        intent = detect_temporal_normative_intent(
            "Em 12/03/19, qual era o normativo aplicado ao caso?",
            allow_llm=False,
        )

        self.assertTrue(intent["enabled"])
        self.assertEqual(intent["reference_precision"], "day")
        self.assertEqual(intent["reference_start"], date(2019, 3, 12))
        self.assertEqual(intent["reference_end"], date(2019, 3, 12))
        self.assertIsNone(intent["cutoff_notice"])

    def test_detects_month_reference(self):
        intent = detect_temporal_normative_intent(
            "Em marco de 2019, qual norma vigente se aplicava ao caso?",
            allow_llm=False,
        )

        self.assertTrue(intent["enabled"])
        self.assertEqual(intent["reference_precision"], "month")
        self.assertEqual(intent["reference_start"], date(2019, 3, 1))
        self.assertEqual(intent["reference_end"], date(2019, 3, 1))
        self.assertIn("03/2019", intent["reference_label"])
        self.assertIn("01/03/2019", intent["reference_label"])
        self.assertIn("01/03/2019", intent["cutoff_notice"])

    def test_detects_year_reference_as_first_day_cutoff(self):
        intent = detect_temporal_normative_intent(
            "Em 2019, qual norma vigente se aplicava ao caso?",
            allow_llm=False,
        )

        self.assertTrue(intent["enabled"])
        self.assertEqual(intent["reference_precision"], "year")
        self.assertEqual(intent["reference_start"], date(2019, 1, 1))
        self.assertEqual(intent["reference_end"], date(2019, 1, 1))
        self.assertIn("01/01/2019", intent["cutoff_notice"])

    def test_does_not_confuse_norm_year_with_query_time(self):
        intent = detect_temporal_normative_intent(
            "A Portaria 123/2019 revogou qual regra?",
            allow_llm=False,
        )

        self.assertFalse(intent["enabled"])
        self.assertIsNone(intent["year"])
        self.assertIsNone(intent["reference_start"])

    def test_sanctions_query_with_year_is_treated_as_temporal(self):
        intent = detect_temporal_normative_intent(
            (
                "Em 2019, quais eram as sancoes aplicaveis sobre o titulo minerario "
                "em caso de nao apresentacao do Relatorio Anual de Lavra?"
            ),
            allow_llm=False,
        )

        self.assertTrue(intent["enabled"])
        self.assertTrue(intent["has_applicability_marker"])
        self.assertEqual(intent["reference_start"], date(2019, 1, 1))

    def test_sanctions_query_with_full_date_is_treated_as_temporal(self):
        intent = detect_temporal_normative_intent(
            (
                "Em 12/03/19, quais eram as sancoes aplicaveis sobre o titulo "
                "minerario em caso de nao apresentacao do Relatorio Anual de Lavra?"
            ),
            allow_llm=False,
        )

        self.assertTrue(intent["enabled"])
        self.assertTrue(intent["has_applicability_marker"])
        self.assertEqual(intent["reference_start"], date(2019, 3, 12))

    def test_explicit_date_can_trigger_temporal_llm_even_without_normative_vocabulary(self):
        with patch("src.normative_temporal._llm_temporal_parser_enabled", return_value=True):
            with patch("src.normative_temporal._try_llm_temporal_parse", return_value=None) as mock_parse:
                intent = detect_temporal_normative_intent(
                    "Em 12/03/19, como o caso estava enquadrado?",
                    allow_llm=True,
                )

        self.assertTrue(mock_parse.called)
        self.assertTrue(intent["enabled"])
        self.assertTrue(intent["explicit_temporal_reference"])
        self.assertEqual(intent["reference_start"], date(2019, 3, 12))


class RouterMetadataTests(unittest.TestCase):
    def test_ignores_bare_temporal_year_as_document_filter(self):
        tipo, numero, ano = extract_metadata(
            "Em 2019, qual era o normativo aplicado ao caso?",
            temporal_intent={"enabled": True},
        )

        self.assertIsNone(tipo)
        self.assertIsNone(numero)
        self.assertIsNone(ano)

    def test_keeps_explicit_norm_reference_even_in_temporal_query(self):
        tipo, numero, ano = extract_metadata(
            "Em 2019, o que dizia a Portaria 123/2018 sobre o caso?",
            temporal_intent={"enabled": True},
        )

        self.assertEqual(tipo, "portaria")
        self.assertEqual(numero, "123")
        self.assertEqual(ano, "2018")


class TemporalApplicabilityTests(unittest.TestCase):
    def test_active_for_exact_day_before_revocation(self):
        chunk = {
            "data_inicio_vigencia": "2018-01-01",
            "data_fim_vigencia": "2019-03-20",
            "status_normativo": "revogado",
        }

        active = is_active_in_reference_period(
            chunk,
            reference_start=date(2019, 3, 12),
            reference_end=date(2019, 3, 12),
        )
        status = status_in_reference_period(
            chunk,
            reference_start=date(2019, 3, 12),
            reference_end=date(2019, 3, 12),
        )

        self.assertTrue(active)
        self.assertEqual(status, "vigente_na_data_e_revogada_posteriormente")

    def test_inactive_after_revocation(self):
        chunk = {
            "data_inicio_vigencia": "2018-01-01",
            "data_fim_vigencia": "2019-03-20",
            "status_normativo": "revogado",
        }

        active = is_active_in_reference_period(
            chunk,
            reference_start=date(2019, 4, 1),
            reference_end=date(2019, 4, 1),
        )
        status = status_in_reference_period(
            chunk,
            reference_start=date(2019, 4, 1),
            reference_end=date(2019, 4, 1),
        )

        self.assertFalse(active)
        self.assertEqual(status, "revogada_antes_do_periodo")

    def test_year_cutoff_does_not_activate_norm_that_started_later(self):
        chunk = {
            "data_inicio_vigencia": "2019-06-01",
            "status_normativo": "vigente",
        }

        active = is_active_in_reference_period(
            chunk,
            reference_start=date(2019, 1, 1),
            reference_end=date(2019, 1, 1),
        )
        status = status_in_reference_period(
            chunk,
            reference_start=date(2019, 1, 1),
            reference_end=date(2019, 1, 1),
        )

        self.assertFalse(active)
        self.assertEqual(status, "ainda_nao_vigente_no_periodo")


class TemporalSuccessorRecoveryTests(unittest.TestCase):
    def test_recovers_active_successor_norm_from_revocation_metadata(self):
        revoked_chunk = {
            "chunk_id": "revoked::1",
            "doc_id": "revoked_doc",
            "doc": "PORTARIA N° 782, DE 27 DE DEZEMBRO DE 2002.pdf",
            "doc_name": "PORTARIA N° 782, DE 27 DE DEZEMBRO DE 2002.pdf",
            "page": 1,
            "chunk_index": 0,
            "text": (
                "Art. 1º Instituir o Relatorio Anual de Lavra - RAL. "
                "A nao apresentacao do RAL sujeita o titular a sancoes."
            ),
            "classe_documental": "normativo",
            "tipo_norma": "Portaria",
            "numero_norma": 782,
            "ano_norma": 2002,
            "status_normativo": "revogado",
            "data_inicio_vigencia": "2002-12-27",
            "data_fim_vigencia": "2016-05-12",
            "revogado_por": "PORTARIA Nº 155, DE 12 DE MAIO DE 2016",
            "revogado_por_doc_id": "successor_doc",
            "revogado_por_data": "2016-05-12",
        }
        successor_chunk = {
            "chunk_id": "successor::1",
            "doc_id": "successor_doc",
            "doc": "Vistas dos Processos Minerarios.pdf",
            "doc_name": "Vistas dos Processos Minerarios.pdf",
            "page": 1,
            "chunk_index": 0,
            "text": (
                "PORTARIA Nº 155, DE 12 DE MAIO DE 2016. "
                "Relatorio Anual de Lavra - RAL. "
                "Os processos minerarios observam o tratamento do RAL."
            ),
            "classe_documental": "normativo",
            "tipo_norma": "Portaria",
            "numero_norma": 155,
            "ano_norma": 2016,
            "status_normativo": "vigente",
            "data_inicio_vigencia": "2016-05-12",
        }
        unrelated_chunk = {
            "chunk_id": "successor::2",
            "doc_id": "successor_doc",
            "doc": "ANM - Unidades Estaduais.pdf",
            "doc_name": "ANM - Unidades Estaduais.pdf",
            "page": 1,
            "chunk_index": 0,
            "text": "PORTARIA Nº 155, DE 12 DE MAIO DE 2016. Anexo sobre unidades estaduais.",
            "classe_documental": "normativo",
            "tipo_norma": "Portaria",
            "numero_norma": 155,
            "ano_norma": 2016,
            "status_normativo": "vigente",
            "data_inicio_vigencia": "2016-05-12",
        }
        temporal_intent = {
            "enabled": True,
            "year": 2016,
            "reference_start": date(2016, 5, 17),
            "reference_end": date(2016, 5, 17),
            "reference_label": "17/05/2016",
        }
        config = {
            "top_k_final": 4,
            "alpha_semantic": 0.7,
            "beta_lexical": 0.3,
        }

        with patch(
            "src.rag_pipeline.get_chunk_lookup",
            return_value={
                revoked_chunk["chunk_id"]: revoked_chunk,
                successor_chunk["chunk_id"]: successor_chunk,
                unrelated_chunk["chunk_id"]: unrelated_chunk,
            },
        ):
            with patch(
                "src.rag_pipeline._run_rerank_with_fallback",
                side_effect=lambda query, results, config, top_k, progress_callback=None: results[:top_k],
            ):
                selected, _ = _select_temporal_results(
                    results=[(0.9, revoked_chunk)],
                    query=(
                        "Em 17/05/2016, quais sancoes poderiam ser aplicadas "
                        "em caso de nao apresentacao do Relatorio Anual de Lavra?"
                    ),
                    config=config,
                    temporal_intent=temporal_intent,
                    base_name="ANM_Legis_tratada2",
                )

        self.assertIsNotNone(selected)
        selected_ids = [chunk["chunk_id"] for _, chunk in selected]
        self.assertIn("successor::1", selected_ids)
        gate = _evaluate_temporal_gate(selected, temporal_intent)
        self.assertTrue(gate["has_active_normative"])


class MiningDomainPolicyTests(unittest.TestCase):
    def test_detects_mining_query_intent(self):
        intent = _detect_mining_domain_intent(
            "Quais normas tratam de lavra e titulo minerario?"
        )

        self.assertTrue(intent["enabled"])
        self.assertTrue(intent["has_mining_terms"])

    def test_boosts_principal_mining_norm_before_contextual_norm(self):
        principal = {
            "chunk_id": "principal",
            "relacao_com_mineracao": "direta",
            "usar_como_fundamento_principal": "true",
            "area_juridica_principal": "minerario",
            "familia_normativa_mineraria": "regimes_de_aproveitamento",
        }
        contextual = {
            "chunk_id": "contextual",
            "relacao_com_mineracao": "sem_relacao_identificada",
            "usar_como_fundamento_principal": "false",
            "area_juridica_principal": "administrativo_institucional",
            "familia_normativa_mineraria": "nao_classificada",
        }
        intent = {
            "enabled": True,
            "has_mining_terms": True,
            "is_open_normative_query": False,
        }

        boosted = _apply_mining_domain_policy(
            [(0.80, contextual), (0.60, principal)],
            intent,
        )

        self.assertEqual(boosted[0][1]["chunk_id"], "principal")

    def test_fallback_adds_principal_only_for_explicit_mining_query(self):
        principal = {
            "chunk_id": "principal",
            "relacao_com_mineracao": "direta",
            "usar_como_fundamento_principal": "true",
        }
        contextual = {
            "chunk_id": "contextual",
            "relacao_com_mineracao": "referenciada",
            "usar_como_fundamento_principal": "false",
        }

        selected = _ensure_mining_fallback_coverage(
            reranked=[(0.9, contextual)],
            candidate_pool=[(0.4, principal)],
            mining_intent={
                "enabled": True,
                "has_mining_terms": True,
                "is_open_normative_query": False,
            },
            top_k=4,
        )
        self.assertIn("principal", [chunk["chunk_id"] for _, chunk in selected])

        open_selected = _ensure_mining_fallback_coverage(
            reranked=[(0.9, contextual)],
            candidate_pool=[(0.4, principal)],
            mining_intent={
                "enabled": True,
                "has_mining_terms": False,
                "is_open_normative_query": True,
            },
            top_k=4,
        )
        self.assertNotIn("principal", [chunk["chunk_id"] for _, chunk in open_selected])


class LiaCohereRerankTests(unittest.TestCase):
    def test_parses_cohere_style_results(self):
        payload = {
            "results": [
                {"index": 2, "relevance_score": 0.98},
                {"index": 0, "relevance_score": 0.75},
            ]
        }

        self.assertEqual(
            _parse_lia_rerank_response(payload),
            [(2, 0.98), (0, 0.75)],
        )

    def test_parses_alternate_data_results(self):
        payload = {
            "data": [
                {"document_index": 1, "score": 0.44},
            ]
        }

        self.assertEqual(_parse_lia_rerank_response(payload), [(1, 0.44)])

    def test_parses_nested_result_envelope(self):
        payload = {
            "result": {
                "results": [
                    {"documentIndex": 3, "relevanceScore": 0.91},
                    {"documentIndex": 1, "relevanceScore": 0.72},
                ]
            }
        }

        self.assertEqual(
            _parse_lia_rerank_response(payload),
            [(3, 0.91), (1, 0.72)],
        )

    def test_parses_json_string_envelope(self):
        payload = {
            "output": '{"results": [{"index": 4, "relevance_score": 0.66}]}'
        }

        self.assertEqual(_parse_lia_rerank_response(payload), [(4, 0.66)])


class FinalLlmFallbackTests(unittest.TestCase):
    def test_final_answer_uses_selected_model(self):
        calls = []

        def fake_chat_completion(
            messages,
            temperature=0.0,
            llm_model=None,
            max_retries=5,
        ):
            calls.append(llm_model)
            return "ok"

        with patch("src.rag_pipeline.chat_completion", side_effect=fake_chat_completion):
            model, answer = _chat_completion_with_model_fallback(
                [{"role": "user", "content": "teste"}],
                temperature=0.0,
                llm_model="gpt-5.4",
            )

        self.assertEqual(model, "gpt-5.4")
        self.assertEqual(answer, "ok")
        self.assertEqual(calls, ["gpt-5.4"])

    def test_final_answer_can_use_gpt_53_when_selected(self):
        calls = []

        def fake_chat_completion(
            messages,
            temperature=0.0,
            llm_model=None,
            max_retries=5,
        ):
            calls.append(llm_model)
            return "ok"

        with patch("src.rag_pipeline.chat_completion", side_effect=fake_chat_completion):
            model, answer = _chat_completion_with_model_fallback(
                [{"role": "user", "content": "teste"}],
                temperature=0.0,
                llm_model="gpt-5.3-chat",
            )

        self.assertEqual(model, "gpt-5.3-chat")
        self.assertEqual(answer, "ok")
        self.assertEqual(calls, ["gpt-5.3-chat"])

    def test_final_answer_does_not_fallback_on_404(self):
        calls = []

        def fake_chat_completion(
            messages,
            temperature=0.0,
            llm_model=None,
            max_retries=5,
        ):
            calls.append(llm_model)
            raise LIAClientError("not found", status_code=404)

        with patch("src.rag_pipeline.chat_completion", side_effect=fake_chat_completion):
            with self.assertRaises(LIAClientError):
                _chat_completion_with_model_fallback(
                    [{"role": "user", "content": "teste"}],
                    temperature=0.0,
                    llm_model="gpt-5.4",
                )

        self.assertEqual(calls, ["gpt-5.4"])

    def test_final_answer_retries_same_model_with_reduced_context_on_gateway_timeout(self):
        calls = []

        def fake_chat_completion(
            messages,
            temperature=0.0,
            llm_model=None,
            max_retries=5,
        ):
            calls.append((llm_model, max_retries, messages[1]["content"]))
            if len(calls) == 1:
                raise LIAClientError("gateway timeout", status_code=504)
            return "ok"

        with patch("src.rag_pipeline.chat_completion", side_effect=fake_chat_completion):
            model, answer = _chat_completion_with_model_fallback(
                [
                    {"role": "system", "content": "sistema"},
                    {"role": "user", "content": "Contexto:\n" + ("x" * 20000)},
                ],
                temperature=0.0,
                llm_model="gpt-5.4",
                retry_context="x" * 20000,
            )

        self.assertEqual(model, "gpt-5.4")
        self.assertEqual(answer, "ok")
        self.assertEqual(calls[0][0:2], ("gpt-5.4", 1))
        self.assertEqual(calls[1][0:2], ("gpt-5.4", 1))
        self.assertLess(len(calls[1][2]), len(calls[0][2]))


class QueryGenerationTests(unittest.TestCase):
    def test_extracts_json_array_from_query_generation_response(self):
        response = '["lavra titulo minerario", "norma aplicavel RAL", "sancoes minerarias"]'

        self.assertEqual(
            _extract_json_array(response),
            ["lavra titulo minerario", "norma aplicavel RAL", "sancoes minerarias"],
        )

    def test_extracts_array_inside_markdown(self):
        response = '```json\n{"queries": ["codigo de mineracao", "processo minerario"]}\n```'

        self.assertEqual(
            _extract_json_array(response),
            ["codigo de mineracao", "processo minerario"],
        )


class MultiBaseEvidenceTests(unittest.TestCase):
    def test_evidence_includes_base_label(self):
        evidence = build_evidence(
            [
                (
                    0.9,
                    {
                        "base_rag": "Base_A",
                        "doc": "doc.pdf",
                        "page": 1,
                        "text": "Texto sobre lavra mineral.",
                    },
                )
            ],
            "lavra mineral",
        )

        self.assertIn("Base Base_A", evidence)


if __name__ == "__main__":
    unittest.main()
