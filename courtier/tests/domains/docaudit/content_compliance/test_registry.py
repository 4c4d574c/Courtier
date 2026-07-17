from content_compliance.core import ComplianceResult
from content_compliance.registry import CheckerRegistry, get_checker, list_supported_types, register


class FakeChecker:
    @property
    def doc_type(self) -> str:
        return "测试"

    async def check(self, text: str, subtype: str | None = None) -> ComplianceResult:
        return ComplianceResult(is_valid=True)


def test_registry_register_and_get():
    reg = CheckerRegistry()
    reg.register(FakeChecker())
    assert reg.get("测试") is not None
    assert reg.get("不存在") is None


def test_registry_list_supported():
    reg = CheckerRegistry()
    reg.register(FakeChecker())
    assert reg.list_supported() == ["测试"]


def test_global_register_and_get():
    register(FakeChecker())
    checker = get_checker("测试")
    assert checker is not None
    assert checker.doc_type == "测试"


def test_global_list_supported():
    types = list_supported_types()
    assert "测试" in types
