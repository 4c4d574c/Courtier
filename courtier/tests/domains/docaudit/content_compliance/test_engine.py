from content_compliance.engine import RuleConfig, RuleEngine


def test_required_rule_passes():
    rules = [
        RuleConfig(
            id="R001",
            name="has greeting",
            check_scope="开头",
            check_length=50,
            type="required",
            pattern="你好",
            message="开头缺少问候语",
        ),
    ]
    engine = RuleEngine(rules)
    violations = engine.check("你好，这是一条测试文本。")
    assert len(violations) == 0


def test_required_rule_fails():
    rules = [
        RuleConfig(
            id="R001",
            name="has greeting",
            check_scope="开头",
            check_length=50,
            type="required",
            pattern="你好",
            message="开头缺少问候语",
        ),
    ]
    engine = RuleEngine(rules)
    violations = engine.check("这是一条测试文本。")
    assert len(violations) == 1
    assert violations[0].rule_id == "R001"
    assert violations[0].message == "开头缺少问候语"
    assert violations[0].position == "开头"


def test_forbidden_rule_fails():
    rules = [
        RuleConfig(
            id="R002",
            name="no swear",
            check_scope="全文",
            type="forbidden",
            pattern="脏话",
            message="全文出现禁止用语",
        ),
    ]
    engine = RuleEngine(rules)
    violations = engine.check("这是一句脏话。")
    assert len(violations) == 1
    assert violations[0].rule_id == "R002"


def test_forbidden_rule_passes():
    rules = [
        RuleConfig(
            id="R002",
            name="no swear",
            check_scope="全文",
            type="forbidden",
            pattern="脏话",
            message="全文出现禁止用语",
        ),
    ]
    engine = RuleEngine(rules)
    violations = engine.check("这是一句正常的话。")
    assert len(violations) == 0


def test_end_scope_checks_tail_only():
    rules = [
        RuleConfig(
            id="R003",
            name="end with thanks",
            check_scope="结尾",
            check_length=20,
            type="required",
            pattern="谢谢",
            message="结尾缺少致谢",
        ),
    ]
    engine = RuleEngine(rules)
    violations = engine.check(
        "谢谢阅读。这是一段很长的正文内容，需要超过二十个字符才能触发结尾检查。"
    )
    assert len(violations) == 1
    violations = engine.check("这是一段很长的正文内容。谢谢。")
    assert len(violations) == 0


def test_body_scope_checks_full_text():
    rules = [
        RuleConfig(
            id="R004",
            name="body has keyword",
            check_scope="正文",
            type="required",
            pattern="关键",
            message="正文缺少关键词",
        ),
    ]
    engine = RuleEngine(rules)
    violations = engine.check("这是一段关键内容。")
    assert len(violations) == 0
