"""map align: Constellate's camera frame -> tree world, via operator anchors.

Constellate exports the seed phone's OpenCV frame: +X right, +Y DOWN, +Z
into the scene, origin at an optical center, no gravity. Nothing downstream
may ever see those axes -- this module owns the one similarity transform
(Umeyama fit over >=3 operator-declared anchors) that turns them into tree
world: Z-up, meters, origin declared once per site.

Anchors over automatic correspondence search, deliberately: three anchors
is five minutes of work at any scale, is deterministic, and has no failure
mode fancier than "you fat-fingered an anchor" -- which leave-one-out
residuals name for you.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SCHEMA = "cambium.transform/1"


def umeyama(src: np.ndarray, dst: np.ndarray, *, with_scale: bool) -> tuple[float, np.ndarray, np.ndarray]:
    """Least-squares similarity: dst ~= s * R @ src + t (Umeyama 1991).

    Mirrors the estimator Constellate's own merge step uses. det(R) is
    forced +1 (proper rotation) -- a reflection can only appear if the
    anchors themselves are inconsistent, which the caller reports via
    leave-one-out.
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    n = src.shape[0]
    mu_s, mu_d = src.mean(axis=0), dst.mean(axis=0)
    sc, dc = src - mu_s, dst - mu_d
    cov = dc.T @ sc / n
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1.0
    R = U @ S @ Vt
    if with_scale:
        var_s = (sc ** 2).sum() / n
        s = float((D * np.diag(S)).sum() / var_s)
    else:
        s = 1.0
    t = mu_d - s * R @ mu_s
    return s, R, t


def _residuals(s: float, R: np.ndarray, t: np.ndarray, src, dst) -> list[float]:
    pred = (s * (R @ np.asarray(src, float).T)).T + t
    return [float(np.linalg.norm(p - d)) for p, d in zip(pred, np.asarray(dst, float))]


def fit_anchors(
    anchors: list[dict],
    measured: dict[int, list[float]],
    *,
    with_scale: bool,
    authored: dict[str, list[float]] | None = None,
) -> dict:
    """Fit the transform from anchor declarations.

    anchors: [{"index": 3, "world": [x,y,z]} | {"index": 3, "fixture_id": "B004"}]
    measured: sweep_index -> camera-frame xyz (from measured-points.json)
    authored: fixture_id -> world xyz, needed for fixture_id-form anchors.
    """
    if len(anchors) < 3:
        raise ValueError(
            f"got {len(anchors)} anchors, need >= 3 non-collinear -- add "
            f"another tape-measured lantern to anchors.json"
        )
    src, dst, labels = [], [], []
    for a in anchors:
        idx = a.get("index")
        if idx is None or idx not in measured:
            raise ValueError(
                f"anchor {a!r}: index {idx} has no measured point -- it was "
                f"dropped out of the sweep (unmapped) or is out of range; "
                f"pick an index with status 'measured' in map status"
            )
        if "world" in a:
            world = a["world"]
        elif "fixture_id" in a:
            if not authored or a["fixture_id"] not in authored:
                raise ValueError(
                    f"anchor {a!r} names fixture_id {a.get('fixture_id')!r} "
                    f"but no authored layout provides its position -- pass "
                    f"--authored path/to/fixtures.json"
                )
            world = authored[a["fixture_id"]]
        else:
            raise ValueError(
                f"anchor {a!r} needs 'world': [x,y,z] (tape) or "
                f"'fixture_id': 'B004' (authored position)"
            )
        src.append(measured[idx])
        dst.append([float(v) for v in world])
        labels.append(f"index {idx}")

    s, R, t = umeyama(np.array(src), np.array(dst), with_scale=with_scale)
    residuals = _residuals(s, R, t, src, dst)

    # Leave-one-out: refit without each anchor; the anchor whose removal
    # most improves the rest is the prime suspect for a typo.
    suspect = None
    if len(anchors) >= 4:
        base = float(np.mean(residuals))
        best_drop, best_mean = None, base
        for i in range(len(anchors)):
            keep = [j for j in range(len(anchors)) if j != i]
            s2, R2, t2 = umeyama(
                np.array([src[j] for j in keep]),
                np.array([dst[j] for j in keep]),
                with_scale=with_scale,
            )
            m = float(np.mean(_residuals(s2, R2, t2, [src[j] for j in keep], [dst[j] for j in keep])))
            if m < best_mean * 0.5:
                best_drop, best_mean = i, m
        if best_drop is not None:
            suspect = labels[best_drop]

    return {
        "s": s,
        "R": R.tolist(),
        "t": t.tolist(),
        "method": "umeyama-anchors",
        "with_scale": with_scale,
        "anchors": anchors,
        "residuals_m": [round(r, 4) for r in residuals],
        "suspect_anchor": suspect,
    }


def apply_transform(tf: dict, xyz_camera: list[float]) -> list[float]:
    s, R, t = tf["s"], np.array(tf["R"]), np.array(tf["t"])
    return [round(float(v), 6) for v in s * R @ np.array(xyz_camera, float) + t]


def write_transform(
    fit: dict, out_path: str | Path, *, created: str, session_ref: str,
    scaled_input: bool,
) -> Path:
    """Append-versioned: transform.json is always the latest; prior fits
    survive as transform.1.json, transform.2.json, ..."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        n = 1
        while (out.parent / f"{out.stem}.{n}{out.suffix}").exists():
            n += 1
        out.rename(out.parent / f"{out.stem}.{n}{out.suffix}")
    doc = {
        "schema": SCHEMA,
        "created": created,
        "session_ref": session_ref,
        "scaled_input": scaled_input,
        **fit,
    }
    out.write_text(json.dumps(doc, indent=2) + "\n")
    return out


def sanity_report(fit: dict, *, scaled_input: bool) -> list[str]:
    """Human warnings for `map align` output; empty list = all clear."""
    warnings = []
    mean_r = float(np.mean(fit["residuals_m"])) if fit["residuals_m"] else 0.0
    if mean_r > 0.3:
        warnings.append(
            f"mean anchor residual {mean_r:.2f} m is large -- suspect a "
            f"mistyped anchor"
            + (f" (leave-one-out points at {fit['suspect_anchor']})"
               if fit.get("suspect_anchor") else "")
        )
    if scaled_input and abs(fit["s"] - 1.0) > 0.05:
        warnings.append(
            f"fitted scale {fit['s']:.3f} disagrees >5% with the sweep's tape "
            f"scale -- the tape measurement and the anchors contradict each "
            f"other; re-measure one of them"
        )
    return warnings
