from tools import document_tools


def test_document_reader_reads_txt_file(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("第一行\n第二行", encoding="utf-8")
    monkeypatch.setattr(document_tools, "_validate_path", lambda target, mode: (True, target, ""))

    result = document_tools.document_reader(str(path))

    assert result.success is True
    assert "第一行\n第二行" in result.data


def test_document_reader_reads_markdown_file(tmp_path, monkeypatch):
    path = tmp_path / "notes.md"
    path.write_text("# 标题\n\n正文", encoding="utf-8")
    monkeypatch.setattr(document_tools, "_validate_path", lambda target, mode: (True, target, ""))

    result = document_tools.document_reader(str(path))

    assert result.success is True
    assert "# 标题\n\n正文" in result.data
