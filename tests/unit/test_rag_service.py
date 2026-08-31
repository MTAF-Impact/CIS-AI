from app.models.fault_line import FaultLine
from app.services.rag_service import build_grounding_context


def _fault_line(community_name, grievance_theme, description=None):
    return FaultLine(community_name=community_name, grievance_theme=grievance_theme, description=description)


class TestBuildGroundingContext:
    def test_empty_input_is_empty_string(self):
        assert build_grounding_context([]) == ""

    def test_renders_community_and_theme(self):
        context = build_grounding_context([_fault_line("Penjaringan", "Tidal flooding neglect")])
        assert "Penjaringan" in context
        assert "Tidal flooding neglect" in context

    def test_includes_description_when_present(self):
        context = build_grounding_context(
            [_fault_line("Penjaringan", "Tidal flooding neglect", "Flooded three times in five years.")]
        )
        assert "Flooded three times in five years." in context

    def test_omits_description_when_absent(self):
        context = build_grounding_context([_fault_line("Penjaringan", "Tidal flooding neglect")])
        assert "None" not in context
        assert context.strip() == "- [Penjaringan] Tidal flooding neglect"

    def test_appends_extra_notes(self):
        context = build_grounding_context([], extra_notes="policy budget is $2M")
        assert "policy budget is $2M" in context
        assert "Additional context" in context

    def test_multiple_fault_lines_each_on_own_line(self):
        context = build_grounding_context(
            [_fault_line("A", "theme-a"), _fault_line("B", "theme-b")]
        )
        lines = [line for line in context.split("\n") if line.strip()]
        assert len(lines) == 2
