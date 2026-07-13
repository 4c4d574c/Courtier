"""Courtier CLI — validate-domain and other utilities."""
from __future__ import annotations
import sys
from pathlib import Path
import click
from courtier.domain.loader import DomainLoader

@click.group()
def main():
    """Courtier agent platform CLI."""
    pass

@main.command()
@click.argument("domain_path", type=click.Path(exists=True, path_type=Path))
def validate_domain(domain_path: Path):
    """Validate a domain package."""
    issues = DomainLoader.validate_domain(domain_path)
    if not issues:
        click.echo(f"✓ Domain package at '{domain_path}' is valid.")
        sys.exit(0)
    click.echo(f"✗ Domain package at '{domain_path}' has {len(issues)} issue(s):")
    for issue in issues:
        click.echo(f"  ✗ {issue}")
    sys.exit(1)

if __name__ == "__main__":
    main()
