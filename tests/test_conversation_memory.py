import unittest
import tempfile
from unittest.mock import patch

from src import conversation_memory as memory


class ConversationMemoryTests(unittest.TestCase):
    def test_build_short_memory_uses_recent_turns(self):
        history = [
            {"question": "pergunta antiga", "answer": "resposta antiga"},
            {"question": "pergunta recente", "answer": "resposta recente"},
        ]

        text = memory.build_short_memory(history, max_turns=1)

        self.assertIn("pergunta recente", text)
        self.assertNotIn("pergunta antiga", text)

    def test_retrieves_relevant_persistent_memory(self):
        with patch("src.conversation_memory.get_user_root") as mock_root:
            with tempfile.TemporaryDirectory() as tmp:
                mock_root.return_value = memory.Path(tmp)
                with patch("src.conversation_memory.build_turn_summary") as mock_summary:
                    mock_summary.side_effect = [
                        "Use a base ANM para Relatorio Anual de Lavra.",
                        "Use o botao de exportacao.",
                    ]
                    memory.append_memory_summary(
                        "Como consultar RAL?",
                        "Use a base ANM para Relatorio Anual de Lavra.",
                        base="ANM",
                        mode="RAG",
                        llm_model=None,
                    )
                    memory.append_memory_summary(
                        "Como exportar planilha?",
                        "Use o botao de exportacao.",
                        base="App",
                        mode="UI",
                        llm_model=None,
                    )

                found = memory.retrieve_relevant_memories("sancoes sobre RAL lavra", limit=1)

        self.assertEqual(len(found), 1)
        self.assertIn("Relatorio Anual de Lavra", found[0]["summary"])

    def test_memory_context_combines_short_and_retrieved(self):
        with patch("src.conversation_memory.get_user_root") as mock_root:
            with tempfile.TemporaryDirectory() as tmp:
                mock_root.return_value = memory.Path(tmp)
                with patch("src.conversation_memory.build_turn_summary") as mock_summary:
                    mock_summary.return_value = "Usuario costuma consultar normas de lavra na base ANM."
                    memory.append_memory_summary(
                        "Base preferida",
                        "Usuario costuma consultar normas de lavra na base ANM.",
                        base="ANM",
                        mode="RAG",
                        llm_model=None,
                    )
                context, meta = memory.build_memory_context(
                    "lavra mineral",
                    chat_history=[{"question": "oi", "answer": "ola"}],
                    short_turns=1,
                    retrieval_limit=2,
                    base="ANM",
                )

        self.assertIn("Memoria curta", context)
        self.assertIn("Memoria persistente", context)
        self.assertEqual(meta["short_turns"], 1)
        self.assertEqual(meta["retrieved"], 1)


if __name__ == "__main__":
    unittest.main()
