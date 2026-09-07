"""
time_stop.py
Alert-only time-stop nudge: flags when an open trade has exceeded its
max hold window for its duration class. Does not close or modify any
real position - AUTO_TRADE_ENABLED stays false, this just sends a
Telegram reminder to review manually.
"""

from datetime import datetime

DAY_MAX_HOLD_HOURS = 24          # DAY-classified trades: close within this window
DAY_SWING_MAX_HOLD_HOURS = 72    # DAY/SWING: a bit more room
SWING_MAX_HOLD_HOURS = 240       # SWING: 10-day hard safety cap even if "no fixed time stop"


def record_open_trade(pair_state, signal, opened_at_iso):
    """Call this right after sending a new signal alert."""
    pair_state["open_trade"] = {
        "direction": signal["direction"],
        "entry": signal["entry"],
        "sl": signal["sl"],
        "duration_class": signal["duration"],       # "DAY" / "DAY/SWING" / "SWING"
        "condition_label": signal["condition_label"],
        "opened_at": opened_at_iso,
    }


def check_time_stop(pair_state, current_time_iso):
    """
    Returns a warning dict if the currently tracked open trade has
    exceeded its max hold window for its duration class, else None.
    """
    trade = pair_state.get("open_trade")
    if not trade:
        return None

    opened_at = datetime.fromisoformat(trade["opened_at"])
    now = datetime.fromisoformat(current_time_iso)
    hours_open = (now - opened_at).total_seconds() / 3600

    limits = {
        "DAY": DAY_MAX_HOLD_HOURS,
        "DAY/SWING": DAY_SWING_MAX_HOLD_HOURS,
        "SWING": SWING_MAX_HOLD_HOURS,
    }
    limit = limits.get(trade["duration_class"], DAY_MAX_HOLD_HOURS)

    if hours_open >= limit:
        return {
            "direction": trade["direction"],
            "duration_class": trade["duration_class"],
            "hours_open": round(hours_open, 1),
            "limit_hours": limit,
            "message": (
                f"⏰ TIME STOP — {trade['duration_class']} trade has been open "
                f"{round(hours_open,1)}h (limit {limit}h). Review/close manually — "
                f"no auto-close is wired up."
            ),
        }
    return None


def clear_open_trade(pair_state):
    pair_state.pop("open_trade", None)
