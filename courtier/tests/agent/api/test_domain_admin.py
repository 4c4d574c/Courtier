"""Tests for domain scan fallback, disabled list, scaffold, and enable/disable."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from courtier.agent.api.services.domain_admin_service import (
    list_domains_with_state,
    scaffold_domain,
    set_domain_enabled,
)
from courtier.config import CourtierConfig
from courtier.domain.disabled import (
    read_disabled_domains,
    write_disabled_domains,
)


def _make_domain(root: Path, name: str, with_skill: bool = True) -> Path:
    domain = root / "domains" / name
    (domain / "config" / "prompts" / "zh-CN").mkdir(parents=True)
    (domain / "config" / "domain.yaml").write_text(
        f"name: {name}\ntitle: {name} 域\ndescription: d\nlocales: [zh-CN]\n"
        "requires_plugins: []\nrequires_services: []\n",
        encoding="utf-8",
    )
    (domain / "config" / "prompts" / "zh-CN" / "prompts.yaml").write_text(
        "notes: {}\n", encoding="utf-8"
    )
    skills = domain / "skills"
    skills.mkdir()
    if with_skill:
        (skills / "demo.md").write_text(
            "---\nname: demo\ntype: skill\nversion: '1.0'\n---\n\n# 目标\nx\n",
            encoding="utf-8",
        )
    return domain


class TestDiscoverScanFallback:
    def test_unset_env_scans_all_disk_domains(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COURTIER_DOMAIN_PACKAGES", raising=False)
        _make_domain(tmp_path, "alpha")
        _make_domain(tmp_path, "beta")
        cfg = CourtierConfig.from_env(repo_root=tmp_path)
        assert cfg.explicit_domain_names is False
        domains = cfg.discover()
        assert sorted(d.name for d in domains) == ["alpha", "beta"]

    def test_disabled_domains_excluded(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COURTIER_DOMAIN_PACKAGES", raising=False)
        _make_domain(tmp_path, "alpha")
        _make_domain(tmp_path, "beta")
        write_disabled_domains(tmp_path / "domains", {"beta"})
        domains = CourtierConfig.from_env(repo_root=tmp_path).discover()
        assert [d.name for d in domains] == ["alpha"]

    def test_explicit_allowlist_ignores_disabled_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_DOMAIN_PACKAGES", "beta")
        _make_domain(tmp_path, "alpha")
        _make_domain(tmp_path, "beta")
        write_disabled_domains(tmp_path / "domains", {"beta"})
        cfg = CourtierConfig.from_env(repo_root=tmp_path)
        assert cfg.explicit_domain_names is True
        domains = cfg.discover()
        assert [d.name for d in domains] == ["beta"]

    def test_dirs_without_manifest_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COURTIER_DOMAIN_PACKAGES", raising=False)
        _make_domain(tmp_path, "alpha")
        (tmp_path / "domains" / "random_dir").mkdir()
        domains = CourtierConfig.from_env(repo_root=tmp_path).discover()
        assert [d.name for d in domains] == ["alpha"]


class TestDisabledFile:
    def test_roundtrip(self, tmp_path):
        assert read_disabled_domains(tmp_path) == set()
        write_disabled_domains(tmp_path, {"b", "a"})
        assert read_disabled_domains(tmp_path) == {"a", "b"}
        text = (tmp_path / ".disabled").read_text(encoding="utf-8")
        assert text == "a\nb\n"

    def test_comments_and_blanks_ignored(self, tmp_path):
        (tmp_path / ".disabled").write_text("# note\n\nalpha\n", encoding="utf-8")
        assert read_disabled_domains(tmp_path) == {"alpha"}


class TestScaffoldDomain:
    def test_scaffold_creates_valid_package(self, tmp_path):
        result = scaffold_domain(tmp_path, name="legal", title="法务", description="法务文档")
        domain = tmp_path / "domains" / "legal"
        assert (domain / "config" / "domain.yaml").is_file()
        assert (domain / "config" / "prompts" / "zh-CN" / "prompts.yaml").is_file()
        assert (domain / "skills").is_dir()
        # Only the empty-skills issue should remain.
        assert any("skill" in i.lower() or "技能" in i for i in result["issues"])
        assert len(result["issues"]) == 1

    def test_scaffold_rejects_duplicate(self, tmp_path):
        scaffold_domain(tmp_path, name="legal")
        with pytest.raises(HTTPException) as exc:
            scaffold_domain(tmp_path, name="legal")
        assert exc.value.status_code == 409

    def test_scaffold_rejects_bad_name(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            scaffold_domain(tmp_path, name="Bad-Name")
        assert exc.value.status_code == 400


class TestSetDomainEnabled:
    def test_disable_then_enable(self, tmp_path):
        _make_domain(tmp_path, "alpha")
        set_domain_enabled(tmp_path, "alpha", False)
        assert read_disabled_domains(tmp_path / "domains") == {"alpha"}
        domains = list_domains_with_state(tmp_path)
        assert domains[0]["enabled"] is False
        assert domains[0]["items"] == []
        set_domain_enabled(tmp_path, "alpha", True)
        assert read_disabled_domains(tmp_path / "domains") == set()
        assert list_domains_with_state(tmp_path)[0]["enabled"] is True

    def test_unknown_domain_raises(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            set_domain_enabled(tmp_path, "ghost", False)
        assert exc.value.status_code == 404
