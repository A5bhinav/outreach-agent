"""Portfolio profile selection, sender merge and company creation."""
import shutil

import pytest
import yaml

from agents import profiles
from conftest import ROOT


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    d = tmp_path / "portfolio"
    d.mkdir()
    shutil.copy(ROOT / "portfolio" / "_template.yaml", d / "_template.yaml")
    monkeypatch.setattr(profiles, "TEMPLATE", d / "_template.yaml")
    for slug, name in [("acme-robotics", "Acme Robotics"), ("beta-bio", "PLACEHOLDER: Beta Bio")]:
        (d / f"{slug}.yaml").write_text(yaml.safe_dump({"company_name": name, "pitch": "p"}))
    return d


def test_companies_skips_templates(portfolio):
    assert list(profiles.companies(portfolio)) == ["acme-robotics", "beta-bio"]


@pytest.mark.parametrize("company,request_text,expected", [
    ("acme-robotics", "", "acme-robotics"),
    ("acme", "", "acme-robotics"),                       # partial name
    (None, "find GCs for Acme Robotics in Texas", "acme-robotics"),
    (None, "beta bio needs a systems engineer", "beta-bio"),
])
def test_resolve(portfolio, company, request_text, expected):
    assert profiles.resolve(company, None, request_text, portfolio).stem == expected


def test_resolve_ambiguous_or_unknown(portfolio):
    with pytest.raises(profiles.ProfileError, match="which portfolio company"):
        profiles.resolve(None, None, "find general contractors", portfolio)
    with pytest.raises(profiles.ProfileError, match="no single portfolio company"):
        profiles.resolve("zzz", None, "", portfolio)


def test_single_company_is_automatic(portfolio):
    (portfolio / "beta-bio.yaml").unlink()
    assert profiles.resolve(None, None, "anything", portfolio).stem == "acme-robotics"


def test_load_merges_sender(portfolio, tmp_path):
    sender = tmp_path / "sender.yaml"
    sender.write_text(yaml.safe_dump({"name": "Sam", "firm": "Fund", "voice": "investor"}))
    path = portfolio / "acme-robotics.yaml"
    path.write_text(yaml.safe_dump({"company_name": "Acme Robotics", "pitch": "p", "sender": {"voice": "founder"}}))
    cfg = profiles.load(path, sender)
    assert cfg["sender"] == {"name": "Sam", "firm": "Fund", "voice": "founder"}  # company override wins
    (portfolio / "bad.yaml").write_text(yaml.safe_dump({"company_name": "Bad"}))
    with pytest.raises(profiles.ProfileError, match="missing: pitch"):
        profiles.load(portfolio / "bad.yaml", sender)


def test_new_company(portfolio):
    path = profiles.new_company("Gamma Grid, Inc.", portfolio)
    assert path.name == "gamma-grid-inc.yaml"
    cfg = yaml.safe_load(path.read_text())
    assert cfg["company_name"] == "Gamma Grid, Inc." and "PLACEHOLDER" in cfg["pitch"]
    with pytest.raises(profiles.ProfileError, match="already exists"):
        profiles.new_company("Gamma Grid, Inc.", portfolio)


def test_shipped_files_parse():
    assert "example-robotics" in profiles.companies()
    cfg = profiles.load(profiles.companies()["example-robotics"], ROOT / "sender.example.yaml")
    assert cfg["sender"]["firm_address"] and cfg["company_name"]
