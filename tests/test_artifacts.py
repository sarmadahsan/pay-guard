from payguard.artifacts import pdf_page_count, store_artifact
from tests.conftest import pdf_bytes


def test_pdf_page_count_from_disk(tmp_path):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p1", tmp_path)
    assert art.kind == "pdf" and art.page_count == 12 and pdf_page_count(art.path) == 12
    assert art.summary()["page_count"] == 12 and "json" not in art.summary()


def test_pdf_page_count_on_garbage_is_none(tmp_path):
    art = store_artifact(b"%PDF-1.4 not really", "application/pdf", "p2", tmp_path)
    assert art.page_count is None
