"""2x2 replay: TWO prompts x TWO versions of a night's file, through Grok's API with NO search, scored
against Kalshi's official results. It answers one question: do the high numbers come from the new
prompt, from the new news blocks, or from both?

    ~/.venvs/wnt-gap/bin/python scripts/replay_2x2.py --list         show the nights it can use. Costs nothing.
    ~/.venvs/wnt-gap/bin/python scripts/replay_2x2.py                the newest 2 usable nights (about $1.40; it asks first)
    ~/.venvs/wnt-gap/bin/python scripts/replay_2x2.py --nights 2026-10-01,2026-10-02
    ~/.venvs/wnt-gap/bin/python scripts/replay_2x2.py --full-file ~/Downloads/gap-2026-10-02-1528-preview.txt
    ~/.venvs/wnt-gap/bin/python scripts/replay_2x2.py --old gap-aba659f --new gap-be37744 --repeat 2

The four runs of a night:
    old prompt + old-style file     old prompt + full file
    new prompt + old-style file     new prompt + full file
"full file" = the file stored for that night (or the file given with --full-file for its date).
"old-style file" = the same file without the blocks added on Oct 1-2 (previous broadcasts, ABC feeds,
other networks, top stories). Date, words, word history and the Google News headlines stay.

It asks (hidden input) for the Supabase DATABASE_URL and the xAI key. It WRITES NOTHING to the
database, never trades and sends no Telegram message. It saves a report in your Downloads folder.
It never prints the database URL or a key. Safe to paste the output into chat.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from _secrets import _prompt_hidden, need_database_url  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--list", action="store_true", help="only show the usable nights and what each stored file contains")
ap.add_argument("--nights", help="comma-separated dates (default: the newest 2 usable nights)")
ap.add_argument("--old", default="gap-aba659f", help="label of the old prompt (default gap-aba659f)")
ap.add_argument("--new", default="", help="label of the new prompt (default: the prompt in this repo)")
ap.add_argument("--full-file", action="append", default=[], help="a saved Grok file to use as the full file of its date")
ap.add_argument("--repeat", type=int, default=1, help="runs per cell, averaged word by word (default 1)")
ap.add_argument("--effort", default="", help="Grok thinking effort: low / medium / high (default: the bot's setting)")
ap.add_argument("--out", help="folder for the report (default: ~/Downloads)")
ap.add_argument("--yes", action="store_true", help="skip the 'this costs money' question")
ARGS = ap.parse_args()

need_database_url("Supabase DATABASE_URL (hidden): ")
if not ARGS.list and not os.environ.get("XAI_API_KEY"):
    _k = _prompt_hidden("xAI (Grok) API key")
    if _k:
        os.environ["XAI_API_KEY"] = _k
    del _k

from gap import clock, config as C, prompt, promptlab, replay, shadow, store, xai  # noqa: E402

COST_PER_RUN = {"low": 0.06, "medium": 0.11, "high": 0.17, "xhigh": 0.25}   # measured Oct 2 (low, high); others estimated


def usable_nights(days: int = 45) -> list[dict]:
    """Settled nights with a stored file, newest first. Uses stored results only (no Kalshi call)."""
    today = clock.today_ct()
    start = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    dates = {str(p["event_date"])[:10] for p in store.packages_between(start, today)}
    dates |= {str(r["event_date"])[:10] for r in store.runs_between(start, today) if r.get("prompt_text")}
    void = set((store.get_state("void_nights", {}) or {}).keys())
    out = []
    for d in sorted(dates - void, reverse=True):
        ni = promptlab.night_input(d)
        if not ni:
            continue
        truth = promptlab.outcomes(ni)
        if len(truth) < len(ni["words"]):
            continue
        run = store.get_run_for_date(d) or {}
        out.append({"date": d, "ni": ni, "truth": truth, "blocks": replay.blocks_in(ni["user"]),
                    "prompt": run.get("prompt_version") or "?", "chars": len(ni["user"])})
    return out


def confirm(runs: int, effort: str, ask=input) -> bool:
    each = COST_PER_RUN.get(effort, 0.17)
    print("")
    print(f"This sends {runs} runs to Grok's API (no search, effort {effort}): about ${runs * each:.2f} "
          f"(${each:.2f} a run, measured Oct 2). A run takes 2 to 6 minutes; the four runs of a night go side by side.")
    if ARGS.yes:
        return True
    try:
        return ask("Type yes to spend that, anything else to stop: ").strip().lower() == "yes"
    except EOFError:
        return False


def main() -> int:
    print(f"{C.VERSION} | 2x2 replay (prompt x file) | writes nothing to the database, never trades")
    print(f"database: connected ({store.engine().url.host}), read only")
    nights = usable_nights()
    if not nights:
        print("no settled night with a stored Grok file was found")
        return 1
    with_new = [n for n in nights if any(b in n["blocks"] for b in ("ABC feeds", "other networks", "broadcasts"))]

    print("")
    print("usable nights (newest first):")
    print("date | words | said | prompt that night | file size | blocks in the stored file")
    for n in nights[:15]:
        said = int(sum(n["truth"].values()))
        print(f"{n['date']} | {len(n['ni']['words'])} | {said} | {n['prompt']} | {n['chars']:,} | {', '.join(n['blocks']) or 'none'}")
    if ARGS.list:
        print("")
        print(f"{len(with_new)} of these have the new news blocks, so they can show the FILE effect. "
              "The others can only show the PROMPT effect (2 runs each).")
        return 0

    # ---- prompts
    new_label = ARGS.new or C.PROMPT_VERSION
    if ARGS.new and ARGS.new != C.PROMPT_VERSION:
        new_text, new_where = replay.prompt_text_for(ARGS.new)
    else:
        try:
            new_text, new_where = C.prompt_text().strip(), "prompts/system_prompt.txt in this repo"
        except RuntimeError as exc:
            new_text, new_where = None, str(exc)[:120]
    old_text, old_where = replay.prompt_text_for(ARGS.old)
    print("")
    print(f"old prompt {ARGS.old}: " + (f"{len(old_text):,} characters, from {old_where}" if old_text else "NOT FOUND"))
    print(f"new prompt {new_label}: " + (f"{len(new_text):,} characters, from {new_where}" if new_text else "NOT FOUND"))
    if not old_text or not new_text:
        print("A prompt text is missing. The labels the database knows are in the 'prompt that night' column above.")
        return 1
    if old_text == new_text:
        print("The two prompts are the same text. Nothing to compare.")
        return 1

    # ---- nights and files
    by_date = {n["date"]: n for n in nights}
    if ARGS.nights:
        wanted = [d.strip() for d in ARGS.nights.split(",") if d.strip()]
        missing = [d for d in wanted if d not in by_date]
        if missing:
            print(f"cannot use {', '.join(missing)}: no stored file, or not fully settled")
            return 1
        chosen = [by_date[d] for d in wanted]
    else:
        chosen = (with_new or nights)[:2]
    overrides = {}
    for path in ARGS.full_file:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                _sys, user = prompt.split_paste(fh.read())
        except OSError as exc:
            print(f"cannot read {os.path.basename(path)}: {type(exc).__name__}")
            return 1
        d = replay.file_date(user)
        if d not in by_date:
            print(f"{os.path.basename(path)} is for {d}: that night has no stored file or is not settled")
            return 1
        lost = [w["word"] for w in by_date[d]["ni"]["words"] if w["word"] not in user]
        if lost:
            print(f"{os.path.basename(path)} does not list every word of {d} ({len(lost)} missing). Not used.")
            return 1
        overrides[d] = (user.rstrip("\n"), os.path.basename(path))
        if by_date[d] not in chosen:
            chosen.append(by_date[d])

    effort = (ARGS.effort or C.XAI_EFFORT).strip().lower()
    C.XAI_EFFORT = effort
    plan = []                                              # (date, prompt key, file key, system text, user text)
    files = {}
    for n in sorted(chosen, key=lambda x: x["date"]):
        d = n["date"]
        full, src = overrides.get(d, (n["ni"]["user"], "the stored file"))
        light = replay.old_style(full)
        same = light.strip() == full.strip()
        files[d] = {"full": full, "light": light, "same": same, "src": src}
        for pk, ptext in (("old", old_text), ("new", new_text)):
            for fk, ftext in ((("full", full),) if same else (("light", light), ("full", full))):
                plan.append((d, pk, fk, ptext, ftext))
    print("")
    print("plan:")
    for d, f in sorted(files.items()):
        note = ("one file only (it has none of the new blocks): prompt effect only" if f["same"]
                else f"old-style file {len(f['light']):,} characters, full file {len(f['full']):,} characters")
        print(f"  {d}: {f['src']}; {note}")
    runs = len(plan) * max(1, ARGS.repeat)
    if not C.XAI_API_KEY:
        print("no xAI key given: stopping before any run")
        return 1
    if not confirm(runs, effort):
        print("stopped. Nothing was sent and nothing was spent.")
        return 0

    # ---- run
    spent = {"usd": 0.0, "runs": 0, "failed": 0}

    def one(job):
        d, pk, fk, ptext, ftext = job
        names = [w["word"] for w in by_date[d]["ni"]["words"]]
        paste = ptext.rstrip() + prompt.SEP + ftext + "\n"
        got = []
        for _ in range(max(1, ARGS.repeat)):
            try:
                g = xai.forecast("plain", paste, names, d, shadow.PREFACE, on_attempt=lambda m: None)
            except Exception as exc:  # noqa: BLE001
                u = getattr(exc, "usage", None) or {}
                return job, got, f"{type(exc).__name__}: {str(exc)[:160]}", float(u.get("cost_usd") or 0.0)
            got.append(({f["word"]: float(f["probability"]) for f in g["forecasts"]},
                        float((g["usage"] or {}).get("cost_usd") or 0.0), g["seconds"]))
        return job, got, None, 0.0

    results = {}                                           # (date, prompt, file) -> word -> probability
    for d in sorted(files):
        jobs = [j for j in plan if j[0] == d]
        print(f"{d}: {len(jobs) * max(1, ARGS.repeat)} runs ...", flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for job, got, err, lost_usd in pool.map(one, jobs):
                _d, pk, fk = job[0], job[1], job[2]
                spent["usd"] += lost_usd + sum(c for _f, c, _s in got)
                spent["runs"] += len(got)
                if err:
                    spent["failed"] += 1
                    print(f"  {pk} prompt + {fk} file: FAILED ({err})")
                if got:
                    results[(d, pk, fk)] = replay.mean_forecast([f for f, _c, _s in got])
                    print(f"  {pk} prompt + {'old-style' if fk == 'light' else 'full'} file: done "
                          f"({len(got)} run(s), {sum(s for _f, _c, s in got):.0f}s, ${sum(c for _f, c, _s in got):.2f})", flush=True)

    # ---- report
    truth_by = {d: {w["word"]: by_date[d]["truth"][w["market_ticker"]] for w in by_date[d]["ni"]["words"]} for d in files}
    cells = [("old", "light"), ("old", "full"), ("new", "light"), ("new", "full")]
    name = {("old", "light"): f"{ARGS.old} + old-style file", ("old", "full"): f"{ARGS.old} + full file",
            ("new", "light"): f"{new_label} + old-style file", ("new", "full"): f"{new_label} + full file"}
    lines = [f"# 2x2 replay, {clock.now_ct().strftime('%Y-%m-%d %H:%M')} CT ({C.VERSION})", "",
             f"Model: {xai.label('plain')}, no search, effort {effort}, {max(1, ARGS.repeat)} run(s) per cell. "
             f"Old prompt {ARGS.old}, new prompt {new_label}.",
             "Brier: lower is better. 'avg not said' = average number on words that were NOT said (lower is better). "
             "'<=30' = words Book L could trade, and how many of those were said (a loss).", ""]

    def table(title: str, rows: list[tuple[str, dict]]) -> None:
        lines.extend([title, "", "cell | words | said | Brier | avg said | avg not said | <=30 | <=30 said", "---|---|---|---|---|---|---|---"])
        for label, st in rows:
            lines.append(f"{label} | {st['n']} | {st['said']} | {replay.fmt(st['brier'])} | {replay.fmt(st['avg_said'], '.0f')} | "
                         f"{replay.fmt(st['avg_not_said'], '.0f')} | {st['low']} | {st['low_said']}")
        lines.append("")

    both = [d for d in sorted(files) if not files[d]["same"] and all((d, pk, fk) in results for pk, fk in cells)]
    if both:
        table(f"ALL NIGHTS WITH BOTH FILES ({', '.join(both)})",
              [(name[c], replay.pooled([(results[(d, c[0], c[1])], truth_by[d]) for d in both])) for c in cells])
        st = {c: replay.pooled([(results[(d, c[0], c[1])], truth_by[d]) for d in both]) for c in cells}
        p_eff = ((st[("new", "light")]["brier"] + st[("new", "full")]["brier"]) - (st[("old", "light")]["brier"] + st[("old", "full")]["brier"])) / 2
        f_eff = ((st[("old", "full")]["brier"] + st[("new", "full")]["brier"]) - (st[("old", "light")]["brier"] + st[("new", "light")]["brier"])) / 2
        lines += [f"PROMPT effect (new minus old, both files averaged): {p_eff:+.3f} Brier. Minus = the new prompt is better.",
                  f"FILE effect (full minus old-style, both prompts averaged): {f_eff:+.3f} Brier. Minus = the new blocks help.",
                  "A difference under about 0.03 on this few words is noise.", ""]
    for d in sorted(files):
        rows = [(name[c], replay.cell_stats(results[(d, c[0], c[1])], truth_by[d])) for c in cells if (d, c[0], c[1]) in results]
        if rows:
            table(f"NIGHT {d}", rows)
        have = [c for c in cells if (d, c[0], c[1]) in results]
        if have:
            lines += ["word | said? | " + " | ".join(name[c] for c in have), "---|---|" + "|".join("---" for _ in have)]
            for w in by_date[d]["ni"]["words"]:
                y = truth_by[d][w["word"]]
                lines.append(f"{w['word']} | {'YES' if y >= 0.5 else 'no'} | "
                             + " | ".join(replay.fmt(results[(d, c[0], c[1])].get(w["word"]), ".0f") for c in have))
            lines.append("")
    lines.append(f"spent: ${spent['usd']:.2f} on {spent['runs']} run(s)" + (f", {spent['failed']} cell(s) failed" if spent["failed"] else ""))
    report = "\n".join(lines)
    print("")
    print(report)
    folder = os.path.expanduser(ARGS.out or "~/Downloads")
    fname = f"gap-replay-2x2-{clock.now_ct().strftime('%Y-%m-%d-%H%M')}.md"
    try:
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\nsaved: {fname} in {folder}")
    except OSError as exc:
        print(f"\ncould not save the report ({type(exc).__name__})")
    print("nothing was written to the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
