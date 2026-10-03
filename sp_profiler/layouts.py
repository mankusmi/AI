"""Group sheets into distinct layouts and cluster near-identical layouts into families."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Layout:
    layout_hash: str
    headers: list[str]               # raw headers of first occurrence
    norm_headers: list[str]
    sheets: int = 0
    files: set = field(default_factory=set)
    total_rows: int = 0
    examples: list[str] = field(default_factory=list)
    rank: int = 0
    family: str = ""

    @property
    def layout_id(self) -> str:
        return f"L{self.rank:02d}"


def build_layouts(records: list[dict]) -> list[Layout]:
    """``records``: dicts with rel_path, sheet, layout_hash, headers, norm_headers, data_rows."""
    by_hash: dict[str, Layout] = {}
    for r in records:
        lay = by_hash.get(r["layout_hash"])
        if lay is None:
            lay = by_hash[r["layout_hash"]] = Layout(r["layout_hash"], r["headers"], r["norm_headers"])
        lay.sheets += 1
        lay.files.add(r["rel_path"])
        lay.total_rows += r["data_rows"]
        if len(lay.examples) < 3 and r["rel_path"] not in lay.examples:
            lay.examples.append(r["rel_path"])
    layouts = sorted(by_hash.values(), key=lambda l: (-len(l.files), -l.sheets, l.layout_hash))
    for i, lay in enumerate(layouts, 1):
        lay.rank = i
    assign_families(layouts)
    return layouts


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def assign_families(layouts: list[Layout], threshold: float = 0.8) -> None:
    """Greedy clustering by header-set Jaccard similarity; most common layout seeds a family."""
    seeds: list[tuple[str, set]] = []
    for lay in layouts:
        hs = set(lay.norm_headers)
        for fam, seed in seeds:
            if jaccard(hs, seed) >= threshold:
                lay.family = fam
                break
        else:
            fam = f"F{len(seeds) + 1:02d}"
            seeds.append((fam, hs))
            lay.family = fam


def diff_to_seed(layout: Layout, layouts: list[Layout]) -> dict:
    """Added/removed headers relative to the first layout of the same family."""
    seed = next(l for l in layouts if l.family == layout.family)
    a, b = set(seed.norm_headers), set(layout.norm_headers)
    return {"added": sorted(b - a), "removed": sorted(a - b),
            "reordered": a == b and seed.norm_headers != layout.norm_headers}


def header_frequency(records: list[dict]) -> dict[str, dict]:
    """Per normalised header: files it appears in, and the raw spellings seen."""
    out: dict[str, dict] = defaultdict(lambda: {"files": set(), "spellings": defaultdict(int)})
    for r in records:
        for raw, norm in zip(r["headers"], r["norm_headers"]):
            out[norm]["files"].add(r["rel_path"])
            out[norm]["spellings"][raw] += 1
    return out
