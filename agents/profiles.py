"""Portfolio company profiles (portfolio/<slug>.yaml) and the shared sender profile (sender.yaml).

A run uses one company profile merged with the sender profile. The company is picked by
--company, by --config PATH, by a company named in the request, or, when there's only one,
automatically.
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "portfolio"
SENDER = ROOT / "sender.yaml"
SENDER_EXAMPLE = ROOT / "sender.example.yaml"
TEMPLATE = PORTFOLIO / "_template.yaml"
REQUIRED = ["company_name", "pitch"]


class ProfileError(Exception):
    pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.replace("PLACEHOLDER:", "").lower()).strip("-")


def _read(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} isn't valid YAML: {e}") from e


def companies(portfolio: Path = PORTFOLIO) -> dict[str, Path]:
    """slug -> profile path for every company file (files starting with _ are templates)."""
    return {p.stem: p for p in sorted(portfolio.glob("*.yaml")) if not p.name.startswith("_")}


def _names(path: Path) -> list[str]:
    name = str(_read(path).get("company_name", "")).replace("PLACEHOLDER:", "").strip().lower()
    return [n for n in {name, path.stem.replace("-", " ")} if len(n) >= 3]


def resolve(company: str | None = None, config: str | None = None, request: str = "",
            portfolio: Path = PORTFOLIO) -> Path:
    """Pick the company profile file for a run."""
    if config:
        p = Path(config)
        p = p if p.exists() or p.is_absolute() else ROOT / config
        if not p.exists():
            raise ProfileError(f"config not found: {config}")
        return p
    found = companies(portfolio)
    if company:
        slug = slugify(company)
        if slug in found:
            return found[slug]
        matches = [p for s, p in found.items() if slug in s or any(slug.replace("-", " ") in n for n in _names(p))]
        if len(matches) == 1:
            return matches[0]
        raise ProfileError(f"no single portfolio company matches '{company}'. Known: {', '.join(found) or 'none'}")
    if not found:
        raise ProfileError("no company profiles in portfolio/. Create one with --new-company \"Company Name\"")
    text = request.lower()
    named = [p for p in found.values() if any(re.search(rf"\b{re.escape(n)}\b", text) for n in _names(p))]
    if len(named) == 1:
        return named[0]
    if len(found) == 1:
        return next(iter(found.values()))
    raise ProfileError(f"which portfolio company is this for? Pass --company with one of: {', '.join(found)}")


def load(path: Path, sender_path: Path | None = None) -> dict:
    """Company profile merged with the shared sender profile (the company's own sender: keys win)."""
    cfg = _read(path)
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise ProfileError(f"{path} is missing: {', '.join(missing)}")
    sp = sender_path or (SENDER if SENDER.exists() else SENDER_EXAMPLE)
    sender = _read(sp) if sp.exists() else {}
    cfg["sender"] = {**sender, **(cfg.get("sender") or {})}
    if not cfg["sender"].get("name"):
        raise ProfileError(f"no sender name: fill in {SENDER.name} (copy {SENDER_EXAMPLE.name})")
    return cfg


def new_company(name: str, portfolio: Path = PORTFOLIO) -> Path:
    path = portfolio / f"{slugify(name)}.yaml"
    if path.exists():
        raise ProfileError(f"{path} already exists")
    text = TEMPLATE.read_text(encoding="utf-8").replace('"PLACEHOLDER: Company name"', json.dumps(name))
    path.write_text(text, encoding="utf-8")
    return path

