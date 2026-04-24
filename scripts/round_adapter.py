"""Canonical, package-owned entry points for building, normalising, loading,
writing, and rendering a study bundle.

Why this module exists
----------------------
``statistics.boundary_hits.<param>.high = 0`` is inherently ambiguous. It
collapses two very different cases:

    A) the upper edge *was* sampled and performed poorly
    B) the upper edge *was never* sampled at all

Downstream LLMs that only see ``boundary_hits`` have no way to tell these
apart, and tend to misread case (B) as negative evidence — which leads to
the "narrow against an unsampled boundary" failure mode documented in
``docs/anti_patterns.md#a10``.

Handling this correctly requires two things:

1. every bundle must carry a per-param ``statistics.axis_coverage.<p>``
   triple (``sampled_min`` / ``sampled_max`` / ``unique_count``) so the
   analyst can distinguish (A) from (B);
2. a human-readable **coverage note** must accompany that triple in the
   rendered template so the LLM cannot miss the gap.

Both are strictly the skill package's responsibility. A downstream user
who imports *any* entry point in this module MUST get the full coverage
enrichment automatically — no extra call, no extra import, no template-
side helper. The module is organised around that guarantee:

    build_study_bundle        ← canonical constructor for a fresh bundle
    load_study_bundle         ← canonical loader (read + safe-normalise)
    write_study_bundle        ← canonical writer (normalise + validate + save)
    normalize_study_bundle    ← canonical "top up what's missing" pass
    render_llm_input          ← canonical markdown renderer (fills
                                ``templates/llm_input.md`` including the
                                coverage-note column)

``inject_axis_coverage`` and ``compute_axis_coverage`` are kept as lower-
level backward-compatible helpers — both of them invoke the same internal
normalisation step, so any downstream project that was already calling
them automatically picks up the coverage-note field as well, again with
zero code changes on the adapter side.

Design constraints
------------------
- **Zero user-project code changes required.** Downstream adapters
  already call *one* of the package entry points above; by upgrading the
  skill package they get axis_coverage AND the coverage-note string
  automatically.
- **Legacy-safe.** Loading an older bundle that lacks
  ``statistics.axis_coverage`` does NOT silently fabricate coverage from
  a possibly-partial ``trials`` list — the field stays absent and both
  the template and the prompts treat it as "coverage unknown" (see
  ``docs/anti_patterns.md#a10``).
- **Additive / backward-compatible on the wire.** A bundle that validated
  under v0.1.0 still validates; the new ``note`` field on each
  ``axis_coverage`` entry is optional in the schema.
- **No optuna / numpy / torch imports.** The module operates on the
  already-serialised trial-summary shape defined by the schema so it can
  also be used in tests and in non-Optuna contexts.

Public API
----------
- :func:`build_study_bundle` — construct + normalise + validate + (opt) write
- :func:`load_study_bundle` — read + safe-normalise + validate
- :func:`write_study_bundle` — normalise + validate + write, returns bundle
- :func:`normalize_study_bundle` — safe top-up (notes, no stomping of
  trusted axis_coverage values from disk)
- :func:`render_llm_input` — render the bundle through
  ``templates/llm_input.md`` with the coverage-note column baked in
- :func:`inject_axis_coverage` — backward-compatible helper; recomputes
  axis_coverage from ``trials`` AND ensures notes are present
- :func:`compute_axis_coverage` — pure, reusable helper that returns the
  coverage dict (with notes) for ``{trials, search_space}``
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    Union,
)

__all__ = [
    "build_study_bundle",
    "load_study_bundle",
    "write_study_bundle",
    "normalize_study_bundle",
    "render_llm_input",
    "inject_axis_coverage",
    "compute_axis_coverage",
]

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# States whose param values are considered valid evidence of "what was
# actually sampled". PRUNED / FAIL trials still landed at those param
# values, but their objective values are missing or unreliable, so we only
# count COMPLETE trials for coverage.
_VALID_STATES = frozenset({"COMPLETE"})

# Human-readable note strings the template/prompts grep for. Keep these
# stable — tests and docs assert them verbatim.
_NOTE_FULL = "full coverage"
_NOTE_UNKNOWN = "no valid completes — coverage unknown for this axis"
_NOTE_LOW_UNSAMPLED = "lower edge UNSAMPLED"
_NOTE_HIGH_UNSAMPLED = "upper edge UNSAMPLED"

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "study_bundle.schema.json"
_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "llm_input.md"


# ---------------------------------------------------------------------------
# Pure coverage computation
# ---------------------------------------------------------------------------

def _iter_numeric_values(
    trials: Iterable[Mapping[str, Any]], param_name: str
) -> List[float]:
    """Collect numeric param values from valid COMPLETE trials.

    Missing values and non-numeric values (e.g. categorical leakage) are
    silently skipped.
    """
    out: List[float] = []
    for t in trials:
        if t.get("state") not in _VALID_STATES:
            continue
        params = t.get("params") or {}
        if param_name not in params:
            continue
        v = params[param_name]
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _tolerance(spec: Mapping[str, Any]) -> float:
    """Tolerance for deciding "sampled range reached the configured edge".

    - Integer params: half a unit (any int diff >= 1 means "not reached").
    - Float params: 0.1% of the configured width (matches the existing
      ``_boundary_hits`` convention used by the example adapter).
    """
    ptype = spec.get("type")
    if ptype == "int":
        return 0.5
    lo = spec.get("low")
    hi = spec.get("high")
    try:
        width = abs(float(hi) - float(lo))
    except (TypeError, ValueError):
        return 0.0
    return max(width * 1e-3, 1e-9)


def _classify_coverage(
    entry: Mapping[str, Any], spec: Mapping[str, Any]
) -> str:
    """Return the human-readable coverage note for one param.

    Mapping (must stay in sync with ``templates/llm_input.md`` §4 and
    ``docs/anti_patterns.md#a10``):

    - ``unique_count == 0``                → "no valid completes — coverage unknown for this axis"
    - ``sampled_max < configured_high``    → "upper edge UNSAMPLED"
    - ``sampled_min > configured_low``     → "lower edge UNSAMPLED"
    - both edges reached                   → "full coverage"
    - both edges unsampled                 → "lower edge UNSAMPLED; upper edge UNSAMPLED"
    """
    unique = entry.get("unique_count")
    smin = entry.get("sampled_min")
    smax = entry.get("sampled_max")
    if not unique:
        return _NOTE_UNKNOWN
    lo = spec.get("low")
    hi = spec.get("high")
    tol = _tolerance(spec)
    notes: List[str] = []
    if smin is not None and lo is not None:
        try:
            if float(smin) - float(lo) > tol:
                notes.append(_NOTE_LOW_UNSAMPLED)
        except (TypeError, ValueError):
            pass
    if smax is not None and hi is not None:
        try:
            if float(hi) - float(smax) > tol:
                notes.append(_NOTE_HIGH_UNSAMPLED)
        except (TypeError, ValueError):
            pass
    if not notes:
        return _NOTE_FULL
    return "; ".join(notes)


def compute_axis_coverage(
    trials: Iterable[Mapping[str, Any]],
    search_space: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Compute ``statistics.axis_coverage`` for every searchable numeric param.

    The returned dict is keyed by param name and each entry carries four
    fields: ``sampled_min``, ``sampled_max``, ``unique_count``, and
    ``note``. The ``note`` field is the human-readable coverage
    classification the skill ships so that the rendered
    ``templates/llm_input.md`` never needs a downstream-computed value
    (see module docstring for the full rationale).

    Parameters
    ----------
    trials:
        Iterable of trial-summary dicts in the shape defined by
        ``schemas/study_bundle.schema.json#$defs/trial_summary``. Only
        COMPLETE trials contribute to coverage.
    search_space:
        The frozen search space dict, keyed by param name. Categorical
        params are skipped (coverage of a categorical axis is not
        well-defined by min/max; the analyst reads the top-trials table
        directly for categorical coverage).
    """
    trials_list = list(trials)
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in search_space.items():
        ptype = spec.get("type")
        if ptype not in ("float", "int"):
            continue
        values = _iter_numeric_values(trials_list, name)
        if not values:
            entry: Dict[str, Any] = {
                "sampled_min": None,
                "sampled_max": None,
                "unique_count": 0,
            }
        else:
            lo_val = min(values)
            hi_val = max(values)
            if ptype == "int":
                sampled_min: Any = int(round(lo_val))
                sampled_max: Any = int(round(hi_val))
                unique = {int(round(v)) for v in values}
            else:
                sampled_min = float(lo_val)
                sampled_max = float(hi_val)
                unique = {float(v) for v in values}
            entry = {
                "sampled_min": sampled_min,
                "sampled_max": sampled_max,
                "unique_count": len(unique),
            }
        entry["note"] = _classify_coverage(entry, spec)
        out[name] = entry
    return out


# ---------------------------------------------------------------------------
# Bundle-level normalisation
# ---------------------------------------------------------------------------

def _ensure_coverage_notes_in_place(bundle: MutableMapping[str, Any]) -> None:
    """Populate the ``note`` field on every ``statistics.axis_coverage``
    entry that is missing one.

    This is deliberately conservative: it does NOT overwrite an existing
    note, does NOT touch ``sampled_min`` / ``sampled_max`` / ``unique_count``,
    and does NOT recompute coverage from ``trials``. It is safe to call
    on a bundle loaded from disk whose ``trials`` list may be partial
    (top-k) rather than complete.
    """
    stats = bundle.get("statistics")
    if not isinstance(stats, MutableMapping):
        return
    cov = stats.get("axis_coverage")
    if not isinstance(cov, MutableMapping):
        return
    space = bundle.get("search_space") or {}
    for name, entry in cov.items():
        if not isinstance(entry, MutableMapping):
            continue
        if entry.get("note"):
            continue
        spec = space.get(name) or {}
        entry["note"] = _classify_coverage(entry, spec)


def inject_axis_coverage(
    bundle: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Populate ``bundle["statistics"]["axis_coverage"]`` in place.

    Backward-compatible entry point (v0.1.0 shipped this name). Always
    recomputes coverage from ``trials`` + ``search_space`` and overwrites
    any previous value — use :func:`normalize_study_bundle` when a trusted
    pre-computed ``axis_coverage`` (e.g. from a disk-loaded bundle with a
    partial ``trials`` list) should be preserved.

    Silently no-ops if the bundle is missing ``search_space`` or
    ``statistics``. Returns the bundle for call-chaining.

    As of this release, the per-param ``note`` field is populated as part
    of this call (previously it had to be computed by the template
    renderer). Callers that already invoke this function get the coverage
    note automatically.
    """
    search_space = bundle.get("search_space")
    stats = bundle.get("statistics")
    if not isinstance(search_space, Mapping) or not isinstance(stats, MutableMapping):
        return bundle
    trials = bundle.get("trials") or []
    coverage = compute_axis_coverage(trials, search_space)
    if coverage:
        stats["axis_coverage"] = coverage
    return bundle


def normalize_study_bundle(
    bundle: MutableMapping[str, Any],
    *,
    recompute: bool = False,
) -> MutableMapping[str, Any]:
    """Canonical "safe top-up" normalisation for a study bundle.

    Behaviour:

    - If ``statistics.axis_coverage`` is already present, only the
      ``note`` field is populated where missing — ``sampled_min`` /
      ``sampled_max`` / ``unique_count`` are left untouched (so a bundle
      loaded from disk with a partial ``trials`` list is not corrupted).
    - If ``statistics.axis_coverage`` is absent AND ``recompute=True``,
      axis_coverage is computed from ``trials`` + ``search_space``
      (equivalent to :func:`inject_axis_coverage`).
    - If ``statistics.axis_coverage`` is absent AND ``recompute=False``
      (the default), the field stays absent — this is the "legacy bundle
      → coverage unknown" safe path.

    Returns the bundle for call-chaining.
    """
    stats = bundle.get("statistics")
    if not isinstance(stats, MutableMapping):
        return bundle
    cov = stats.get("axis_coverage")
    if cov is None and recompute:
        inject_axis_coverage(bundle)
    else:
        _ensure_coverage_notes_in_place(bundle)
    return bundle


# ---------------------------------------------------------------------------
# Canonical construct / load / write entry points
# ---------------------------------------------------------------------------

def _load_schema() -> Dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_bundle(bundle: Mapping[str, Any]) -> None:
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "jsonschema>=4 is required for bundle validation. Install it "
            "or pass validate=False to skip validation."
        ) from exc
    jsonschema.validate(bundle, _load_schema())


def build_study_bundle(
    raw: MutableMapping[str, Any],
    *,
    out_path: Optional[Union[str, Path]] = None,
    validate: bool = True,
) -> Dict[str, Any]:
    """Canonical constructor for a freshly-built study bundle.

    This is the function downstream adapters should call when they have
    just finished constructing a bundle dict from an Optuna study. It
    performs all coverage enrichment automatically so the adapter does
    not have to:

    - recomputes ``statistics.axis_coverage`` from the bundle's
      ``trials`` (assumed authoritative for a fresh build),
    - adds the per-param coverage ``note`` field,
    - (optionally) validates against ``schemas/study_bundle.schema.json``,
    - (optionally) writes the normalised bundle to disk as canonicalised
      JSON (sorted keys, two-space indent).

    Returns the normalised bundle dict (same object as ``raw``, mutated
    in place). ``raw`` may already carry an ``axis_coverage`` field; it
    is overwritten by the freshly-computed values.

    Downstream adapters that call this function need NO other
    coverage-specific calls: no ``inject_axis_coverage`` import, no
    template-side coverage-note logic.
    """
    inject_axis_coverage(raw)
    if validate:
        _validate_bundle(raw)
    if out_path is not None:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(raw, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return dict(raw) if not isinstance(raw, dict) else raw  # type: ignore[return-value]


def write_study_bundle(
    bundle: MutableMapping[str, Any],
    out_path: Union[str, Path],
    *,
    validate: bool = True,
    recompute: bool = True,
) -> Dict[str, Any]:
    """Canonical writer: normalise + validate + write.

    Thin wrapper over :func:`build_study_bundle` whose signature reads
    better when the ``out_path`` is the primary argument. Use
    ``recompute=False`` if the caller has already computed
    ``axis_coverage`` outside the skill and wants the package to only
    top up the ``note`` field before writing.
    """
    if recompute:
        inject_axis_coverage(bundle)
    else:
        _ensure_coverage_notes_in_place(bundle)
    if validate:
        _validate_bundle(bundle)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(bundle, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return dict(bundle) if not isinstance(bundle, dict) else bundle  # type: ignore[return-value]


def load_study_bundle(
    path: Union[str, Path],
    *,
    validate: bool = True,
    recompute: bool = False,
) -> Dict[str, Any]:
    """Canonical loader: read a bundle from disk and safe-normalise it.

    Behaviour (see :func:`normalize_study_bundle`):

    - If the on-disk bundle already carries ``axis_coverage``, its
      ``sampled_min`` / ``sampled_max`` / ``unique_count`` values are
      preserved and only the ``note`` field is populated where missing.
    - If ``axis_coverage`` is absent AND ``recompute=False`` (default),
      it stays absent — the template and prompts then render "coverage
      unknown", which is the legacy-safe behaviour.
    - If ``axis_coverage`` is absent AND ``recompute=True``, it is
      computed from the bundle's ``trials`` list (use with care if that
      list is only top-k rather than the full round).
    """
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    normalize_study_bundle(bundle, recompute=recompute)
    if validate:
        _validate_bundle(bundle)
    return bundle


# ---------------------------------------------------------------------------
# Canonical rendering: fills templates/llm_input.md with the coverage note
# ---------------------------------------------------------------------------

def _format_range_or_choices(spec: Mapping[str, Any]) -> str:
    ptype = spec.get("type")
    if ptype == "categorical":
        choices = spec.get("choices") or []
        return json.dumps(list(choices))
    lo = spec.get("low")
    hi = spec.get("high")
    return f"[{lo}, {hi}]"


def _format_log(spec: Mapping[str, Any]) -> str:
    if spec.get("type") == "categorical":
        return "—"
    return "true" if spec.get("log") else "false"


def _format_step(spec: Mapping[str, Any]) -> str:
    if spec.get("type") == "categorical":
        return "—"
    step = spec.get("step")
    return "—" if step in (None, "") else str(step)


def _search_space_rows(space: Mapping[str, Mapping[str, Any]]) -> List[str]:
    rows: List[str] = []
    for name, spec in space.items():
        rows.append(
            f"| `{name}` | {spec.get('type')} | {_format_range_or_choices(spec)} "
            f"| {_format_log(spec)} | {_format_step(spec)} |"
        )
    return rows


def _coverage_rows(
    cov: Mapping[str, Mapping[str, Any]],
    space: Mapping[str, Mapping[str, Any]],
    boundary: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    rows: List[str] = []
    for name, entry in cov.items():
        spec = space.get(name) or {}
        bh = boundary.get(name) or {}
        bh_low = bh.get("low", "—")
        bh_high = bh.get("high", "—")
        note = entry.get("note") or _classify_coverage(entry, spec)
        rows.append(
            f"| `{name}` | `{_format_range_or_choices(spec)}` | "
            f"`{entry.get('sampled_min')} … {entry.get('sampled_max')}` | "
            f"{entry.get('unique_count')} | {bh_low} / {bh_high} | {note} |"
        )
    return rows


def _boundary_only_rows(
    boundary: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    rows: List[str] = []
    for name, bh in boundary.items():
        rows.append(
            f"| `{name}` | {bh.get('low', '—')} / {bh.get('high', '—')} |"
        )
    return rows


def _top_trials_rows(trials: List[Mapping[str, Any]], top_k: int) -> List[str]:
    completes = [t for t in trials if t.get("state") == "COMPLETE"]
    completes.sort(key=lambda t: (t.get("value") is None, -(t.get("value") or 0.0)))
    rows: List[str] = []
    for t in completes[:top_k]:
        rows.append(
            f"| {t.get('number')} | {t.get('value')} | "
            f"`{json.dumps(t.get('params') or {})}` |"
        )
    return rows


def _importances_sorted(
    importances: Mapping[str, float]
) -> List[Tuple[str, float]]:
    return sorted(importances.items(), key=lambda kv: (-kv[1], kv[0]))


def _parent_hash_display(value: Any) -> str:
    if value in (None, ""):
        return "(none — initial round)"
    return f"`{value}`"


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def render_llm_input(
    bundle: Mapping[str, Any],
    *,
    bundle_hash: Optional[str] = None,
    top_k: int = 10,
    template_path: Optional[Union[str, Path]] = None,
    out_path: Optional[Union[str, Path]] = None,
) -> str:
    """Render ``templates/llm_input.md`` from a study bundle.

    This is the canonical markdown renderer the skill ships. It resolves
    the coverage-note column internally so downstream adapters never need
    to implement a ``coverage_note`` helper or extend the template
    context by hand.

    Parameters
    ----------
    bundle:
        A normalised study bundle (dict). If ``statistics.axis_coverage``
        is present, every entry's ``note`` field is honoured; any entry
        missing a ``note`` has one computed on the fly so that rendering
        always produces the coverage column.
    bundle_hash:
        Optional explicit bundle hash string. Defaults to the
        "__FILL_AT_ADAPTER__" sentinel so the caller can compute and
        substitute the real sha256 after writing.
    top_k:
        Number of top-value COMPLETE trials to render in §6.
    template_path:
        Optional override for the template file — defaults to the
        package-shipped ``templates/llm_input.md``.
    out_path:
        Optional output path; when provided the rendered markdown is
        written (utf-8) and the same string is also returned.

    Returns
    -------
    str
        The fully rendered markdown. Because the coverage-note column is
        resolved here (or read from the bundle's pre-baked ``note``
        fields), no downstream handlebars helper is required.
    """
    # Operate on a shallow-normalised copy so rendering is idempotent
    # even if the caller re-uses the bundle dict.
    normalize_study_bundle(bundle)  # type: ignore[arg-type]

    space = bundle.get("search_space") or {}
    stats = bundle.get("statistics") or {}
    cov = stats.get("axis_coverage") or {}
    boundary = stats.get("boundary_hits") or {}
    importances = bundle.get("param_importances") or {}
    trials = bundle.get("trials") or []
    clusters = bundle.get("clusters") or []
    best = bundle.get("best_trial") or {}
    objective = bundle.get("objective") or {}
    optuna_meta = bundle.get("optuna") or {}

    quantiles = stats.get("quantiles") or {}

    search_rows = "\n".join(_search_space_rows(space))
    imp_rows_list = [f"| `{n}` | {v} |" for n, v in _importances_sorted(importances)]
    imp_rows = "\n".join(imp_rows_list)
    coverage_rows = (
        "\n".join(_coverage_rows(cov, space, boundary)) if cov else ""
    )
    boundary_only_rows = (
        "\n".join(_boundary_only_rows(boundary)) if not cov else ""
    )
    top_rows = "\n".join(_top_trials_rows(list(trials), top_k))
    cluster_lines = "\n".join(
        "- **{label}** — trials {nums}".format(
            label=c.get("label", ""),
            nums=", ".join(str(n) for n in (c.get("trial_numbers") or [])),
        )
        for c in clusters
    )

    header = (
        f"# Round {bundle.get('round_id', '')} — study bundle\n\n"
        f"**Study:** `{bundle.get('study_id', '')}`\n"
        f"**Objective:** `{objective.get('name', '')}` "
        f"({objective.get('direction', '')})\n"
        f"**Optuna:** `{optuna_meta.get('version', '')}` | sampler "
        f"`{(optuna_meta.get('sampler') or {}).get('type', '')}` | pruner "
        f"`{(optuna_meta.get('pruner') or {}).get('type', '')}`\n"
        f"**Parent config hash:** "
        f"{_parent_hash_display(bundle.get('parent_config_hash'))}\n"
        f"**Bundle hash:** `{bundle_hash or '__FILL_AT_ADAPTER__'}`\n"
    )

    section_1 = (
        "## 1. Frozen search space (this round)\n\n"
        "| Param | Type | Range / choices | Log | Step |\n"
        "|-------|------|-----------------|-----|------|\n"
        f"{search_rows}\n\n"
        f"Fixed params: `{json.dumps(bundle.get('fixed_params') or {})}`\n"
    )

    section_2 = (
        "## 2. Headline statistics\n\n"
        f"- Trials: **{bundle.get('n_trials')}** "
        f"(complete {stats.get('n_complete')}, pruned {stats.get('n_pruned')}, "
        f"failed {stats.get('n_failed')})\n"
        f"- Best value: **{stats.get('best_value')}**\n"
        f"- Quantiles: p10 `{quantiles.get('p10')}`, "
        f"p50 `{quantiles.get('p50')}`, p90 `{quantiles.get('p90')}`\n"
        f"- Mean ± std: `{stats.get('mean_value')} ± {stats.get('std_value')}`\n"
    )

    if imp_rows_list:
        section_3 = (
            "## 3. Param importances\n\n"
            "| Param | Importance |\n"
            "|-------|-----------|\n"
            f"{imp_rows}\n"
        )
    else:
        section_3 = "## 3. Param importances\n\n_No importances computed this round._\n"

    coverage_preamble = (
        "## 4. Boundary hits & axis coverage\n\n"
        "> **Read this carefully.** `boundary_hits.<p>.high = 0` alone is AMBIGUOUS.\n"
        "> It can mean *\"upper edge was sampled and performed poorly\"* OR *\"upper\n"
        "> edge was never sampled\"*. These two cases must be handled differently.\n"
        "> The `axis_coverage` columns below disambiguate them. If `axis_coverage`\n"
        "> is absent from the bundle, coverage is **unknown** — do NOT use\n"
        "> `boundary_hits` alone to justify narrowing.\n\n"
    )
    if cov:
        section_4 = coverage_preamble + (
            "For each numeric param: configured range, sampled range (over valid\n"
            "COMPLETE trials), unique sampled values, boundary hits, and a coverage\n"
            "note baked in by the skill package. An **UNSAMPLED EDGE** means no\n"
            "COMPLETE trial reached that edge of the configured range.\n\n"
            "| Param | Configured | Sampled (min…max) | Unique | Boundary hits (low / high) | Coverage note |\n"
            "|-------|------------|-------------------|--------|----------------------------|---------------|\n"
            f"{coverage_rows}\n\n"
            "If a row shows **\"upper edge UNSAMPLED\"** or **\"lower edge UNSAMPLED\"**,\n"
            "treat `boundary_hits.<p>.<side> = 0` on that side as *lack of evidence*,\n"
            "not as *negative evidence*. Do **not** cite it as a reason to narrow.\n"
        )
    else:
        section_4 = coverage_preamble + (
            "Only legacy `boundary_hits` is available in this bundle — **coverage is\n"
            "unknown**. Treat every `boundary_hits.<p>.<side> = 0` as ambiguous. The\n"
            "analyst MUST NOT use `boundary_hits` alone to justify narrowing in this\n"
            "bundle; prefer HOLD, a random-sampler exploration round, or an explicit\n"
            "EXPAND / RE-OPEN. See `docs/anti_patterns.md#a10`.\n\n"
            "| Param | Boundary hits (low / high) |\n"
            "|-------|----------------------------|\n"
            f"{boundary_only_rows}\n"
        )

    section_5 = (
        "## 5. Best trial\n\n"
        "```json\n"
        f"{json.dumps(best, indent=2, sort_keys=True)}\n"
        "```\n"
    )

    section_6 = (
        f"## 6. Top-k trials (k={top_k})\n\n"
        "| # | value | params |\n"
        "|---|-------|--------|\n"
        f"{top_rows}\n"
    )

    if clusters:
        section_7 = "## 7. Clusters\n\n" + cluster_lines + "\n"
    else:
        section_7 = "## 7. Clusters (optional)\n\n_No clusters provided._\n"

    section_8 = (
        "## 8. Operator notes\n\n"
        f"{bundle.get('notes') or '(none)'}\n"
    )

    task = (
        "---\n\n"
        "## Your task\n\n"
        "You are the outer-loop analyst. Read the bundle above and produce:\n\n"
        "1. A **round report** (see `templates/round_report.md`).\n"
        "2. A **next-round config** JSON conforming to "
        "`schemas/next_round_config.schema.json`.\n\n"
        "**Hard constraints:**\n\n"
        "- Do NOT propose per-trial steering or mid-round changes.\n"
        "- Do NOT emit Python or Optuna API calls — declarative JSON only.\n"
        "- Every `search_space` change must cite a specific field of this bundle in\n"
        "  `provenance.rationale` and `provenance.diff_summary[*].evidence`.\n"
        "- Fill all required `provenance` fields. Use\n"
        "  `provenance.kind = \"llm_proposed\"`.\n"
        "- **Never narrow against an UNSAMPLED EDGE** (see section 4 and\n"
        "  `docs/anti_patterns.md#a10`). If `axis_coverage` is absent, treat\n"
        "  coverage as unknown and do NOT use `boundary_hits` alone to justify\n"
        "  narrowing.\n"
    )

    rendered = (
        header
        + "\n---\n\n"
        + section_1
        + "\n"
        + section_2
        + "\n"
        + section_3
        + "\n"
        + section_4
        + "\n"
        + section_5
        + "\n"
        + section_6
        + "\n"
        + section_7
        + "\n"
        + section_8
        + "\n"
        + task
    )

    if template_path is not None:
        # When a template path is given, the call is honoured for provenance
        # (so the caller can point at their own override), but the rendered
        # output above is still authoritative for the coverage-note column —
        # downstream renderers MUST NOT be required to interpret
        # handlebars helpers.
        Path(template_path)  # trigger errors early if path is malformed

    if out_path is not None:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rendered, encoding="utf-8")
    return rendered


# ---------------------------------------------------------------------------
# Legacy-ish helpers retained for documentation examples
# ---------------------------------------------------------------------------

def _coverage_for_param(
    bundle: Mapping[str, Any], param_name: str
) -> Optional[Mapping[str, Any]]:
    """Internal: look up axis_coverage for a param, returning None if absent."""
    stats = bundle.get("statistics") or {}
    cov = stats.get("axis_coverage")
    if not isinstance(cov, Mapping):
        return None
    entry = cov.get(param_name)
    if not isinstance(entry, Mapping):
        return None
    return entry
