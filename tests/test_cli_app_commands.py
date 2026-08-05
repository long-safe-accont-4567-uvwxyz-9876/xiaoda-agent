from cli_app import build_command_groups


def test_build_command_groups_contains_group_and_items():
    groups = build_command_groups()
    assert groups, "至少一个分组"
    for g in groups:
        assert "group" in g and "items" in g
        assert g["items"], f"分组 {g['group']} 至少一个命令"


def test_build_command_groups_has_help_and_model():
    all_names = {it["name"] for g in build_command_groups() for it in g["items"]}
    assert "/help" in all_names and "/model" in all_names


def test_build_command_groups_aliases_annotated():
    for g in build_command_groups():
        for it in g["items"]:
            if it["name"] == "/model":
                assert it["aliases"] == ["/m"]