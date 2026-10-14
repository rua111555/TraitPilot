"""M6 输入校验单测：只拦截明显错误，宽容优先。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.validation import validate_args  # noqa: E402


BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令"},
        "background": {"type": "boolean"},
        "timeout": {"type": "integer"},
    },
    "required": ["command"],
}


def test_valid_passes():
    assert validate_args(BASH_SCHEMA, {"command": "ls", "timeout": 30}) is None
    assert validate_args(BASH_SCHEMA, {"command": "ls"}) is None  # 可选字段缺失放行


def test_missing_required():
    err = validate_args(BASH_SCHEMA, {"timeout": 30})
    assert err and "command" in err and "必填" in err


def test_wrong_type_string_for_integer():
    # 现状会 int("abc") 抛异常被包成泛化 [异常]，现在应被前置拦截
    err = validate_args(BASH_SCHEMA, {"command": "ls", "timeout": "abc"})
    assert err and "timeout" in err and "integer" in err


def test_bool_not_accepted_as_integer():
    err = validate_args(BASH_SCHEMA, {"command": "ls", "timeout": True})
    assert err and "timeout" in err


def test_unknown_type_name_is_lenient():
    schema = {"type": "object", "properties": {"x": {"type": "weird"}}}
    assert validate_args(schema, {"x": object()}) is None


def test_empty_or_missing_schema_passes():
    assert validate_args({}, {"any": 1}) is None
    assert validate_args(None, {"any": 1}) is None


def test_non_dict_args():
    err = validate_args(BASH_SCHEMA, "not-a-dict")
    assert err and "JSON 对象" in err


def test_enum():
    schema = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["a", "b"]}}}
    assert validate_args(schema, {"mode": "a"}) is None
    err = validate_args(schema, {"mode": "c"})
    assert err and "枚举" in err


def test_nested_object_and_array():
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "required": ["id"],
                          "properties": {"id": {"type": "integer"}}},
            }
        },
    }
    assert validate_args(schema, {"items": [{"id": 1}, {"id": 2}]}) is None
    err = validate_args(schema, {"items": [{"id": 1}, {}]})
    assert err and "id" in err


def test_union_type():
    schema = {"type": "object", "properties": {"v": {"type": ["string", "null"]}}}
    assert validate_args(schema, {"v": None}) is None
    assert validate_args(schema, {"v": "x"}) is None
    err = validate_args(schema, {"v": 3})
    assert err


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
