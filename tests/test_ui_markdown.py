"""UI feature tests for the streaming-Markdown renderer.

Drives the REAL renderer methods on the shared `overlay` fixture and asserts
on Tk Text widget state (tag ranges, window_names, chat text).  No mainloop.
"""
from conftest import chat_text
import tkinter.font as tkfont


# ─── helpers ──────────────────────────────────────────────────────────────────

def _tag_present(ov, tag):
    """True if the tag has at least one range in the chat widget."""
    return len(ov.chat.tag_ranges(tag)) > 0


# ─── bold / italic / inline-code ─────────────────────────────────────────────

def test_bold_markers_consumed(overlay):
    overlay.add_delta("hello **world** end")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "world" in txt
    assert "**" not in txt


def test_bold_tag_range_non_empty(overlay):
    overlay.add_delta("a **bold** b")
    overlay._md_finalize()
    assert _tag_present(overlay, "md_b")


def test_italic_markers_consumed(overlay):
    overlay.add_delta("prefix *italic* suffix")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "italic" in txt
    assert "*italic*" not in txt
    assert _tag_present(overlay, "md_i")


def test_inline_code_markers_consumed(overlay):
    overlay.add_delta("run `ls -la` here")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "ls -la" in txt
    assert "`" not in txt
    assert _tag_present(overlay, "md_code")


def test_underscore_not_italic(overlay):
    """single underscores in snake_case identifiers must NOT be parsed as italic"""
    overlay.add_delta("a_b_c stays literal")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "a_b_c" in txt
    # no italic tag should be set
    assert not _tag_present(overlay, "md_i")


def test_mixed_inline(overlay):
    overlay.add_delta("x **b** _i_ `c` y")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "**" not in txt
    assert "`" not in txt
    assert "b" in txt
    assert "c" in txt
    assert _tag_present(overlay, "md_b")
    assert _tag_present(overlay, "md_code")


# ─── headings ────────────────────────────────────────────────────────────────

def test_h1_tag_range(overlay):
    overlay.add_delta("# Heading One\n")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "Heading One" in txt
    assert _tag_present(overlay, "md_h1")


def test_h2_tag_range(overlay):
    overlay.add_delta("## Sub Heading\n")
    overlay._md_finalize()
    assert _tag_present(overlay, "md_h2")


def test_h3_tag_range(overlay):
    overlay.add_delta("### Small Head\n")
    overlay._md_finalize()
    assert _tag_present(overlay, "md_h3")


# ─── bullet list ─────────────────────────────────────────────────────────────

def test_bullet_tag_range(overlay):
    overlay.add_delta("- first item\n")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "first item" in txt
    assert _tag_present(overlay, "md_bullet")


def test_star_bullet(overlay):
    overlay.add_delta("* star item\n")
    overlay._md_finalize()
    assert _tag_present(overlay, "md_bullet")


# ─── blockquote ──────────────────────────────────────────────────────────────

def test_blockquote_tag_range(overlay):
    overlay.add_delta("> quoted text\n")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "quoted text" in txt
    assert _tag_present(overlay, "md_quote")


# ─── fenced code block ───────────────────────────────────────────────────────

def test_fenced_code_block_tag(overlay):
    overlay.add_delta("```\nsome code\n```\n")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "some code" in txt
    assert _tag_present(overlay, "md_codeblock")


def test_fence_stars_literal(overlay):
    """**stars** inside a fenced block must NOT be parsed as bold."""
    overlay.add_delta("```\n**not bold**\n```\n")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "**not bold**" in txt          # stars stay literal
    assert not _tag_present(overlay, "md_b")


# ─── streaming across chunks ─────────────────────────────────────────────────

def test_bold_across_two_deltas(overlay):
    """Bold marker split across two add_delta calls still renders correctly."""
    overlay.add_delta("**bo")
    overlay.add_delta("ld**")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "bold" in txt
    assert "**" not in txt
    assert _tag_present(overlay, "md_b")


def test_unclosed_bold_stays_literal(overlay):
    """An unclosed **marker shows raw until the closing token arrives."""
    overlay.add_delta("open **marker only")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "**" in txt          # still raw — no closing **
    # no bold tag should be set (nothing closed)
    assert not _tag_present(overlay, "md_b")


# ─── table ───────────────────────────────────────────────────────────────────

