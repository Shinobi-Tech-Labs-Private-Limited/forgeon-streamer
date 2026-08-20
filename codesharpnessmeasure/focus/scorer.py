import os

import cv2

# Raw Laplacian variance depends on board scale (pixels per checker square)
# and image contrast: a fixed threshold reads POOR on a distant board and
# SHARP on a high-contrast blurry one. The score is therefore normalized to a
# reference square size and divided by contrast squared (Laplacian variance
# scales with contrast^2). The thresholds below are PROVISIONAL for the
# normalized score — run one on-rig calibration pass before trusting the
# labels; this is a relative focus meter, not an absolute one.
REF_SQUARE_PX = 40.0
MAX_UPSCALE = 4.0
POOR_THRESH = float(os.environ.get("FOCUS_POOR_THRESH", "0.02"))
OK_THRESH = float(os.environ.get("FOCUS_OK_THRESH", "0.06"))


def compute_focus_score(frame, square_px=None):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if square_px and square_px > 0:
        scale = min(REF_SQUARE_PX / float(square_px), MAX_UPSCALE)
        # No dead-band: a 1 px change in the square estimate must not flip
        # between "resized" and "not resized" (that alone moved the score ~30 %).
        if abs(scale - 1.0) > 0.005:
            # INTER_AREA is right for shrinking; for enlarging it degenerates
            # to nearest-neighbour and manufactures hard block edges, which
            # made a distant, blurrier board score HIGHER. Use linear upscale.
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interp)
    contrast = max(float(gray.std()), 1.0)
    score = cv2.Laplacian(gray, cv2.CV_64F).var() / (contrast * contrast)
    return round(score, 4)


def classify_score(score):
    if score < POOR_THRESH:
        return 'POOR', '#e74c3c'
    elif score < OK_THRESH:
        return 'OK', '#f39c12'
    else:
        return 'SHARP', '#27ae60'


# ---------------------------------------------------------------------------
# Relative (best-so-far) verdict — what a solo operator actually needs
# ---------------------------------------------------------------------------
#
# The absolute thresholds above are provisional. Focusing alone at the camera
# you only need to know "am I at the peak?": turn the ring, watch the score
# climb, overshoot, walk back until it says PEAK. FocusTracker keeps each
# camera's best score since start / last reset and grades the current one
# against it.

PEAK_RATIO = float(os.environ.get("FOCUS_PEAK_RATIO", "0.97"))
NEAR_RATIO = float(os.environ.get("FOCUS_NEAR_RATIO", "0.85"))


class FocusTracker:
    def __init__(self):
        self._best = {}

    def best(self, cam_id):
        return self._best.get(cam_id)

    def update(self, cam_id, score):
        """Record score; return (best, ratio_to_best, label, color)."""
        best = max(self._best.get(cam_id, 0.0), float(score))
        self._best[cam_id] = best
        ratio = float(score) / best if best > 0 else 0.0
        if ratio >= PEAK_RATIO:
            return best, ratio, 'PEAK', '#2e7d32'
        if ratio >= NEAR_RATIO:
            return best, ratio, 'NEAR', '#f9a825'
        return best, ratio, 'LOW', '#c62828'

    def reset(self, cam_id=None):
        if cam_id is None:
            self._best.clear()
        else:
            self._best.pop(cam_id, None)
        return dict(self._best)


def measure_frame(frame, tracker=None, cam_id=None, target='auto'):
    """Full pipeline for one frame -> JSON-friendly payload.

    Uses the cube (or legacy board) detector, the scale/contrast-normalised
    score, the absolute PROVISIONAL label, and — when a tracker is given —
    the relative PEAK/NEAR/LOW verdict, which then drives `label`/`color`
    (the absolute one is kept as `abs_label`).
    """
    from .charuco import detect_and_segment  # local import: avoid cycle at module load

    target = str(target or 'auto').strip().lower()
    detection = detect_and_segment(frame, target=target)
    tracker_id = f'{cam_id}:{target}' if cam_id is not None else None
    if not detection.get('detected'):
        payload = {
            'detected': False,
            'target': None,
            'score': None,
            'label': 'NO BOARD' if target == 'board' else 'NO CUBE',
            'color': '#888888',
            'corners': int(detection.get('corners') or 0),
            'markers': 0,
            'faces': [],
        }
        if tracker is not None and tracker_id is not None:
            payload['best'] = tracker.best(tracker_id)
        return payload
    score = compute_focus_score(detection['board_crop'], square_px=detection.get('square_px'))
    abs_label, abs_color = classify_score(score)
    payload = {
        'detected': True,
        'target': detection.get('target'),
        'score': score,
        'label': abs_label,
        'color': abs_color,
        'abs_label': abs_label,
        'corners': int(detection.get('corners') or 0),
        'markers': int(detection.get('markers') or 0),
        'faces': list(detection.get('faces') or []),
        'square_px': (round(float(detection['square_px']), 1) if detection.get('square_px') else None),
    }
    if tracker is not None and tracker_id is not None:
        best, ratio, label, color = tracker.update(tracker_id, score)
        payload.update({'best': round(best, 4), 'ratio_to_best': round(ratio, 3), 'label': label, 'color': color})
    return payload
