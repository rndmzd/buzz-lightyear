"""
Trigger definitions and text matching.

Source-agnostic: any input that produces text (voice, chat, hotkeys, …) can
load triggers and call TriggerEngine.handle_text().
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from config import (
    DEFAULT_ACTION,
    DEFAULT_COOLDOWN,
    DEFAULT_PHRASE,
    DEFAULT_TIME_SEC,
    DEFAULT_TRIGGERS_FILE,
    SCRIPT_DIR,
)


class TriggerConfigError(Exception):
    """Invalid or unreadable triggers configuration."""


@dataclass(frozen=True)
class TriggerAction:
    """One phrase and the output it should fire when matched."""

    phrase: str
    action: str
    time_sec: float
    toy: str | None = None
    stop_previous: int | None = None
    cooldown: float = DEFAULT_COOLDOWN


@dataclass(frozen=True)
class ActionDefaults:
    action: str = DEFAULT_ACTION
    time_sec: float = DEFAULT_TIME_SEC
    toy: str | None = None
    stop_previous: int | None = None
    cooldown: float = DEFAULT_COOLDOWN


MatchHandler = Callable[[TriggerAction, str], None]
"""Callback: (matched trigger, transcript/source text) -> None."""


def parse_stop_previous(raw: object, *, context: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TriggerConfigError(
            f"{context}: stopPrevious must be an int, got {raw!r}."
        ) from exc


def _pick(data: dict, *keys: str, default=None):
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return default


def _phrases_from_entry(entry: dict, *, index: int) -> list[str]:
    if "phrases" in entry and entry["phrases"] is not None:
        phrases = entry["phrases"]
        if not isinstance(phrases, list) or not phrases:
            raise TriggerConfigError(
                f"triggers[{index}].phrases must be a non-empty list of strings."
            )
        return [str(p) for p in phrases]
    if "phrase" in entry and entry["phrase"] is not None and str(entry["phrase"]).strip():
        return [str(entry["phrase"])]
    raise TriggerConfigError(
        f"triggers[{index}] needs a 'phrase' string or non-empty 'phrases' list."
    )


def load_triggers_document(path: Path) -> dict:
    """Load raw triggers.json document (defaults + trigger entries)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriggerConfigError(f"Triggers config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TriggerConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TriggerConfigError(f"{path}: root must be a JSON object.")
    return raw


def normalize_trigger_entries(raw_entries: list) -> list[dict]:
    """Normalize GUI/file entries into a list of editable dicts."""
    out: list[dict] = []
    for i, entry in enumerate(raw_entries or []):
        if not isinstance(entry, dict):
            raise TriggerConfigError(f"triggers[{i}] must be an object.")
        phrases = _phrases_from_entry(entry, index=i)
        item = {
            "phrases": phrases,
            "action": str(entry.get("action") or DEFAULT_ACTION),
            "timeSec": float(entry.get("timeSec", entry.get("time_sec", DEFAULT_TIME_SEC))),
            "cooldown": float(entry.get("cooldown", DEFAULT_COOLDOWN)),
        }
        toy = entry.get("toy")
        if toy:
            item["toy"] = str(toy)
        if "stopPrevious" in entry or "stop_previous" in entry:
            sp = entry.get("stopPrevious", entry.get("stop_previous"))
            if sp is not None and sp != "":
                item["stopPrevious"] = int(sp)
        out.append(item)
    return out


def expand_entries_to_actions(
    entries: list[dict],
    defaults: ActionDefaults | None = None,
) -> list[TriggerAction]:
    """Expand phrase-group entries into one TriggerAction per phrase."""
    defaults = defaults or ActionDefaults()
    triggers: list[TriggerAction] = []
    for i, entry in enumerate(entries):
        phrases = entry.get("phrases") or []
        if not phrases and entry.get("phrase"):
            phrases = [entry["phrase"]]
        if not phrases:
            raise TriggerConfigError(f"triggers[{i}] has no phrases.")
        action = str(entry.get("action") or defaults.action)
        time_sec = float(entry.get("timeSec", entry.get("time_sec", defaults.time_sec)))
        cooldown = float(entry.get("cooldown", defaults.cooldown))
        toy = entry.get("toy") or defaults.toy
        if toy is not None:
            toy = str(toy) or None
        stop_previous = entry.get("stopPrevious", entry.get("stop_previous"))
        if stop_previous is None:
            stop_previous = defaults.stop_previous
        elif stop_previous == "":
            stop_previous = None
        else:
            stop_previous = int(stop_previous)
        for phrase in phrases:
            phrase = str(phrase).strip()
            if not phrase:
                continue
            triggers.append(
                TriggerAction(
                    phrase=phrase,
                    action=action,
                    time_sec=time_sec,
                    toy=toy,
                    stop_previous=stop_previous,
                    cooldown=cooldown,
                )
            )
    return triggers


def save_triggers_document(
    path: Path,
    *,
    defaults: dict,
    entries: list[dict],
) -> None:
    """Write triggers.json from defaults + editable entries."""
    triggers_out: list[dict] = []
    for entry in entries:
        phrases = [str(p).strip() for p in (entry.get("phrases") or []) if str(p).strip()]
        if not phrases:
            continue
        item: dict = {
            "action": str(entry.get("action") or DEFAULT_ACTION),
            "timeSec": float(entry.get("timeSec", DEFAULT_TIME_SEC)),
            "cooldown": float(entry.get("cooldown", DEFAULT_COOLDOWN)),
        }
        if len(phrases) == 1:
            item["phrase"] = phrases[0]
        else:
            item["phrases"] = phrases
        toy = entry.get("toy")
        if toy:
            item["toy"] = str(toy)
        if entry.get("stopPrevious") is not None and entry.get("stopPrevious") != "":
            item["stopPrevious"] = int(entry["stopPrevious"])
        triggers_out.append(item)

    doc = {
        "defaults": {
            "action": str(defaults.get("action") or DEFAULT_ACTION),
            "timeSec": float(defaults.get("timeSec", DEFAULT_TIME_SEC)),
            "cooldown": float(defaults.get("cooldown", DEFAULT_COOLDOWN)),
        },
        "triggers": triggers_out,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_triggers_file(
    path: Path,
    base: ActionDefaults,
) -> tuple[ActionDefaults, list[TriggerAction]]:
    """Load defaults + expanded triggers from a JSON config file."""
    raw = load_triggers_document(path)

    file_defaults = raw.get("defaults") or {}
    if file_defaults is None:
        file_defaults = {}
    if not isinstance(file_defaults, dict):
        raise TriggerConfigError(f"{path}: 'defaults' must be an object.")

    defaults = ActionDefaults(
        action=str(_pick(file_defaults, "action", default=base.action)),
        time_sec=float(
            _pick(file_defaults, "timeSec", "time_sec", default=base.time_sec)
        ),
        toy=_pick(file_defaults, "toy", default=base.toy),
        stop_previous=parse_stop_previous(
            _pick(
                file_defaults,
                "stopPrevious",
                "stop_previous",
                default=base.stop_previous,
            ),
            context=f"{path} defaults.stopPrevious",
        ),
        cooldown=float(_pick(file_defaults, "cooldown", default=base.cooldown)),
    )
    if defaults.toy is not None:
        defaults = replace(defaults, toy=str(defaults.toy))

    entries = raw.get("triggers")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise TriggerConfigError(f"{path}: 'triggers' must be a list.")

    normalized = normalize_trigger_entries(entries)
    triggers = expand_entries_to_actions(normalized, defaults)
    return defaults, triggers


def resolve_config_path(config: str | Path) -> Path:
    """Resolve a triggers config path against cwd, then the project directory."""
    config_path = Path(config).expanduser()
    if config_path.is_absolute():
        return config_path
    cwd_path = Path.cwd() / config_path
    script_path = SCRIPT_DIR / config_path
    if cwd_path.is_file():
        return cwd_path
    if script_path.is_file():
        return script_path
    return cwd_path


def load_triggers(
    *,
    config: str | Path = DEFAULT_TRIGGERS_FILE,
    base: ActionDefaults | None = None,
    extra_phrases: list[str] | None = None,
) -> list[TriggerAction]:
    """
    Load triggers from JSON and optional extra phrases.

    Shared by all trigger sources (voice, future non-audio sources).
    """
    base = base or ActionDefaults()
    config_path = resolve_config_path(config)
    triggers: list[TriggerAction] = []
    defaults = base

    if config_path.is_file():
        try:
            defaults, triggers = load_triggers_file(config_path, base)
        except TriggerConfigError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Loaded {len(triggers)} trigger(s) from {config_path}")
    else:
        requested = Path(config).expanduser()
        if requested.name != DEFAULT_TRIGGERS_FILE.name:
            raise SystemExit(f"Triggers config not found: {config_path}")

    if extra_phrases:
        for phrase in extra_phrases:
            triggers.append(
                TriggerAction(
                    phrase=phrase,
                    action=defaults.action,
                    time_sec=defaults.time_sec,
                    toy=defaults.toy,
                    stop_previous=defaults.stop_previous,
                    cooldown=defaults.cooldown,
                )
            )

    if not triggers:
        triggers.append(
            TriggerAction(
                phrase=DEFAULT_PHRASE,
                action=defaults.action,
                time_sec=defaults.time_sec,
                toy=defaults.toy,
                stop_previous=defaults.stop_previous,
                cooldown=defaults.cooldown,
            )
        )
        print(
            f"No triggers.json / --phrase; using default phrase {DEFAULT_PHRASE!r}."
        )

    return triggers


def compile_phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a phrase into a whole-word, case-insensitive regex."""
    normalized = " ".join(phrase.lower().split())
    return re.compile(
        r"\b" + r"\s+".join(re.escape(w) for w in normalized.split()) + r"\b",
        re.IGNORECASE,
    )


def describe_trigger(trigger: TriggerAction) -> str:
    """Human-readable summary of a trigger's output fields."""
    parts = [f"action={trigger.action!r}", f"timeSec={trigger.time_sec}"]
    if trigger.toy:
        parts.append(f"toy={trigger.toy!r}")
    if trigger.stop_previous is not None:
        parts.append(f"stopPrevious={trigger.stop_previous}")
    return ", ".join(parts)


class TriggerEngine:
    """
    Match free-form text against configured phrases and invoke a handler.

    Any trigger source can feed text here; cooldown is tracked per phrase.
    """

    def __init__(
        self,
        triggers: list[TriggerAction],
        on_match: MatchHandler,
        *,
        log_matches: bool = True,
    ) -> None:
        self.triggers = list(triggers)
        self.on_match = on_match
        self.log_matches = log_matches
        self._matchers = [
            (t, compile_phrase_pattern(t.phrase)) for t in self.triggers
        ]
        self._last_fired: dict[str, float] = {t.phrase: 0.0 for t in self.triggers}

    def summary(self) -> str:
        return ", ".join(
            f"{t.phrase!r}→{t.action}/{t.time_sec}s" for t in self.triggers
        )

    def handle_text(self, text: str) -> list[TriggerAction]:
        """
        Check text for trigger phrases. Returns the list of triggers that fired.
        """
        if not text:
            return []
        now = time.monotonic()
        fired: list[TriggerAction] = []
        for trigger, pattern in self._matchers:
            if not pattern.search(text):
                continue
            if (now - self._last_fired[trigger.phrase]) < trigger.cooldown:
                continue
            self._last_fired[trigger.phrase] = now
            if self.log_matches:
                print(
                    f'[{datetime.now():%H:%M:%S}] Detected "{trigger.phrase}" '
                    f"in: {text!r}"
                )
            self.on_match(trigger, text)
            fired.append(trigger)
        return fired