def test_table_embeds_canvas(overlay):
    md = (
        "| Name | Score |\n"
        "| --- | --- |\n"
        "| Alice | 10 |\n"
        "| Bob | 20 |\n"
    )
    overlay.add_delta(md)
    overlay._md_finalize()
    overlay.root.update_idletasks()
    assert len(overlay.chat.window_names()) >= 1


def test_table_body_cell_text(overlay):
    md = (
        "| Col A | Col B |\n"
        "| ----- | ----- |\n"
        "| Hello | World |\n"
    )
    overlay.add_delta(md)
    overlay._md_finalize()
    overlay.root.update_idletasks()
    # The canvas items contain the cell text; check via the canvas text items
    wins = overlay.chat.window_names()
    assert wins, "expected an embedded table canvas"
    cv = overlay.chat.nametowidget(wins[0])
    items_text = [cv.itemcget(i, "text") for i in cv.find_all()
                  if cv.type(i) == "text"]
    flat = " ".join(items_text)
    assert "Hello" in flat
    assert "World" in flat


def test_table_cjk_ascii_mixed(overlay):
    md = (
        "| 品名 | Price |\n"
        "| --- | --- |\n"
        "| 蘋果 | 30 |\n"
    )
    overlay.add_delta(md)
    overlay._md_finalize()
    overlay.root.update_idletasks()
    assert len(overlay.chat.window_names()) >= 1


# ─── pipe inside inline-code does NOT split a cell ───────────────────────────

def test_pipe_in_inline_code_not_split(overlay):
    """`a|b` inside a cell must stay as one cell, not split on the pipe."""
    row = "| `a|b` | other |"
    cells = overlay._md_split_table_cells(row)
    # expect exactly 2 cells: the code-containing one and "other"
    assert len(cells) == 2
    # the first cell should contain a|b (backticks stripped)
    assert "a|b" in cells[0]


def test_split_normal_row(overlay):
    row = "| one | two | three |"
    cells = overlay._md_split_table_cells(row)
    assert cells == ["one", "two", "three"]


# ─── tool chip ───────────────────────────────────────────────────────────────

def test_tool_chip_embeds_canvas(overlay):
    overlay.add_tool("Bash", {"command": "echo hi"})
    overlay.root.update_idletasks()
    assert len(overlay.chat.window_names()) >= 1


def test_tool_chip_long_command_ellipsized(overlay):
    long_cmd = "python " + "a" * 200
    overlay.add_tool("Bash", {"command": long_cmd})
    overlay.root.update_idletasks()
    wins = overlay.chat.window_names()
    assert wins, "expected a chip canvas"
    cv = overlay.chat.nametowidget(wins[0])
    items_text = [cv.itemcget(i, "text") for i in cv.find_all()
                  if cv.type(i) == "text"]
    flat = " ".join(items_text)
    # the full 200-char repetition must NOT appear literally
    assert "a" * 200 not in flat


def test_truncate_to_px_exact_fit(overlay):
    f = tkfont.Font(root=overlay.root, font=overlay.f_chip)
    text = "hello world"
    width = f.measure(text)
    result = overlay._truncate_to_px(f, text, width)
    assert result == text


def test_truncate_to_px_too_wide_gets_ellipsis(overlay):
    f = tkfont.Font(root=overlay.root, font=overlay.f_chip)
    text = "hello world this is a long string that exceeds budget"
    budget = f.measure("hello")  # very small
    result = overlay._truncate_to_px(f, text, budget)
    assert result.endswith("…")
    assert f.measure(result) <= budget + f.measure("…") + 2  # within tolerance


def test_truncate_to_px_zero_budget(overlay):
    f = tkfont.Font(root=overlay.root, font=overlay.f_chip)
    result = overlay._truncate_to_px(f, "any text", 0)
    assert result == ""


# ─── thinking block ──────────────────────────────────────────────────────────

def test_add_think_renders_muted_block(overlay):
    overlay.add_think("reasoning text here")
    overlay.root.update_idletasks()
    txt = chat_text(overlay)
    assert "reasoning text here" in txt
    # the "think" and "think_label" tags should be present
    assert _tag_present(overlay, "think")
    assert _tag_present(overlay, "think_label")


def test_add_think_label_text(overlay):
    overlay.add_think("step by step")
    txt = chat_text(overlay)
    assert "thinking" in txt
    assert "step by step" in txt
