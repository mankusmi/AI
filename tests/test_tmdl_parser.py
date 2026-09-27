from pbi_profiler.loaders.tmdl_parser import parse_tmdl


def test_simple_property_and_flag():
    text = "table Sales\n\tlineageTag: abc-123\n\tisHidden\n"
    nodes = parse_tmdl(text)
    assert len(nodes) == 1
    table = nodes[0]
    assert table.keyword == "table"
    assert table.args == "Sales"
    assert table.prop("lineageTag") == "abc-123"
    assert table.flag("isHidden") is True
    assert table.flag("isKey", default=False) is False


def test_quoted_name_with_spaces():
    text = "table Sales\n\tmeasure 'Total Sales' = SUM(Sales[Amount])\n\t\tformatString: #,0\n"
    nodes = parse_tmdl(text)
    measure = nodes[0].find("measure")
    assert measure.args == "Total Sales"
    assert measure.value == "SUM(Sales[Amount])"
    assert measure.prop("formatString") == "#,0"


def test_multiline_expression_body_dedented():
    text = (
        "table Sales\n"
        "\tmeasure 'YTD' =\n"
        "\t\t\tCALCULATE(\n"
        "\t\t\t\t[Total Sales]\n"
        "\t\t\t)\n"
        "\t\tformatString: #,0\n"
    )
    nodes = parse_tmdl(text)
    measure = nodes[0].find("measure")
    assert measure.is_expression is True
    assert measure.value == "CALCULATE(\n\t[Total Sales]\n)"
    assert measure.prop("formatString") == "#,0"


def test_calculated_column_inline_expression():
    text = (
        "table Sales\n"
        "\tcolumn 'Full Name' = Sales[First] & \" \" & Sales[Last]\n"
        "\t\tdataType: string\n"
    )
    nodes = parse_tmdl(text)
    column = nodes[0].find("column")
    assert column.args == "Full Name"
    assert column.value == 'Sales[First] & " " & Sales[Last]'
    assert column.prop("dataType") == "string"


def test_nested_children_two_levels_deep_without_expression():
    text = (
        "table Date\n"
        "\thierarchy 'Date Hierarchy'\n"
        "\t\tlevel Year = Year\n"
        "\t\tlevel Month = MonthName\n"
    )
    nodes = parse_tmdl(text)
    hierarchy = nodes[0].find("hierarchy")
    levels = hierarchy.find_all("level")
    assert [l.args for l in levels] == ["Year", "Month"]


def test_comments_and_blank_lines_are_skipped():
    text = "// a comment\ntable Sales\n\n\t// another comment\n\tlineageTag: x\n"
    nodes = parse_tmdl(text)
    assert len(nodes) == 1
    assert nodes[0].prop("lineageTag") == "x"
