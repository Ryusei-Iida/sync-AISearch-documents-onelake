"""Export output-free Fabric notebooks to readable percent-cell Python files."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(notebook):
    sections = [
        "# Generated from the adjacent .ipynb; edit that notebook, then run",
        "# python scripts/export_notebooks.py",
        "# Reading copy: IPython magic commands are commented out.",
        "# Import the .ipynb into Fabric for execution and parameter-cell metadata.",
        "",
    ]
    for cell in notebook["cells"]:
        kind = cell["cell_type"]
        source = cell["source"]
        if isinstance(source, list):
            source = "".join(source)
        if kind == "markdown":
            sections.append("# %% [markdown]")
            sections.extend("# " + line if line else "#" for line in source.splitlines())
        elif kind == "code":
            sections.append("# %%")
            sections.extend(
                "# " + line if line.lstrip().startswith(("%", "!")) else line
                for line in source.splitlines()
            )
        else:
            raise ValueError(f"Unsupported cell type: {kind}")
        sections.append("")
    return "\n".join(sections).rstrip("\n") + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Check that Python reading copies are up to date.")
    args = parser.parse_args()
    notebooks = sorted((ROOT / "notebooks").glob("*.ipynb"))
    if not notebooks:
        raise SystemExit("No notebooks found.")
    for path in notebooks:
        notebook = json.loads(path.read_text(encoding="utf-8"))
        for cell in notebook["cells"]:
            if cell.get("outputs") or cell.get("execution_count") is not None:
                raise ValueError(f"Clear notebook execution results before publishing: {path.name}")
        destination = path.with_suffix(".py")
        expected = render(notebook)
        if args.check:
            if not destination.exists() or destination.read_text(encoding="utf-8") != expected:
                raise SystemExit(f"Outdated Python reading copy: {destination.name}")
        else:
            destination.write_text(expected, encoding="utf-8")
        print(f"{'Checked' if args.check else 'Exported'}: {destination.name}")


if __name__ == "__main__":
    main()
